"""Auto-block per (player, line) M1 — state machine de 2 strikes.

Regras (decididas com Plinio em 2026-04-25):

1. Cada (player, line) tem estado independente: ACTIVE | SHADOW | PERMANENT.

2. Strike 1 (ACTIVE -> SHADOW):
   - trigger: PL acumulado da linha desde 2026-04-15 01:07 UTC <= -3.0u
   - efeito: alertas continuam sendo salvos no banco (com suppressed=TRUE),
     validados normalmente, mas NAO sao enviados ao grupo Telegram.

3. Unblock (SHADOW -> ACTIVE):
   - shadow_pl >= +1.0u E shadow_n >= 5
   - shadow_pl = PL_total - shadow_start_pl (recuperacao desde bloqueio)
   - shadow_n = qty alertas validados em SHADOW
   - block_count fica em 1 (nao reseta).

4. Strike 2 (ACTIVE com block_count=1 -> PERMANENT):
   - trigger: PL_total <= -2.0u
   - PERMANENT nunca volta automaticamente. Sem revisao manual no MVP.

5. Sem reset de strikes. Player que ja foi bloqueado uma vez carrega o
   block_count=1 indefinidamente.

6. Janela de PL: TUDO desde deploy 57fced7 (2026-04-15 01:07 UTC).
   Sem rolling window — soma cumulativa total.

Apenas M1 (tabela `alerts`). M2 nao usa este modulo.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

from loguru import logger
from sqlalchemy import and_, func, select

from src.db.models import Alert, BlockedLine
from src.db.repositories import BlockedLineRepository

# Constantes da regra
CUTOFF_UTC = datetime(2026, 4, 15, 1, 7, 0)
STRIKE1_BLOCK_PL = -3.0
STRIKE1_UNBLOCK_PL = +1.0
STRIKE1_UNBLOCK_MIN_N = 5
STRIKE2_BLOCK_PL = -2.0
LINES_TRACKED = ("over15", "over25", "over35", "over45")


def _now_naive_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _fetch_pl_per_line(blocked_repo: BlockedLineRepository) -> list[dict]:
    """Retorna [{'player', 'line', 'pl', 'n'}] desde CUTOFF_UTC, M1 only."""
    stmt = (
        select(
            Alert.losing_player.label("player"),
            Alert.best_line.label("line"),
            func.sum(Alert.profit_flat).label("pl"),
            func.count(Alert.id).label("n"),
        )
        .where(
            Alert.sent_at >= CUTOFF_UTC,
            Alert.profit_flat.is_not(None),
            Alert.best_line.is_not(None),
        )
        .group_by(Alert.losing_player, Alert.best_line)
    )
    result = await blocked_repo.execute_query(stmt)
    rows = result.all()
    return [
        {
            "player": r.player,
            "line": r.line,
            "pl": float(r.pl or 0.0),
            "n": int(r.n or 0),
        }
        for r in rows
        if r.line in LINES_TRACKED
    ]


async def _shadow_progress(
    blocked_repo: BlockedLineRepository,
    player: str,
    line: str,
    shadow_start_at: datetime,
    shadow_start_pl: float,
) -> tuple[float, int]:
    """Retorna (shadow_pl, shadow_n) — alertas APOS shadow_start_at."""
    stmt = (
        select(
            func.coalesce(func.sum(Alert.profit_flat), 0.0).label("pl"),
            func.count(Alert.id).label("n"),
        )
        .where(
            Alert.losing_player == player,
            Alert.best_line == line,
            Alert.sent_at > shadow_start_at,
            Alert.profit_flat.is_not(None),
        )
    )
    result = await blocked_repo.execute_query(stmt)
    row = result.one()
    return float(row.pl or 0.0), int(row.n or 0)


async def recompute_all_states(
    blocked_repo: BlockedLineRepository,
) -> dict[str, list[str]]:
    """Recomputa state de todas as (player, line) com alertas validados.

    Idempotente. Roda no cron horario. Retorna dict de transicoes feitas.
    """
    rows = await _fetch_pl_per_line(blocked_repo)
    transitions: dict[str, list[str]] = {
        "blocked_strike1": [],
        "blocked_strike2": [],
        "unblocked": [],
        "no_change": [],
    }

    now = _now_naive_utc()

    for r in rows:
        player = r["player"]
        line = r["line"]
        pl_total = r["pl"]
        existing = await blocked_repo.get(player, line)
        state = existing.state if existing else "ACTIVE"
        block_count = existing.block_count if existing else 0
        shadow_start_pl = existing.shadow_start_pl if existing else None
        shadow_start_at = existing.shadow_start_at if existing else None

        new_state = state
        new_block_count = block_count
        new_shadow_start_pl = shadow_start_pl
        new_shadow_start_at = shadow_start_at
        last_block_at = None
        last_unblock_at = None

        if state == "ACTIVE":
            if block_count == 0 and pl_total <= STRIKE1_BLOCK_PL:
                new_state = "SHADOW"
                new_block_count = 1
                new_shadow_start_pl = pl_total
                new_shadow_start_at = now
                last_block_at = now
                transitions["blocked_strike1"].append(f"{player}/{line} PL={pl_total:+.2f}u")
                logger.warning(
                    f"BLOCK strike1 {player}/{line}: PL={pl_total:+.2f}u <= "
                    f"{STRIKE1_BLOCK_PL} -> SHADOW"
                )
            elif block_count == 1 and pl_total <= STRIKE2_BLOCK_PL:
                new_state = "PERMANENT"
                new_block_count = 2
                last_block_at = now
                transitions["blocked_strike2"].append(f"{player}/{line} PL={pl_total:+.2f}u")
                logger.error(
                    f"BLOCK strike2 PERMANENT {player}/{line}: PL={pl_total:+.2f}u <= "
                    f"{STRIKE2_BLOCK_PL} -> PERMANENT"
                )
        elif state == "SHADOW":
            if shadow_start_at is None or shadow_start_pl is None:
                logger.warning(
                    f"SHADOW {player}/{line} sem shadow_start_at/pl — pulando recheck"
                )
                transitions["no_change"].append(f"{player}/{line} (corrupted)")
                continue
            shadow_pl, shadow_n = await _shadow_progress(
                blocked_repo, player, line, shadow_start_at, shadow_start_pl
            )
            if shadow_pl >= STRIKE1_UNBLOCK_PL and shadow_n >= STRIKE1_UNBLOCK_MIN_N:
                new_state = "ACTIVE"
                last_unblock_at = now
                transitions["unblocked"].append(
                    f"{player}/{line} shadow_pl={shadow_pl:+.2f}u n={shadow_n}"
                )
                logger.info(
                    f"UNBLOCK {player}/{line}: shadow_pl={shadow_pl:+.2f}u "
                    f">= {STRIKE1_UNBLOCK_PL}, n={shadow_n} >= {STRIKE1_UNBLOCK_MIN_N}"
                )
        # PERMANENT: nada acontece automaticamente.

        if new_state == state and new_block_count == block_count:
            transitions["no_change"].append(f"{player}/{line}")
            continue

        await blocked_repo.upsert(
            player=player,
            line=line,
            state=new_state,
            block_count=new_block_count,
            shadow_start_pl=new_shadow_start_pl,
            shadow_start_at=new_shadow_start_at,
            last_block_at=last_block_at,
            last_unblock_at=last_unblock_at,
        )

    return transitions


async def should_suppress(
    blocked_repo: BlockedLineRepository, player: str, line: str
) -> bool:
    """Quick check chamado pelo alert_engine antes de notifier.send().

    Lookup unico no banco. Se state in (SHADOW, PERMANENT) -> True.
    """
    return await blocked_repo.is_suppressed(player, line)


async def get_status(
    blocked_repo: BlockedLineRepository,
) -> list[dict]:
    """Retorna lista detalhada para o comando /blocked.

    Cada item tem: player, line, state, block_count, pl_total, shadow_pl,
    shadow_n, shadow_start_pl, shadow_start_at, needed_for_unblock.
    """
    rows = await _fetch_pl_per_line(blocked_repo)
    pl_map = {(r["player"], r["line"]): (r["pl"], r["n"]) for r in rows}

    blocked = await blocked_repo.list_blocked()
    out = []
    for bl in blocked:
        pl_total, n_total = pl_map.get((bl.player, bl.line), (0.0, 0))
        shadow_pl = 0.0
        shadow_n = 0
        if bl.state == "SHADOW" and bl.shadow_start_at and bl.shadow_start_pl is not None:
            shadow_pl, shadow_n = await _shadow_progress(
                blocked_repo, bl.player, bl.line, bl.shadow_start_at, bl.shadow_start_pl
            )
        out.append(
            {
                "player": bl.player,
                "line": bl.line,
                "state": bl.state,
                "block_count": bl.block_count,
                "pl_total": pl_total,
                "n_total": n_total,
                "shadow_pl": shadow_pl,
                "shadow_n": shadow_n,
                "shadow_start_pl": float(bl.shadow_start_pl) if bl.shadow_start_pl is not None else None,
                "shadow_start_at": bl.shadow_start_at,
                "last_block_at": bl.last_block_at,
                "needed_pl": STRIKE1_UNBLOCK_PL,
                "needed_n": STRIKE1_UNBLOCK_MIN_N,
            }
        )
    return out
