"""Dry-run do watch predictor — usa alertas reais dos ultimos N dias e
verifica quais teriam disparado um watch (predict_watch_candidate),
qual linha/odd alvo, e a cobertura vs alertas reais.

NAO envia nada para o Telegram. NAO salva nada no DB.
NAO toca o agendador asyncio do watch.

Uso: python scripts/test_watch_predictor.py [days=7]
"""
from __future__ import annotations

import asyncio
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from src.core.stats_engine import StatsEngine
from src.db.database import async_session_factory, init_db
from src.db.models import Alert, Match
from src.db.repositories import (
    AlertRepository,
    MatchRepository,
    MethodStatsRepository,
    PlayerRepository,
    TeamStatsRepository,
)


async def run(days: int) -> None:
    await init_db()
    sf = async_session_factory

    stats_engine = StatsEngine(
        match_repo=MatchRepository(sf),
        player_repo=PlayerRepository(sf),
        alert_repo=AlertRepository(sf),
        method_stats_repo=MethodStatsRepository(sf),
        team_stats_repo=TeamStatsRepository(sf),
    )

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_naive = cutoff.replace(tzinfo=None)

    async with sf() as s:
        rows = (await s.execute(
            select(Alert, Match).join(Match, Alert.match_id == Match.id)
            .where(Alert.sent_at >= cutoff_naive)
            .order_by(Alert.sent_at.desc())
        )).all()

    # Limit to 20 most recent for speed (override via arg2)
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    rows = rows[:limit]
    print(f"\n=== Analisando {len(rows)} alertas (limit={limit}) dos ultimos {days}d ===\n", flush=True)

    matched = 0
    unmatched = 0
    line_match = 0
    line_mismatch = 0
    line_dist: Counter[str] = Counter()
    by_line: Counter[str] = Counter()
    examples: list[str] = []

    for idx, (alert, return_match) in enumerate(rows, 1):
        print(f"  [{idx}/{len(rows)}] {alert.losing_player} (alert#{alert.id})", flush=True)
        # game1_match: pair_match_id do return_match aponta para o G1
        if not return_match.pair_match_id:
            continue
        async with sf() as s:
            game1 = await s.get(Match, return_match.pair_match_id)
        if not game1:
            continue

        # loser_goals_g1
        if game1.player_home == alert.losing_player:
            loser_goals_g1 = game1.score_home or 0
        else:
            loser_goals_g1 = game1.score_away or 0

        loser_was_home_g1 = (game1.player_home == alert.losing_player)
        winner = game1.player_away if loser_was_home_g1 else game1.player_home

        try:
            cand = await stats_engine.predict_watch_candidate(
                return_match=return_match,
                game1_match=game1,
                losing_player=alert.losing_player,
                opponent_player=winner,
                loser_goals_g1=loser_goals_g1,
                loser_was_home_g1=loser_was_home_g1,
            )
        except Exception as e:
            print(f"  ERR alerta {alert.id} ({alert.losing_player}): {e}")
            continue

        actual_line = alert.best_line or "?"
        by_line[actual_line] += 1

        if cand is None:
            unmatched += 1
            if len(examples) < 8:
                examples.append(
                    f"  MISS  alert#{alert.id} {alert.losing_player:14s} "
                    f"linha_real={actual_line} (n=0 candidatos)"
                )
            continue

        matched += 1
        line_dist[cand["line"]] += 1
        if cand["line"] == actual_line:
            line_match += 1
        else:
            line_mismatch += 1
            if len(examples) < 8:
                examples.append(
                    f"  DIFF  alert#{alert.id} {alert.losing_player:14s} "
                    f"watch={cand['line']}@{cand['target_odds']:.2f} "
                    f"vs alert={actual_line} tp_pred={cand['predicted_tp']:.0%}"
                )

    total = matched + unmatched
    cov = (matched / total * 100) if total else 0.0
    line_acc = (line_match / matched * 100) if matched else 0.0
    print(f"Cobertura: {matched}/{total} alertas teriam watch ({cov:.1f}%)")
    print(f"Linha correta: {line_match}/{matched} ({line_acc:.1f}%)")
    print(f"\nDistribuicao watch por linha: {dict(line_dist)}")
    print(f"Distribuicao alerta_real por linha: {dict(by_line)}")

    if examples:
        print("\nExemplos:")
        for e in examples:
            print(e)

    print(f"\n=== FIM ===")


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 7
    asyncio.run(run(days))
