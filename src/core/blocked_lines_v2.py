"""Auto-block per (player, line) M2 — state machine de 2 strikes.

Replica do M1 (ver `blocked_lines.py`) aplicada a `alerts_v2`.

Regras (mesmas do M1, decididas com Plinio em 2026-04-26):

1. Cada (player, line) tem estado independente: ACTIVE | SHADOW | PERMANENT.

2. Strike 1 (ACTIVE -> SHADOW):
   - trigger: PL acumulado da linha desde CUTOFF_UTC <= -1.0u
   - efeito: alertas v2 continuam sendo salvos (com suppressed=TRUE),
     validados normalmente, mas NAO sao enviados ao grupo Telegram M2.

3. Unblock (SHADOW -> ACTIVE):
   - shadow_pl >= +1.0u E shadow_n >= 3
   - shadow_pl = PL_total - shadow_start_pl (recuperacao desde bloqueio)
   - shadow_n = qty alertas validados em SHADOW
   - block_count fica em 1 (nao reseta).

4. Strike 2 (ACTIVE com block_count=1 -> PERMANENT):
   - trigger: PL_total <= -2.0u
   - PERMANENT nunca volta automaticamente.

Apenas M2 (tabela `alerts_v2`). M1 usa `blocked_lines.py`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from loguru import logger
from sqlalchemy import func, select, text

from src.db.models import AlertV2, BlockedLineV2
from src.db.repositories import BlockedLineV2Repository

# Mesmas constantes do M1
CUTOFF_UTC = datetime(2026, 4, 15, 1, 7, 0)
STRIKE1_BLOCK_PL = -1.0
STRIKE1_UNBLOCK_PL = +1.0
STRIKE1_UNBLOCK_MIN_N = 3
STRIKE2_BLOCK_PL = -2.0
LINES_TRACKED = ("over15", "over25", "over35", "over45")


def _now_naive_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _fetch_pl_per_line(blocked_repo: BlockedLineV2Repository) -> list[dict]:
    """Retorna [{'player', 'line', 'pl', 'n'}] desde CUTOFF_UTC, M2 only."""
    stmt = (
        select(
            AlertV2.losing_player.label("player"),
            AlertV2.best_line.label("line"),
            func.sum(AlertV2.profit_flat).label("pl"),
            func.count(AlertV2.id).label("n"),
        )
        .where(
            AlertV2.sent_at >= CUTOFF_UTC,
            AlertV2.profit_flat.is_not(None),
            AlertV2.best_line.is_not(None),
        )
        .group_by(AlertV2.losing_player, AlertV2.best_line)
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
    blocked_repo: BlockedLineV2Repository,
    player: str,
    line: str,
    shadow_start_at: datetime,
    shadow_start_pl: float,
) -> tuple[float, int]:
    """Retorna (shadow_pl, shadow_n) — alertas v2 APOS shadow_start_at."""
    stmt = (
        select(
            func.coalesce(func.sum(AlertV2.profit_flat), 0.0).label("pl"),
            func.count(AlertV2.id).label("n"),
        )
        .where(
            AlertV2.losing_player == player,
            AlertV2.best_line == line,
            AlertV2.sent_at > shadow_start_at,
            AlertV2.profit_flat.is_not(None),
        )
    )
    result = await blocked_repo.execute_query(stmt)
    row = result.one()
    return float(row.pl or 0.0), int(row.n or 0)


async def recompute_all_states(
    blocked_repo: BlockedLineV2Repository,
) -> dict[str, list[str]]:
    """Recomputa state de todas as (player, line) em M2.

    Idempotente. Retorna dict de transicoes feitas.
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
                transitions["blocked_strike1"].append(
                    f"{player}/{line} PL={pl_total:+.2f}u"
                )
                logger.warning(
                    f"M2 BLOCK strike1 {player}/{line}: PL={pl_total:+.2f}u <= "
                    f"{STRIKE1_BLOCK_PL} -> SHADOW"
                )
            elif block_count == 1 and pl_total <= STRIKE2_BLOCK_PL:
                new_state = "PERMANENT"
                new_block_count = 2
                last_block_at = now
                transitions["blocked_strike2"].append(
                    f"{player}/{line} PL={pl_total:+.2f}u"
                )
                logger.error(
                    f"M2 BLOCK strike2 PERMANENT {player}/{line}: "
                    f"PL={pl_total:+.2f}u <= {STRIKE2_BLOCK_PL} -> PERMANENT"
                )
        elif state == "SHADOW":
            if shadow_start_at is None or shadow_start_pl is None:
                logger.warning(
                    f"M2 SHADOW {player}/{line} sem shadow_start — pulando recheck"
                )
                transitions["no_change"].append(f"{player}/{line} (corrupted)")
                continue
            shadow_pl, shadow_n = await _shadow_progress(
                blocked_repo, player, line, shadow_start_at, shadow_start_pl
            )
            if (
                shadow_pl >= STRIKE1_UNBLOCK_PL
                and shadow_n >= STRIKE1_UNBLOCK_MIN_N
            ):
                new_state = "ACTIVE"
                last_unblock_at = now
                transitions["unblocked"].append(
                    f"{player}/{line} shadow_pl={shadow_pl:+.2f}u n={shadow_n}"
                )
                logger.info(
                    f"M2 UNBLOCK {player}/{line}: shadow_pl={shadow_pl:+.2f}u "
                    f">= {STRIKE1_UNBLOCK_PL}, n={shadow_n} >= "
                    f"{STRIKE1_UNBLOCK_MIN_N}"
                )

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
    blocked_repo: BlockedLineV2Repository, player: str, line: str
) -> bool:
    """Quick check chamado pelo alert_engine_v2 antes de notifier.send_alert_v2()."""
    return await blocked_repo.is_suppressed(player, line)


async def _today_pl_per_line(
    blocked_repo: BlockedLineV2Repository,
) -> dict[tuple[str, str], tuple[float, int]]:
    """Retorna {(player, line): (pl_today, n_today)} para alertas v2 hoje BRT."""
    stmt = text(
        """
        SELECT losing_player AS player, best_line AS line,
               COALESCE(SUM(profit_flat), 0.0) AS pl,
               COUNT(*) AS n
        FROM alerts_v2
        WHERE (sent_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/Sao_Paulo')::date
              = (NOW() AT TIME ZONE 'America/Sao_Paulo')::date
          AND profit_flat IS NOT NULL
          AND best_line IS NOT NULL
        GROUP BY losing_player, best_line
        """
    )
    result = await blocked_repo.execute_query(stmt)
    out = {}
    for r in result.all():
        if r.line not in LINES_TRACKED:
            continue
        out[(r.player, r.line)] = (float(r.pl or 0.0), int(r.n or 0))
    return out


async def build_hourly_report(blocked_repo: BlockedLineV2Repository) -> str:
    """Monta relatorio M2 (admin DM only)."""
    line_label = {
        "over15": "O1.5", "over25": "O2.5",
        "over35": "O3.5", "over45": "O4.5",
    }

    all_pl = await _fetch_pl_per_line(blocked_repo)
    today_pl = await _today_pl_per_line(blocked_repo)
    blocked_list = await blocked_repo.list_blocked()

    pl_map = {(r["player"], r["line"]): (r["pl"], r["n"]) for r in all_pl}
    blocked_pairs = {(bl.player, bl.line): bl for bl in blocked_list}

    now_brt = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=3)
    header = (
        f"📊 <b>RELATORIO M2 — {now_brt.strftime('%d/%m %H:%M')} BRT</b>"
    )

    parts: list[str] = [header, ""]

    if not blocked_list:
        parts.append("🟢 Nenhuma linha M2 bloqueada.")
    else:
        parts.append(f"🔒 <b>M2 Bloqueadas ({len(blocked_list)}):</b>")
        parts.append("<pre>")
        for bl in blocked_list:
            tot_pl, tot_n = pl_map.get((bl.player, bl.line), (0.0, 0))
            today_p, today_n = today_pl.get((bl.player, bl.line), (0.0, 0))
            label = line_label.get(bl.line, bl.line)
            tag = "PERM" if bl.state == "PERMANENT" else "SHAD"
            arrow = "↑" if today_p > 0 else ("↓" if today_p < 0 else "=")
            roi = (tot_pl / tot_n * 100.0) if tot_n > 0 else 0.0
            parts.append(
                f"{tag} {bl.player[:12]:<12} {label:<5} "
                f"PL={tot_pl:+6.2f}u({tot_n:>2}) ROI={roi:+6.1f}% "
                f"hoje={today_p:+5.2f}u({today_n}){arrow}"
            )
        parts.append("</pre>")

    parts.append("")

    rows = []
    for (player, line), (pl_total, n_total) in pl_map.items():
        if line not in line_label:
            continue
        pl_t, n_t = today_pl.get((player, line), (0.0, 0))
        rows.append({
            "player": player, "line": line,
            "pl_total": pl_total, "n_total": n_total,
            "pl_today": pl_t, "n_today": n_t,
            "blocked": (player, line) in blocked_pairs,
        })
    rows.sort(key=lambda r: -r["pl_total"])

    if rows:
        parts.append(f"📋 <b>M2 Todas as linhas ({len(rows)}):</b>")
        parts.append("<pre>")
        parts.append(
            f"{'jogador':<13} {'lin':<4} {'PL_total':>11} {'ROI':>7} {'hoje':>9}"
        )
        for r in rows:
            label = line_label.get(r["line"], r["line"])
            if r["n_today"] > 0:
                arrow = "↑" if r["pl_today"] > 0 else ("↓" if r["pl_today"] < 0 else "=")
                today_str = f"{r['pl_today']:+5.2f}u{arrow}"
            else:
                today_str = "    —    "
            mark = "B " if r["blocked"] else "  "
            roi = (r["pl_total"] / r["n_total"] * 100.0) if r["n_total"] > 0 else 0.0
            parts.append(
                f"{mark}{r['player'][:11]:<11} {label:<4} "
                f"{r['pl_total']:+7.2f}u({r['n_total']:>2}) "
                f"{roi:+6.1f}% "
                f"{today_str}"
            )
        parts.append("</pre>")

        sum_today_pl = sum(r["pl_today"] for r in rows if r["n_today"] > 0)
        sum_today_n = sum(r["n_today"] for r in rows)
        if sum_today_n:
            roi = sum_today_pl / sum_today_n * 100
            parts.append("")
            parts.append(
                f"🧮 <b>M2 Hoje:</b> {sum_today_n} alertas, "
                f"{sum_today_pl:+.2f}u, ROI {roi:+.1f}%"
            )
        else:
            parts.append("")
            parts.append("ℹ️ Nenhum alerta M2 validado hoje ainda.")

    return "\n".join(parts)


async def get_status(
    blocked_repo: BlockedLineV2Repository,
) -> list[dict]:
    """Retorna lista detalhada para comando admin."""
    rows = await _fetch_pl_per_line(blocked_repo)
    pl_map = {(r["player"], r["line"]): (r["pl"], r["n"]) for r in rows}

    blocked = await blocked_repo.list_blocked()
    out = []
    for bl in blocked:
        pl_total, n_total = pl_map.get((bl.player, bl.line), (0.0, 0))
        shadow_pl = 0.0
        shadow_n = 0
        if (
            bl.state == "SHADOW"
            and bl.shadow_start_at
            and bl.shadow_start_pl is not None
        ):
            shadow_pl, shadow_n = await _shadow_progress(
                blocked_repo, bl.player, bl.line,
                bl.shadow_start_at, bl.shadow_start_pl,
            )
        out.append({
            "player": bl.player,
            "line": bl.line,
            "state": bl.state,
            "block_count": bl.block_count,
            "pl_total": pl_total,
            "n_total": n_total,
            "shadow_pl": shadow_pl,
            "shadow_n": shadow_n,
            "shadow_start_pl": (
                float(bl.shadow_start_pl)
                if bl.shadow_start_pl is not None else None
            ),
            "shadow_start_at": bl.shadow_start_at,
            "last_block_at": bl.last_block_at,
            "needed_pl": STRIKE1_UNBLOCK_PL,
            "needed_n": STRIKE1_UNBLOCK_MIN_N,
        })
    return out
