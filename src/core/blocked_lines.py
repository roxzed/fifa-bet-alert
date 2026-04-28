"""Auto-block per (player, line) M1 — state machine v2 (lapidado 2026-04-28).

Decidido com Plinio em 2026-04-28 apos lapidacao completa via simulacao A/B
contrafactual em 432 alertas reais (CUTOFF -> 28/04). Variante final:
v2K-pl1n5. Ganho esperado: +12u/14d (~+25u/mes) vs v1.

Mudancas vs v1:
- Janela rolling 20 (em vez de cumulativo desde CUTOFF)
- Sample minimo no bloqueio: n>=4 default, n>=3 high-risk
- Threshold mais conservador no bloqueio: -1.5u default
- LINE CLIFF (NOVO): 3+ reds e -2u em 1 dia BRT na mesma linha
- PLAYER CLIFF (NOVO): 3+ reds e -2.5u em 1 dia BRT no player inteiro
  -> bloqueia TODAS as linhas em que o player ja teve alerta
- Unblock uniforme: pl>=+1u E n>=5 (mais qualidade do que v1 com n=3)
- Timeout: 30 dias em SHADOW sem alertas reseta pra ACTIVE
- PERMANENT exige amostra: post_unblock_n>=5 E post_unblock_pl<=-2u
  (post_unblock = alertas APOS o ultimo unblock)

Filosofia: bloquear facil + cirurgico, unblock dificil + qualidade.

Janela:
- "rolling 20" = ultimos 20 alertas validados da (player, line) desde CUTOFF
- "dia BRT" = (sent_at AT TIME ZONE UTC AT TIME ZONE America/Sao_Paulo)::date
              == NOW BRT::date

Apenas M1 (tabela `alerts`). M2 nao usa este modulo.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Sequence

from loguru import logger
from sqlalchemy import and_, func, select

from src.db.models import Alert, BlockedLine
from src.db.repositories import BlockedLineRepository

# CUTOFF: data de deploy do regime atual (deploy 57fced7).
CUTOFF_UTC = datetime(2026, 4, 15, 1, 7, 0)

# v2K-pl1n5 (lapidado 2026-04-28)
ROLLING_WINDOW = 20
STRIKE1_BLOCK_PL = -1.5         # default
STRIKE1_BLOCK_N = 4             # default
STRIKE1_BLOCK_PL_HR = -1.0      # high-risk
STRIKE1_BLOCK_N_HR = 3          # high-risk
LINE_CLIFF_PL = -2.0            # PL na linha no dia BRT
LINE_CLIFF_N = 3                # alertas na linha no dia BRT
PLAYER_CLIFF_PL = -2.5          # PL do player (qualquer linha) no dia BRT
PLAYER_CLIFF_N = 3              # alertas do player no dia BRT
STRIKE1_UNBLOCK_PL = 1.0        # uniforme
STRIKE1_UNBLOCK_MIN_N = 5       # uniforme (em vez de 3 do v1)
STRIKE2_BLOCK_PL = -2.0         # post_unblock
STRIKE2_BLOCK_N = 5             # post_unblock (em vez de cumulativo do v1)
TIMEOUT_DAYS = 30

LINES_TRACKED = ("over15", "over25", "over35", "over45")


def _now_naive_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _brt_date(sent_at_utc: datetime):
    """Converte sent_at UTC pra date BRT (UTC-3, sem DST em America/Sao_Paulo)."""
    return (sent_at_utc - timedelta(hours=3)).date()


async def _fetch_all_alerts(
    blocked_repo: BlockedLineRepository,
) -> list[tuple[str, str, datetime, float]]:
    """Retorna [(player, line, sent_at, profit)] desde CUTOFF, ordenado por sent_at."""
    stmt = (
        select(
            Alert.losing_player.label("player"),
            Alert.best_line.label("line"),
            Alert.sent_at.label("sent_at"),
            Alert.profit_flat.label("profit"),
        )
        .where(
            Alert.sent_at >= CUTOFF_UTC,
            Alert.profit_flat.is_not(None),
            Alert.best_line.is_not(None),
        )
        .order_by(Alert.sent_at.asc())
    )
    result = await blocked_repo.execute_query(stmt)
    return [
        (r.player, r.line, r.sent_at, float(r.profit))
        for r in result.all()
        if r.line in LINES_TRACKED
    ]


def _rolling_metrics(
    alerts_for_line: list[tuple[datetime, float]],
) -> tuple[float, int]:
    """PL e n dos ultimos ROLLING_WINDOW alertas."""
    last = alerts_for_line[-ROLLING_WINDOW:]
    return sum(p for _, p in last), len(last)


def _today_brt_pl(
    alerts_for_line: list[tuple[datetime, float]], today_brt
) -> tuple[float, int]:
    """PL e n dos alertas do dia BRT atual."""
    pl, n = 0.0, 0
    for sent_at, profit in alerts_for_line:
        if _brt_date(sent_at) == today_brt:
            pl += profit
            n += 1
    return pl, n


def _post_unblock_metrics(
    alerts_for_line: list[tuple[datetime, float]],
    last_unblock_at: datetime | None,
) -> tuple[float, int]:
    """PL e n dos alertas APOS o ultimo unblock."""
    if last_unblock_at is None:
        return 0.0, 0
    pl, n = 0.0, 0
    for sent_at, profit in alerts_for_line:
        if sent_at > last_unblock_at:
            pl += profit
            n += 1
    return pl, n


def _shadow_metrics(
    alerts_for_line: list[tuple[datetime, float]],
    shadow_start_at: datetime | None,
) -> tuple[float, int]:
    """PL e n dos alertas em SHADOW (sent_at > shadow_start_at)."""
    if shadow_start_at is None:
        return 0.0, 0
    pl, n = 0.0, 0
    for sent_at, profit in alerts_for_line:
        if sent_at > shadow_start_at:
            pl += profit
            n += 1
    return pl, n


async def recompute_all_states(
    blocked_repo: BlockedLineRepository,
) -> dict[str, list[str]]:
    """v2K-pl1n5 — recomputa state das (player, line) sob nova politica.

    Idempotente. Roda no cron a cada 5min.
    Retorna dict de transicoes feitas:
    - blocked_strike1 (rolling)
    - blocked_strike1_line_cliff
    - blocked_strike1_player_cliff
    - blocked_strike2 (PERMANENT)
    - unblocked
    - timeout_unblocked
    - no_change
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

    # Agrupar por (player, line) e por player
    by_line: dict[tuple[str, str], list[tuple[datetime, float]]] = defaultdict(list)
    by_player_lines_seen: dict[str, set[str]] = defaultdict(set)
    for player, line, sent_at, profit in rows:
        by_line[(player, line)].append((sent_at, profit))
        by_player_lines_seen[player].add(line)

    # Estado atual de TODAS as blocked_lines
    existing_states: dict[tuple[str, str], BlockedLine] = {}
    for bl in await blocked_repo.list_all():
        existing_states[(bl.player, bl.line)] = bl

    now = _now_naive_utc()
    today_brt = _brt_date(now)

    # ---- Player-level day metrics (pra player cliff) ----
    player_day_pl: dict[str, float] = defaultdict(float)
    player_day_n: dict[str, int] = defaultdict(int)
    for (player, line), alerts in by_line.items():
        for sent_at, profit in alerts:
            if _brt_date(sent_at) == today_brt:
                player_day_pl[player] += profit
                player_day_n[player] += 1

    # Players cuja day-soma cumpre player cliff (independente de line)
    players_in_cliff: set[str] = {
        p for p in player_day_pl
        if player_day_n[p] >= PLAYER_CLIFF_N and player_day_pl[p] <= PLAYER_CLIFF_PL
    }

    # Lazy import pra evitar ciclo
    from src.core.stats_engine import StatsEngine
    HIGH_RISK = getattr(StatsEngine, "HIGH_RISK_PLAYERS", set())

    # ---- Iterar (player, line) e decidir transicao ----
    for (player, line), alerts in by_line.items():
        existing = existing_states.get((player, line))
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

        is_hr = player in HIGH_RISK
        block_pl_th = STRIKE1_BLOCK_PL_HR if is_hr else STRIKE1_BLOCK_PL
        block_n_th = STRIKE1_BLOCK_N_HR if is_hr else STRIKE1_BLOCK_N

        rolling_pl, rolling_n = _rolling_metrics(alerts)
        line_day_pl, line_day_n = _today_brt_pl(alerts, today_brt)

        if state == "ACTIVE":
            triggered = None
            if block_count == 0:
                # 1) PLAYER CLIFF: prioritario, manda todas linhas seen pra SHADOW
                if player in players_in_cliff:
                    triggered = "player_cliff"
                # 2) LINE CLIFF
                elif line_day_n >= LINE_CLIFF_N and line_day_pl <= LINE_CLIFF_PL:
                    triggered = "line_cliff"
                # 3) Rolling normal
                elif rolling_n >= block_n_th and rolling_pl <= block_pl_th:
                    triggered = "rolling"

                if triggered is not None:
                    new_state = "SHADOW"
                    new_block_count = 1
                    new_shadow_start_pl = rolling_pl
                    new_shadow_start_at = now
                    last_block_at_set = now
                    msg = (
                        f"BLOCK {triggered.upper()} {player}/{line}: "
                        f"rolling_PL={rolling_pl:+.2f}u(n={rolling_n}) "
                        f"day_line_PL={line_day_pl:+.2f}u(n={line_day_n}) "
                        f"day_player_PL={player_day_pl.get(player, 0):+.2f}u(n={player_day_n.get(player, 0)})"
                    )
                    logger.warning(msg)
                    if triggered == "rolling":
                        transitions["blocked_strike1"].append(
                            f"{player}/{line} PL={rolling_pl:+.2f}u(n={rolling_n})"
                        )
                    elif triggered == "line_cliff":
                        transitions["blocked_strike1_line_cliff"].append(
                            f"{player}/{line} day_PL={line_day_pl:+.2f}u(n={line_day_n})"
                        )
                    else:  # player_cliff
                        transitions["blocked_strike1_player_cliff"].append(
                            f"{player}/{line} via player day_PL={player_day_pl[player]:+.2f}u(n={player_day_n[player]})"
                        )
            elif block_count == 1:
                # Strike 2 -> PERMANENT (pos-unblock metrics)
                pu_pl, pu_n = _post_unblock_metrics(alerts, last_unblock_at)
                if pu_n >= STRIKE2_BLOCK_N and pu_pl <= STRIKE2_BLOCK_PL:
                    new_state = "PERMANENT"
                    new_block_count = 2
                    last_block_at_set = now
                    transitions["blocked_strike2"].append(
                        f"{player}/{line} post_unblock_PL={pu_pl:+.2f}u(n={pu_n})"
                    )
                    logger.error(
                        f"BLOCK STRIKE2 PERMANENT {player}/{line}: "
                        f"post_unblock_PL={pu_pl:+.2f}u(n={pu_n}) "
                        f"<= {STRIKE2_BLOCK_PL}u (n>={STRIKE2_BLOCK_N})"
                    )
        elif state == "SHADOW":
            shadow_pl, shadow_n = _shadow_metrics(alerts, shadow_start_at)
            # Timeout: 30 dias em SHADOW sem alertas
            if shadow_start_at is not None:
                days_locked = (now - shadow_start_at).total_seconds() / 86400
                if days_locked >= TIMEOUT_DAYS and shadow_n == 0:
                    new_state = "ACTIVE"
                    new_last_unblock_at = now
                    transitions["timeout_unblocked"].append(
                        f"{player}/{line} sem alertas em {days_locked:.0f}d"
                    )
                    logger.info(
                        f"TIMEOUT UNBLOCK {player}/{line}: {days_locked:.0f}d sem alertas"
                    )
            # Unblock criteria
            if (new_state == "SHADOW"
                    and shadow_pl >= STRIKE1_UNBLOCK_PL
                    and shadow_n >= STRIKE1_UNBLOCK_MIN_N):
                new_state = "ACTIVE"
                new_last_unblock_at = now
                transitions["unblocked"].append(
                    f"{player}/{line} shadow_PL={shadow_pl:+.2f}u(n={shadow_n})"
                )
                logger.info(
                    f"UNBLOCK {player}/{line}: shadow_PL={shadow_pl:+.2f}u(n={shadow_n}) "
                    f">= {STRIKE1_UNBLOCK_PL}u (n>={STRIKE1_UNBLOCK_MIN_N})"
                )
        # PERMANENT: sem transicao automatica

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
            last_block_at=last_block_at_set,
            last_unblock_at=new_last_unblock_at,
        )

    return transitions


async def should_suppress(
    blocked_repo: BlockedLineRepository, player: str, line: str
) -> bool:
    """Quick check chamado pelo alert_engine antes de notifier.send().

    Lookup unico no banco. Se state in (SHADOW, PERMANENT) -> True.
    """
    return await blocked_repo.is_suppressed(player, line)


async def _today_pl_per_line(
    blocked_repo: BlockedLineRepository,
) -> dict[tuple[str, str], tuple[float, int]]:
    """Retorna {(player, line): (pl_today, n_today)} para alertas hoje BRT.
    Usado pelo report admin (mantido como antes).
    """
    from sqlalchemy import text
    stmt = text("""
        SELECT losing_player AS player, best_line AS line,
               COALESCE(SUM(profit_flat), 0.0) AS pl,
               COUNT(*) AS n
        FROM alerts
        WHERE (sent_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/Sao_Paulo')::date
              = (NOW() AT TIME ZONE 'America/Sao_Paulo')::date
          AND profit_flat IS NOT NULL
          AND best_line IS NOT NULL
        GROUP BY losing_player, best_line
    """)
    result = await blocked_repo.execute_query(stmt)
    out = {}
    for r in result.all():
        if r.line not in LINES_TRACKED:
            continue
        out[(r.player, r.line)] = (float(r.pl or 0.0), int(r.n or 0))
    return out


async def _fetch_pl_per_line_aggregated(
    blocked_repo: BlockedLineRepository,
) -> list[dict]:
    """[{player, line, pl, n}] cumulativo desde CUTOFF — usado pelos relatorios."""
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
    return [
        {"player": r.player, "line": r.line, "pl": float(r.pl or 0.0), "n": int(r.n or 0)}
        for r in result.all()
        if r.line in LINES_TRACKED
    ]


async def build_hourly_report(blocked_repo: BlockedLineRepository) -> str:
    """Monta texto HTML para Telegram com:
    - Linhas bloqueadas (sempre)
    - Tabela completa de TODAS (jogador, linha) com PL_total + hoje (se houve)
    - Resumo do dia
    """
    line_label = {"over15": "O1.5", "over25": "O2.5",
                  "over35": "O3.5", "over45": "O4.5"}

    all_pl = await _fetch_pl_per_line_aggregated(blocked_repo)
    today_pl = await _today_pl_per_line(blocked_repo)
    blocked_list = await blocked_repo.list_blocked()

    pl_map = {(r["player"], r["line"]): (r["pl"], r["n"]) for r in all_pl}
    blocked_pairs = {(bl.player, bl.line): bl for bl in blocked_list}

    now_brt = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=3)
    header = f"📊 <b>RELATORIO DE ACOMPANHAMENTO — {now_brt.strftime('%d/%m %H:%M')} BRT</b>"

    parts: list[str] = [header, ""]

    if not blocked_list:
        parts.append("🟢 Nenhuma linha bloqueada.")
    else:
        parts.append(f"🔒 <b>Bloqueadas ({len(blocked_list)}):</b>")
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

    from src.core.stats_engine import StatsEngine
    static_full_blacklist = StatsEngine.PLAYER_BLACKLIST
    static_conditional = StatsEngine.PLAYER_CONDITIONAL_BLACKLIST
    static_swap_to_o15 = StatsEngine.PLAYER_SWAP_TO_OVER15

    def _is_blocked(player: str, line: str) -> bool:
        if (player, line) in blocked_pairs:
            return True
        if player in static_full_blacklist:
            return True
        cond = static_conditional.get(player, {})
        if line in cond.get("block_lines", set()):
            return True
        if player in static_swap_to_o15 and line != "over15":
            return True
        return False

    rows = []
    for (player, line), (pl_total, n_total) in pl_map.items():
        if line not in line_label:
            continue
        pl_t, n_t = today_pl.get((player, line), (0.0, 0))
        rows.append({
            "player": player, "line": line,
            "pl_total": pl_total, "n_total": n_total,
            "pl_today": pl_t, "n_today": n_t,
            "blocked": _is_blocked(player, line),
        })
    rows.sort(key=lambda r: -r["pl_total"])

    if rows:
        parts.append(f"📋 <b>Todas as linhas ({len(rows)}):</b>")
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
                f"🧮 <b>Hoje:</b> {sum_today_n} alertas, "
                f"{sum_today_pl:+.2f}u, ROI {roi:+.1f}%"
            )
        else:
            parts.append("")
            parts.append("ℹ️ Nenhum alerta validado hoje ainda.")

    return "\n".join(parts)


async def get_status(
    blocked_repo: BlockedLineRepository,
) -> list[dict]:
    """Retorna lista detalhada para o comando /blocked.

    Cada item tem: player, line, state, block_count, pl_total, shadow_pl,
    shadow_n, shadow_start_pl, shadow_start_at, needed_for_unblock.
    """
    rows = await _fetch_pl_per_line_aggregated(blocked_repo)
    pl_map = {(r["player"], r["line"]): (r["pl"], r["n"]) for r in rows}

    # Pegar alertas pra computar shadow_pl/shadow_n
    all_alerts = await _fetch_all_alerts(blocked_repo)
    by_line: dict[tuple[str, str], list[tuple[datetime, float]]] = defaultdict(list)
    for player, line, sent_at, profit in all_alerts:
        by_line[(player, line)].append((sent_at, profit))

    blocked = await blocked_repo.list_blocked()
    out = []
    for bl in blocked:
        pl_total, n_total = pl_map.get((bl.player, bl.line), (0.0, 0))
        shadow_pl, shadow_n = 0.0, 0
        if bl.state == "SHADOW" and bl.shadow_start_at is not None:
            alerts = by_line.get((bl.player, bl.line), [])
            shadow_pl, shadow_n = _shadow_metrics(alerts, bl.shadow_start_at)
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
