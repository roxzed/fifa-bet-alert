"""Auto-block per (player, line, opponent) M2 — state machine de 2 strikes.

v2 granular (2026-05-04): chave inclui opponent — cada matchup H2H tem
state machine independente, igual ao M1 BlockedLine. Antes era apenas
(player, line) agregado, o que bloqueava todas as oportunidades de um
jogador numa linha mesmo quando só um oponente especifico era ruim.

Regras (mesmas do M1, decididas com Plinio em 2026-04-26):

1. Cada (player, line, opponent) tem estado independente: ACTIVE | SHADOW | PERMANENT.

2. Strike 1 (ACTIVE -> SHADOW):
   - trigger: PL acumulado do matchup desde CUTOFF_UTC <= -1.0u
   - efeito: alertas v2 continuam sendo salvos (com suppressed=TRUE),
     validados normalmente, mas NAO sao enviados ao grupo Telegram M2.

3. Unblock (SHADOW -> ACTIVE):
   - shadow_pl >= +1.0u E shadow_n >= 3
   - shadow_pl = PL desde shadow_start_at para esse matchup
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

CUTOFF_UTC = datetime(2026, 4, 15, 1, 7, 0)
STRIKE1_BLOCK_PL = -1.0
STRIKE1_BLOCK_MIN_N = 2  # 2026-06-10: exigir n>=2 antes de bloquear
STRIKE1_UNBLOCK_PL = +1.0
STRIKE1_UNBLOCK_MIN_N = 3
STRIKE2_BLOCK_PL = -2.0
LINES_TRACKED = ("over15", "over25", "over35", "over45")

# 2026-06-17: Unblock por historico de jogo (caminho 3) — espelho do M1.
# Motivacao: combos em SHADOW sem alertas pos-block ficam presos por inercia.
# And-se M2 (2026-06-17) mostrou: 25/83 combos liberariam, EV +10.39u projetado.
HISTORY_UNBLOCK_MIN_N = 6
HISTORY_UNBLOCK_MIN_HR = 0.70
HISTORY_LINE_THRESH = {"over15": 2, "over25": 3, "over35": 4, "over45": 5}


def _now_naive_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def _fetch_pl_per_matchup(blocked_repo: BlockedLineV2Repository) -> list[dict]:
    """Retorna [{'player', 'line', 'opponent', 'pl', 'n'}] desde CUTOFF_UTC, M2 only.

    Deduplica por (match_id, best_line) via DISTINCT ON. Sem isso, polls
    do OddsMonitor criavam multiplos registros do mesmo jogo, inflando
    o PL e podendo acionar STRIKE1/2 incorretamente.
    """
    stmt = text("""
        SELECT player, line, opponent,
               COALESCE(SUM(pl), 0.0) AS pl,
               COUNT(*) AS n
        FROM (
            SELECT DISTINCT ON (match_id, best_line)
                losing_player AS player,
                best_line AS line,
                COALESCE(opponent_player, '') AS opponent,
                profit_flat AS pl
            FROM alerts_v2
            WHERE sent_at >= :cutoff
              AND profit_flat IS NOT NULL
              AND best_line IS NOT NULL
            ORDER BY match_id, best_line, sent_at ASC
        ) deduped
        GROUP BY player, line, opponent
    """)
    result = await blocked_repo.execute_query(stmt, {"cutoff": CUTOFF_UTC})
    rows = result.all()
    return [
        {
            "player": r.player,
            "line": r.line,
            "opponent": r.opponent or "",
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
    opponent: str,
    shadow_start_at: datetime,
) -> tuple[float, int]:
    """Retorna (shadow_pl, shadow_n) — alertas v2 APOS shadow_start_at para
    esse matchup, deduplicados por match_id (1 entrada por jogo)."""
    stmt = text("""
        SELECT COALESCE(SUM(pl), 0.0) AS pl, COUNT(*) AS n
        FROM (
            SELECT DISTINCT ON (match_id, best_line) profit_flat AS pl
            FROM alerts_v2
            WHERE losing_player = :player
              AND best_line = :line
              AND COALESCE(opponent_player, '') = :opp
              AND sent_at > :start
              AND profit_flat IS NOT NULL
            ORDER BY match_id, best_line, sent_at ASC
        ) deduped
    """)
    result = await blocked_repo.execute_query(stmt, {
        "player": player, "line": line, "opp": opponent or "",
        "start": shadow_start_at,
    })
    row = result.one()
    return float(row.pl or 0.0), int(row.n or 0)


async def _post_unblock_metrics(
    blocked_repo: BlockedLineV2Repository,
    player: str,
    line: str,
    opponent: str,
    last_unblock_at: datetime | None,
) -> tuple[float, int]:
    """Retorna (pl, n) dos alertas v2 APOS last_unblock_at — pra evitar
    reblock loop quando o caminho 3 desbloqueia mas pl_total ainda esta
    negativo de alertas pre-block."""
    if last_unblock_at is None:
        return (0.0, 0)
    stmt = text("""
        SELECT COALESCE(SUM(pl), 0.0) AS pl, COUNT(*) AS n
        FROM (
            SELECT DISTINCT ON (match_id, best_line) profit_flat AS pl
            FROM alerts_v2
            WHERE losing_player = :player
              AND best_line = :line
              AND COALESCE(opponent_player, '') = :opp
              AND sent_at > :last_unblock
              AND profit_flat IS NOT NULL
            ORDER BY match_id, best_line, sent_at ASC
        ) deduped
    """)
    result = await blocked_repo.execute_query(stmt, {
        "player": player, "line": line, "opp": opponent or "",
        "last_unblock": last_unblock_at,
    })
    row = result.one()
    return float(row.pl or 0.0), int(row.n or 0)


async def _check_history_unblock(
    blocked_repo: BlockedLineV2Repository,
    player: str,
    line: str,
    opponent: str,
    shadow_start_at: datetime | None,
) -> tuple[bool, int, float]:
    """Verifica se historico de jogo APOS o block sugere desbloqueio.

    Busca matches G2 onde:
      - is_return_match = TRUE, started_at >= shadow_start_at
      - players sao (player, opponent) em qualquer ordem
      - player perdeu o G1 (via pair_match_id)
      - score conhecido

    Retorna (should_unblock, n_jogos, hit_rate).
    """
    if shadow_start_at is None:
        return (False, 0, 0.0)
    thresh = HISTORY_LINE_THRESH.get(line)
    if thresh is None or not opponent:
        return (False, 0, 0.0)
    stmt = text("""
        SELECT m2.player_home, m2.player_away, m2.score_home, m2.score_away
        FROM matches m2
        JOIN matches m1 ON m2.pair_match_id = m1.id
        WHERE m2.is_return_match = TRUE
          AND m2.started_at >= :start_at
          AND m2.score_home IS NOT NULL AND m2.score_away IS NOT NULL
          AND ((m2.player_home = :p AND m2.player_away = :o)
            OR (m2.player_home = :o AND m2.player_away = :p))
          AND ((m1.player_home = :p AND m1.score_home < m1.score_away)
            OR (m1.player_away = :p AND m1.score_away < m1.score_home))
    """)
    result = await blocked_repo.execute_query(
        stmt, {"start_at": shadow_start_at, "p": player, "o": opponent}
    )
    rows = result.all()
    n = len(rows)
    if n < HISTORY_UNBLOCK_MIN_N:
        return (False, n, 0.0)
    hits = 0
    for r in rows:
        pg = r.score_home if r.player_home == player else r.score_away
        if pg is not None and pg >= thresh:
            hits += 1
    hr = hits / n if n else 0.0
    return (hr >= HISTORY_UNBLOCK_MIN_HR, n, hr)


async def recompute_all_states(
    blocked_repo: BlockedLineV2Repository,
) -> dict[str, list[str]]:
    """Recomputa state de todas as (player, line, opponent) em M2.

    Idempotente. Retorna dict de transicoes feitas.
    """
    rows = await _fetch_pl_per_matchup(blocked_repo)
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
        opponent = r["opponent"]
        pl_total = r["pl"]
        n_total = r["n"]
        existing = await blocked_repo.get(player, line, opponent)
        state = existing.state if existing else "ACTIVE"
        block_count = existing.block_count if existing else 0
        shadow_start_at = existing.shadow_start_at if existing else None
        existing_last_unblock_at = existing.last_unblock_at if existing else None

        new_state = state
        new_block_count = block_count
        new_shadow_start_pl = existing.shadow_start_pl if existing else None
        new_shadow_start_at = shadow_start_at
        last_block_at = None
        last_unblock_at = None

        key = f"{player}/{line}/vs.{opponent}"

        if state == "ACTIVE":
            # 2026-05-18: PERMANENT path removido. Mesma filosofia do M1:
            # combo pode ser bloqueado/desbloqueado multiplas vezes via SHADOW.
            # 2026-06-10: adicionar STRIKE1_BLOCK_MIN_N=2. Antes 1 single RED
            # de -1u bloqueava direto, gerando 65 SHADOWs com n=1 (variancia
            # pura, nao drainer real). Agora exige n>=2 antes de bloquear.
            # 2026-06-17: fix reblock loop apos caminho 3. Se ja desbloqueou
            # antes, so rebloqueia se ha >=1 alerta POS-unblock. Senao
            # pl_total reflete drain pre-block ja punido, e o reblock seria
            # imediato (observado no M1: 17/35 combos rebloquearam em 301s).
            can_block = (
                pl_total <= STRIKE1_BLOCK_PL
                and n_total >= STRIKE1_BLOCK_MIN_N
            )
            if can_block and existing_last_unblock_at is not None:
                post_pl, post_n = await _post_unblock_metrics(
                    blocked_repo, player, line, opponent, existing_last_unblock_at
                )
                if post_n < 1:
                    can_block = False
            if can_block:
                new_state = "SHADOW"
                new_block_count = block_count + 1
                new_shadow_start_pl = pl_total
                new_shadow_start_at = now
                last_block_at = now
                transitions["blocked_strike1"].append(f"{key} PL={pl_total:+.2f}u(n={n_total})")
                logger.warning(
                    f"M2 BLOCK strike1 {key}: PL={pl_total:+.2f}u(n={n_total}) <= "
                    f"{STRIKE1_BLOCK_PL} -> SHADOW (block_count={new_block_count})"
                )
        elif state == "SHADOW":
            # 2026-06-10: liberar SHADOWs antigos que tinham n=1 (regra antiga).
            # Apos introduzir STRIKE1_BLOCK_MIN_N=2, esses combos ficam em estado
            # inconsistente — block n=1 nao seria mais aceito hoje.
            if n_total < STRIKE1_BLOCK_MIN_N:
                new_state = "ACTIVE"
                last_unblock_at = now
                transitions["unblocked"].append(
                    f"{key} pl={pl_total:+.2f}u n={n_total} [n<min, regra nova]"
                )
                logger.info(
                    f"M2 UNBLOCK SAMPLE-FIX {key}: n={n_total} < "
                    f"{STRIKE1_BLOCK_MIN_N} (regra atual nao bloquearia)"
                )
                # Pula resto da logica SHADOW (acabamos de unblock)
                if new_state != state or new_block_count != block_count:
                    await blocked_repo.upsert(
                        player=player, line=line, opponent=opponent,
                        state=new_state, block_count=new_block_count,
                        shadow_start_pl=new_shadow_start_pl,
                        shadow_start_at=new_shadow_start_at,
                        last_block_at=last_block_at,
                        last_unblock_at=last_unblock_at,
                    )
                continue

            if shadow_start_at is None:
                logger.warning(f"M2 SHADOW {key} sem shadow_start — pulando recheck")
                transitions["no_change"].append(f"{key} (corrupted)")
                continue
            shadow_pl, shadow_n = await _shadow_progress(
                blocked_repo, player, line, opponent, shadow_start_at
            )
            if (
                shadow_pl >= STRIKE1_UNBLOCK_PL
                and shadow_n >= STRIKE1_UNBLOCK_MIN_N
            ):
                new_state = "ACTIVE"
                last_unblock_at = now
                transitions["unblocked"].append(
                    f"{key} shadow_pl={shadow_pl:+.2f}u n={shadow_n}"
                )
                logger.info(
                    f"M2 UNBLOCK {key}: shadow_pl={shadow_pl:+.2f}u "
                    f">= {STRIKE1_UNBLOCK_PL}, n={shadow_n} >= {STRIKE1_UNBLOCK_MIN_N}"
                )

            # Unblock simetrico (2026-06-06): se PL total acumulado voltou a +1u,
            # libera. Espelho do block que dispara em pl_total <= -1u. Resolve
            # combos com historico positivo sem atividade pos-block que ficavam
            # presos indefinidamente. Cron re-bloqueia se voltar a -1u.
            if new_state == "SHADOW" and pl_total >= STRIKE1_UNBLOCK_PL:
                new_state = "ACTIVE"
                last_unblock_at = now
                transitions["unblocked"].append(
                    f"{key} pl_total={pl_total:+.2f}u [simetrico]"
                )
                logger.info(
                    f"M2 UNBLOCK SIMETRICO {key}: "
                    f"pl_total={pl_total:+.2f}u >= {STRIKE1_UNBLOCK_PL}"
                )

            # Caminho 3 (2026-06-17): unblock por historico de jogo.
            # Espelho do M1: se desde o block o player jogou >=6 G2 contra o
            # opponent e bateu a linha em >=70%, libera. Resolve inercia de
            # combos sem atividade pos-block. And-se M2 mostrou 25/83 combos
            # liberariam, EV/u projetado +10.39u.
            if new_state == "SHADOW":
                try:
                    should_unblock, n_games, hr = await _check_history_unblock(
                        blocked_repo, player, line, opponent, shadow_start_at
                    )
                except Exception as e:
                    logger.warning(
                        f"M2 history unblock check falhou {key}: {e!r}"
                    )
                    should_unblock = False
                    n_games, hr = 0, 0.0
                if should_unblock:
                    new_state = "ACTIVE"
                    last_unblock_at = now
                    transitions["unblocked"].append(
                        f"{key} historico="
                        f"{int(hr*n_games)}/{n_games}({hr*100:.1f}%) [historico]"
                    )
                    logger.info(
                        f"M2 UNBLOCK HISTORICO {key}: "
                        f"{int(hr*n_games)}/{n_games} jogos pos-block "
                        f"({hr*100:.1f}%) bateram a linha"
                    )

        if new_state == state and new_block_count == block_count:
            transitions["no_change"].append(key)
            continue

        await blocked_repo.upsert(
            player=player,
            line=line,
            opponent=opponent,
            state=new_state,
            block_count=new_block_count,
            shadow_start_pl=new_shadow_start_pl,
            shadow_start_at=new_shadow_start_at,
            last_block_at=last_block_at,
            last_unblock_at=last_unblock_at,
        )

    return transitions


async def should_suppress(
    blocked_repo: BlockedLineV2Repository, player: str, line: str, opponent: str = ""
) -> bool:
    """Quick check chamado pelo alert_engine_v2 antes de notifier.send_alert_v2()."""
    return await blocked_repo.is_suppressed(player, line, opponent)


async def _today_pl_per_matchup(
    blocked_repo: BlockedLineV2Repository,
) -> dict[tuple[str, str, str], tuple[float, int]]:
    """Retorna {(player, line, opponent): (pl_today, n_today)} para alertas v2 hoje BRT."""
    stmt = text(
        """
        SELECT losing_player AS player, best_line AS line,
               COALESCE(opponent_player, '') AS opponent,
               COALESCE(SUM(profit_flat), 0.0) AS pl,
               COUNT(*) AS n
        FROM alerts_v2
        WHERE (sent_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/Sao_Paulo')::date
              = (NOW() AT TIME ZONE 'America/Sao_Paulo')::date
          AND profit_flat IS NOT NULL
          AND best_line IS NOT NULL
        GROUP BY losing_player, best_line, opponent_player
        """
    )
    result = await blocked_repo.execute_query(stmt)
    out = {}
    for r in result.all():
        if r.line not in LINES_TRACKED:
            continue
        out[(r.player, r.line, r.opponent)] = (float(r.pl or 0.0), int(r.n or 0))
    return out


async def build_hourly_report(blocked_repo: BlockedLineV2Repository) -> str:
    """Monta relatorio M2 (admin DM only)."""
    line_label = {
        "over15": "O1.5", "over25": "O2.5",
        "over35": "O3.5", "over45": "O4.5",
    }

    all_pl = await _fetch_pl_per_matchup(blocked_repo)
    today_pl = await _today_pl_per_matchup(blocked_repo)
    blocked_list = await blocked_repo.list_blocked()

    pl_map = {(r["player"], r["line"], r["opponent"]): (r["pl"], r["n"]) for r in all_pl}
    blocked_keys = {(bl.player, bl.line, bl.opponent): bl for bl in blocked_list}

    now_brt = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=3)
    header = f"📊 <b>RELATORIO M2 — {now_brt.strftime('%d/%m %H:%M')} BRT</b>"

    parts: list[str] = [header, ""]

    if not blocked_list:
        parts.append("🟢 Nenhuma linha M2 bloqueada.")
    else:
        parts.append(f"🔒 <b>M2 Bloqueadas ({len(blocked_list)}):</b>")
        parts.append("<pre>")
        for bl in blocked_list:
            opp = bl.opponent or "?"
            tot_pl, tot_n = pl_map.get((bl.player, bl.line, bl.opponent), (0.0, 0))
            today_p, today_n = today_pl.get((bl.player, bl.line, bl.opponent), (0.0, 0))
            label = line_label.get(bl.line, bl.line)
            tag = "PERM" if bl.state == "PERMANENT" else "SHAD"
            arrow = "↑" if today_p > 0 else ("↓" if today_p < 0 else "=")
            roi = (tot_pl / tot_n * 100.0) if tot_n > 0 else 0.0
            parts.append(
                f"{tag} {bl.player[:10]:<10} {label:<5} vs {opp[:10]:<10} "
                f"PL={tot_pl:+6.2f}u({tot_n:>2}) ROI={roi:+6.1f}% "
                f"hoje={today_p:+5.2f}u({today_n}){arrow}"
            )
        parts.append("</pre>")

    parts.append("")

    rows = []
    for (player, line, opponent), (pl_total, n_total) in pl_map.items():
        if line not in line_label:
            continue
        pl_t, n_t = today_pl.get((player, line, opponent), (0.0, 0))
        rows.append({
            "player": player, "line": line, "opponent": opponent,
            "pl_total": pl_total, "n_total": n_total,
            "pl_today": pl_t, "n_today": n_t,
            "blocked": (player, line, opponent) in blocked_keys,
        })
    rows.sort(key=lambda r: -r["pl_total"])

    if rows:
        parts.append(f"📋 <b>M2 Todos os matchups ({len(rows)}):</b>")
        parts.append("<pre>")
        parts.append(
            f"{'jogador':<12} {'lin':<4} {'vs':<12} {'PL_total':>11} {'ROI':>7} {'hoje':>9}"
        )
        for r in rows:
            label = line_label.get(r["line"], r["line"])
            opp = (r["opponent"] or "?")[:11]
            if r["n_today"] > 0:
                arrow = "↑" if r["pl_today"] > 0 else ("↓" if r["pl_today"] < 0 else "=")
                today_str = f"{r['pl_today']:+5.2f}u{arrow}"
            else:
                today_str = "    —    "
            mark = "B " if r["blocked"] else "  "
            roi = (r["pl_total"] / r["n_total"] * 100.0) if r["n_total"] > 0 else 0.0
            parts.append(
                f"{mark}{r['player'][:10]:<10} {label:<4} vs {opp:<11} "
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
    rows = await _fetch_pl_per_matchup(blocked_repo)
    pl_map = {(r["player"], r["line"], r["opponent"]): (r["pl"], r["n"]) for r in rows}

    blocked = await blocked_repo.list_blocked()
    out = []
    for bl in blocked:
        pl_total, n_total = pl_map.get((bl.player, bl.line, bl.opponent), (0.0, 0))
        shadow_pl = 0.0
        shadow_n = 0
        if bl.state == "SHADOW" and bl.shadow_start_at:
            shadow_pl, shadow_n = await _shadow_progress(
                blocked_repo, bl.player, bl.line, bl.opponent, bl.shadow_start_at,
            )
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
                float(bl.shadow_start_pl) if bl.shadow_start_pl is not None else None
            ),
            "shadow_start_at": bl.shadow_start_at,
            "last_block_at": bl.last_block_at,
            "needed_pl": STRIKE1_UNBLOCK_PL,
            "needed_n": STRIKE1_UNBLOCK_MIN_N,
        })
    return out
