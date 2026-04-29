"""Auto-block per (player, line, opponent) M1 — state machine v3 H2H granular.

Decidido com Plinio em 2026-04-29 apos analise de drenadores tier S em H2Hs
especificos (12.43u sangrando em matchups onde o player agregado e positivo,
mas matchup especifico drena).

Mudancas vs v2K (player, line):
- CHAVE: agora (player, line, opponent) — granularidade por matchup
- BLOCK rolling: PL <= -1.5u E n>=0 (basta PL bater, sem amostra minima)
- LINE_CLIFF: PL_dia_h2h <= -2.0u E n_dia_h2h >= 3 (agora por matchup)
- PLAYER_CLIFF: MANTIDO agregado — PL_dia_player <= -2.5u E n>=3
  -> bloqueia TODAS as cells (line, opp) que o player ja jogou
- UNBLOCK: PL >= +1.0u E n>=0 (basta PL bater, libera rapido)
- TIMEOUT: 30 dias sem alertas (mantido)
- PERMANENT: post_unblock_pl <= -2u E post_unblock_n >= 5 (mantido)

Filosofia: cirurgico no matchup. Mantem proteção sistemica via player_cliff.

Apenas M1 (tabela `alerts`). M2 nao usa este modulo.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Sequence

from loguru import logger
from sqlalchemy import and_, func, select

from src.db.models import Alert, BlockedLine, Match
from src.db.repositories import BlockedLineRepository

# CUTOFF: data de deploy do regime atual (deploy 57fced7).
CUTOFF_UTC = datetime(2026, 4, 15, 1, 7, 0)

# v3 H2H granular (2026-04-29) — owner aprovou
ROLLING_WINDOW = 20
STRIKE1_BLOCK_PL = -1.5         # rolling PL threshold
STRIKE1_BLOCK_N = 0             # sem amostra minima (basta PL bater)
LINE_CLIFF_PL = -2.0            # PL dia BRT no matchup
LINE_CLIFF_N = 3                # alertas dia BRT no matchup
PLAYER_CLIFF_PL = -2.5          # PL dia BRT do player (qualquer matchup)
PLAYER_CLIFF_N = 3              # alertas dia BRT do player
STRIKE1_UNBLOCK_PL = 1.0        # shadow PL pra desbloquear
STRIKE1_UNBLOCK_MIN_N = 0       # sem amostra minima
STRIKE2_BLOCK_PL = -2.0         # post_unblock PL
STRIKE2_BLOCK_N = 5             # post_unblock n minimo
TIMEOUT_DAYS = 30

LINES_TRACKED = ("over15", "over25", "over35", "over45")


def _now_naive_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _brt_date(sent_at_utc: datetime):
    return (sent_at_utc - timedelta(hours=3)).date()


async def _fetch_all_alerts(
    blocked_repo: BlockedLineRepository,
) -> list[tuple[str, str, str, datetime, float]]:
    """Retorna [(player, line, opponent, sent_at, profit)] desde CUTOFF.

    Opponent eh o OUTRO jogador no match (nao o losing_player).
    """
    stmt = (
        select(
            Alert.losing_player.label("player"),
            Alert.best_line.label("line"),
            Alert.match_id.label("match_id"),
            Alert.sent_at.label("sent_at"),
            Alert.profit_flat.label("profit"),
            Match.player_home.label("home"),
            Match.player_away.label("away"),
        )
        .join(Match, Alert.match_id == Match.id)
        .where(
            Alert.sent_at >= CUTOFF_UTC,
            Alert.profit_flat.is_not(None),
            Alert.best_line.is_not(None),
        )
        .order_by(Alert.sent_at.asc())
    )
    result = await blocked_repo.execute_query(stmt)
    out = []
    for r in result.all():
        if r.line not in LINES_TRACKED:
            continue
        opp = r.away if r.home == r.player else r.home
        out.append((r.player, r.line, opp or "", r.sent_at, float(r.profit)))
    return out


def _rolling_metrics(alerts) -> tuple[float, int]:
    last = alerts[-ROLLING_WINDOW:]
    return sum(p for _, p in last), len(last)


def _today_brt_pl(alerts, today_brt) -> tuple[float, int]:
    pl, n = 0.0, 0
    for sent_at, profit in alerts:
        if _brt_date(sent_at) == today_brt:
            pl += profit
            n += 1
    return pl, n


def _post_unblock_metrics(alerts, last_unblock_at) -> tuple[float, int]:
    if last_unblock_at is None:
        return 0.0, 0
    pl, n = 0.0, 0
    for sent_at, profit in alerts:
        if sent_at > last_unblock_at:
            pl += profit
            n += 1
    return pl, n


def _shadow_metrics(alerts, shadow_start_at) -> tuple[float, int]:
    if shadow_start_at is None:
        return 0.0, 0
    pl, n = 0.0, 0
    for sent_at, profit in alerts:
        if sent_at > shadow_start_at:
            pl += profit
            n += 1
    return pl, n


async def recompute_all_states(
    blocked_repo: BlockedLineRepository,
) -> dict[str, list[str]]:
    """v3 H2H granular — recomputa state das (player, line, opp) cells.

    Idempotente. Roda no cron a cada 5min.
    """
    rows = await _fetch_all_alerts(blocked_repo)
    transitions: dict[str, list[str]] = {
        "blocked_strike1": [],
        "blocked_strike1_line_cliff": [],
        "blocked_strike1_player_cliff": [],
        "blocked_strike2": [],
        "unblocked": [],
        "timeout_unblocked": [],
        "no_change": [],
    }
    if not rows:
        return transitions

    # Agrupar por (player, line, opp) e tambem identificar matchups vistos do player
    by_cell: dict[tuple[str, str, str], list[tuple[datetime, float]]] = defaultdict(list)
    by_player_cells_seen: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for player, line, opp, sent_at, profit in rows:
        by_cell[(player, line, opp)].append((sent_at, profit))
        by_player_cells_seen[player].add((line, opp))

    # Estado atual de TODAS as blocked_lines
    existing_states: dict[tuple[str, str, str], BlockedLine] = {}
    for bl in await blocked_repo.list_all():
        existing_states[(bl.player, bl.line, bl.opponent)] = bl

    now = _now_naive_utc()
    today_brt = _brt_date(now)

    # Player-level day metrics agregado (todas linhas, todos opps)
    player_day_pl: dict[str, float] = defaultdict(float)
    player_day_n: dict[str, int] = defaultdict(int)
    for (player, line, opp), alerts in by_cell.items():
        for sent_at, profit in alerts:
            if _brt_date(sent_at) == today_brt:
                player_day_pl[player] += profit
                player_day_n[player] += 1

    players_in_cliff: set[str] = {
        p for p in player_day_pl
        if player_day_n[p] >= PLAYER_CLIFF_N and player_day_pl[p] <= PLAYER_CLIFF_PL
    }

    # Iterar cada cell e decidir transicao
    for (player, line, opp), alerts in by_cell.items():
        existing = existing_states.get((player, line, opp))
        state = existing.state if existing else "ACTIVE"
        block_count = existing.block_count if existing else 0
        shadow_start_pl = existing.shadow_start_pl if existing else None
        shadow_start_at = existing.shadow_start_at if existing else None
        last_unblock_at = existing.last_unblock_at if existing else None

        new_state = state
        new_block_count = block_count
        new_shadow_start_pl = shadow_start_pl
        new_shadow_start_at = shadow_start_at
        new_last_unblock_at = last_unblock_at
        last_block_at_set: datetime | None = None

        rolling_pl, rolling_n = _rolling_metrics(alerts)
        line_day_pl, line_day_n = _today_brt_pl(alerts, today_brt)

        if state == "ACTIVE":
            triggered = None
            if block_count == 0:
                # 1) PLAYER CLIFF: agregado, prioritario
                if player in players_in_cliff:
                    triggered = "player_cliff"
                # 2) LINE CLIFF: por matchup (3 alertas dia, -2u)
                elif line_day_n >= LINE_CLIFF_N and line_day_pl <= LINE_CLIFF_PL:
                    triggered = "line_cliff"
                # 3) Rolling: pl <= -1.5u (n>=0, sem minimo)
                elif (rolling_n >= STRIKE1_BLOCK_N
                        and rolling_pl <= STRIKE1_BLOCK_PL):
                    triggered = "rolling"

                if triggered is not None:
                    new_state = "SHADOW"
                    new_block_count = 1
                    new_shadow_start_pl = rolling_pl
                    new_shadow_start_at = now
                    last_block_at_set = now
                    msg_key = f"{player}/{line}/vs.{opp}"
                    msg = (
                        f"BLOCK {triggered.upper()} {msg_key}: "
                        f"rolling_PL={rolling_pl:+.2f}u(n={rolling_n}) "
                        f"day_h2h_PL={line_day_pl:+.2f}u(n={line_day_n}) "
                        f"day_player_PL={player_day_pl.get(player, 0):+.2f}u"
                        f"(n={player_day_n.get(player, 0)})"
                    )
                    logger.warning(msg)
                    if triggered == "rolling":
                        transitions["blocked_strike1"].append(
                            f"{msg_key} PL={rolling_pl:+.2f}u(n={rolling_n})"
                        )
                    elif triggered == "line_cliff":
                        transitions["blocked_strike1_line_cliff"].append(
                            f"{msg_key} day_PL={line_day_pl:+.2f}u(n={line_day_n})"
                        )
                    else:
                        transitions["blocked_strike1_player_cliff"].append(
                            f"{msg_key} via player day_PL="
                            f"{player_day_pl[player]:+.2f}u(n={player_day_n[player]})"
                        )
            elif block_count == 1:
                pu_pl, pu_n = _post_unblock_metrics(alerts, last_unblock_at)
                if pu_n >= STRIKE2_BLOCK_N and pu_pl <= STRIKE2_BLOCK_PL:
                    new_state = "PERMANENT"
                    new_block_count = 2
                    last_block_at_set = now
                    transitions["blocked_strike2"].append(
                        f"{player}/{line}/vs.{opp} post_unblock_PL="
                        f"{pu_pl:+.2f}u(n={pu_n})"
                    )
                    logger.error(
                        f"BLOCK STRIKE2 PERMANENT {player}/{line}/vs.{opp}: "
                        f"post_unblock_PL={pu_pl:+.2f}u(n={pu_n})"
                    )
        elif state == "SHADOW":
            shadow_pl, shadow_n = _shadow_metrics(alerts, shadow_start_at)
            # Timeout
            if shadow_start_at is not None:
                days_locked = (now - shadow_start_at).total_seconds() / 86400
                if days_locked >= TIMEOUT_DAYS and shadow_n == 0:
                    new_state = "ACTIVE"
                    new_last_unblock_at = now
                    transitions["timeout_unblocked"].append(
                        f"{player}/{line}/vs.{opp} sem alertas em {days_locked:.0f}d"
                    )
                    logger.info(
                        f"TIMEOUT UNBLOCK {player}/{line}/vs.{opp}: "
                        f"{days_locked:.0f}d sem alertas"
                    )
            # Unblock criteria
            if (new_state == "SHADOW"
                    and shadow_n >= STRIKE1_UNBLOCK_MIN_N
                    and shadow_pl >= STRIKE1_UNBLOCK_PL):
                new_state = "ACTIVE"
                new_last_unblock_at = now
                transitions["unblocked"].append(
                    f"{player}/{line}/vs.{opp} shadow_PL="
                    f"{shadow_pl:+.2f}u(n={shadow_n})"
                )
                logger.info(
                    f"UNBLOCK {player}/{line}/vs.{opp}: "
                    f"shadow_PL={shadow_pl:+.2f}u(n={shadow_n})"
                )

        if new_state == state and new_block_count == block_count:
            transitions["no_change"].append(f"{player}/{line}/vs.{opp}")
            continue

        await blocked_repo.upsert(
            player=player,
            line=line,
            opponent=opp,
            state=new_state,
            block_count=new_block_count,
            shadow_start_pl=new_shadow_start_pl,
            shadow_start_at=new_shadow_start_at,
            last_block_at=last_block_at_set,
            last_unblock_at=new_last_unblock_at,
        )

    return transitions


async def should_suppress(
    blocked_repo: BlockedLineRepository,
    player: str,
    line: str,
    opponent: str,
) -> bool:
    """Quick check chamado pelo alert_engine antes de notifier.send().

    v3: agora exige opponent. Lookup por (player, line, opp).
    """
    return await blocked_repo.is_suppressed(player, line, opponent)


async def _today_pl_per_cell(
    blocked_repo: BlockedLineRepository,
) -> dict[tuple[str, str, str], tuple[float, int]]:
    """Retorna {(player, line, opp): (pl_today, n_today)} para alertas hoje BRT.
    Usado pelo report admin.
    """
    from sqlalchemy import text
    stmt = text("""
        SELECT a.losing_player AS player, a.best_line AS line,
               CASE WHEN m.player_home = a.losing_player
                    THEN m.player_away ELSE m.player_home END AS opp,
               COALESCE(SUM(a.profit_flat), 0.0) AS pl,
               COUNT(*) AS n
        FROM alerts a
        JOIN matches m ON a.match_id = m.id
        WHERE (a.sent_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/Sao_Paulo')::date
              = (NOW() AT TIME ZONE 'America/Sao_Paulo')::date
          AND a.profit_flat IS NOT NULL
          AND a.best_line IS NOT NULL
        GROUP BY a.losing_player, a.best_line, opp
    """)
    result = await blocked_repo.execute_query(stmt)
    out = {}
    for r in result.all():
        if r.line not in LINES_TRACKED:
            continue
        out[(r.player, r.line, r.opp or "")] = (
            float(r.pl or 0.0), int(r.n or 0)
        )
    return out


async def _fetch_pl_per_cell_aggregated(
    blocked_repo: BlockedLineRepository,
) -> list[dict]:
    """[{player, line, opp, pl, n}] cumulativo desde CUTOFF — usado pelos relatorios."""
    from sqlalchemy import text
    stmt = text("""
        SELECT a.losing_player AS player, a.best_line AS line,
               CASE WHEN m.player_home = a.losing_player
                    THEN m.player_away ELSE m.player_home END AS opp,
               SUM(a.profit_flat) AS pl,
               COUNT(*) AS n
        FROM alerts a
        JOIN matches m ON a.match_id = m.id
        WHERE a.sent_at >= :cutoff
          AND a.profit_flat IS NOT NULL
          AND a.best_line IS NOT NULL
        GROUP BY a.losing_player, a.best_line, opp
    """)
    result = await blocked_repo.execute_query(stmt, {"cutoff": CUTOFF_UTC})
    return [
        {"player": r.player, "line": r.line, "opp": r.opp or "",
         "pl": float(r.pl or 0.0), "n": int(r.n or 0)}
        for r in result.all()
        if r.line in LINES_TRACKED
    ]


async def build_hourly_report(blocked_repo: BlockedLineRepository) -> str:
    """Monta texto HTML para Telegram com:
    - Cells bloqueadas (sempre)
    - Resumo do dia
    """
    line_label = {"over15": "O1.5", "over25": "O2.5",
                  "over35": "O3.5", "over45": "O4.5"}

    blocked_list = await blocked_repo.list_blocked()
    today_pl = await _today_pl_per_cell(blocked_repo)

    now_brt = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=3)
    header = (
        f"📊 <b>RELATORIO DE ACOMPANHAMENTO — "
        f"{now_brt.strftime('%d/%m %H:%M')} BRT</b>"
    )

    parts: list[str] = [header, ""]

    if not blocked_list:
        parts.append("🟢 Nenhum matchup bloqueado.")
    else:
        parts.append(f"🔒 <b>Cells bloqueados ({len(blocked_list)}):</b>")
        parts.append("<pre>")
        # Agrupa por player pra leitura
        by_player_blocked: dict[str, list[BlockedLine]] = defaultdict(list)
        for bl in blocked_list:
            by_player_blocked[bl.player].append(bl)
        for player in sorted(by_player_blocked.keys()):
            for bl in by_player_blocked[player]:
                label = line_label.get(bl.line, bl.line)
                tag = "PERM" if bl.state == "PERMANENT" else "SHAD"
                today_p, today_n = today_pl.get(
                    (bl.player, bl.line, bl.opponent), (0.0, 0)
                )
                today_str = ""
                if today_n > 0:
                    arrow = "↑" if today_p > 0 else ("↓" if today_p < 0 else "=")
                    today_str = f" hoje={today_p:+.2f}u({today_n}){arrow}"
                opp_short = (bl.opponent or "?")[:12]
                parts.append(
                    f"{tag} {bl.player[:11]:<11} {label:<5} vs {opp_short:<12}"
                    f"{today_str}"
                )
        parts.append("</pre>")

    # Resumo do dia (agregado, todos os alertas)
    parts.append("")
    sum_today_pl = sum(p for p, _ in today_pl.values())
    sum_today_n = sum(n for _, n in today_pl.values())
    if sum_today_n:
        roi = sum_today_pl / sum_today_n * 100
        parts.append(
            f"🧮 <b>Hoje:</b> {sum_today_n} alertas, "
            f"{sum_today_pl:+.2f}u, ROI {roi:+.1f}%"
        )
    else:
        parts.append("ℹ️ Nenhum alerta validado hoje ainda.")

    return "\n".join(parts)


async def get_status(
    blocked_repo: BlockedLineRepository,
) -> list[dict]:
    """Retorna lista detalhada para o comando /blocked.

    Cada item tem: player, line, opponent, state, block_count, shadow_pl,
    shadow_n, shadow_start_at, needed_pl, needed_n.
    """
    all_alerts = await _fetch_all_alerts(blocked_repo)
    by_cell: dict[tuple[str, str, str], list[tuple[datetime, float]]] = defaultdict(list)
    for player, line, opp, sent_at, profit in all_alerts:
        by_cell[(player, line, opp)].append((sent_at, profit))

    blocked = await blocked_repo.list_blocked()
    out = []
    for bl in blocked:
        key = (bl.player, bl.line, bl.opponent)
        alerts = by_cell.get(key, [])
        pl_total = sum(p for _, p in alerts)
        n_total = len(alerts)
        shadow_pl, shadow_n = 0.0, 0
        if bl.state == "SHADOW" and bl.shadow_start_at is not None:
            shadow_pl, shadow_n = _shadow_metrics(alerts, bl.shadow_start_at)
        out.append({
            "player": bl.player,
            "line": bl.line,
            "opponent": bl.opponent,
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
