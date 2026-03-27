"""Backfill team_stats, matchup_stats e player_team_preferences a partir de Match historicos.

Agrega tudo em memória primeiro e faz upserts em batch (rápido com DB remoto).

Uso: PYTHONPATH=. python scripts/backfill_team_stats.py
"""
import asyncio
from collections import defaultdict
from datetime import datetime, timezone


async def main():
    from src.db.database import async_session_factory, init_db
    from src.db.models import Match, TeamStats, MatchupStats, PlayerTeamPreference
    from sqlalchemy import select, update

    await init_db()
    sf = async_session_factory

    # 1. Buscar todos os jogos encerrados com times
    async with sf() as session:
        stmt = (
            select(
                Match.player_home, Match.player_away,
                Match.team_home, Match.team_away,
                Match.score_home, Match.score_away,
            )
            .where(
                Match.status == "ended",
                Match.score_home.is_not(None),
                Match.score_away.is_not(None),
                Match.team_home.is_not(None),
                Match.team_away.is_not(None),
            )
            .order_by(Match.started_at.asc())
        )
        result = await session.execute(stmt)
        rows = result.all()

    print(f"Total de jogos com times: {len(rows)}")

    # 2. Agregar em memória
    team_agg = defaultdict(lambda: {
        "total_games": 0, "goals_scored": 0, "goals_conceded": 0, "over25_count": 0,
    })
    matchup_agg = defaultdict(lambda: {
        "total_games": 0, "total_goals": 0, "over25_count": 0,
    })
    pref_agg = defaultdict(lambda: {
        "times_used": 0, "goals_scored": 0,
    })

    for ph, pa, th, ta, sh, sa in rows:
        # Team stats
        team_agg[th]["total_games"] += 1
        team_agg[th]["goals_scored"] += sh
        team_agg[th]["goals_conceded"] += sa
        if sh + sa > 2:
            team_agg[th]["over25_count"] += 1

        team_agg[ta]["total_games"] += 1
        team_agg[ta]["goals_scored"] += sa
        team_agg[ta]["goals_conceded"] += sh
        if sh + sa > 2:
            team_agg[ta]["over25_count"] += 1

        # Matchup stats
        key = tuple(sorted([th, ta]))
        matchup_agg[key]["total_games"] += 1
        matchup_agg[key]["total_goals"] += sh + sa
        if sh + sa > 2:
            matchup_agg[key]["over25_count"] += 1

        # Player-team preferences
        pref_agg[(ph, th)]["times_used"] += 1
        pref_agg[(ph, th)]["goals_scored"] += sh
        pref_agg[(pa, ta)]["times_used"] += 1
        pref_agg[(pa, ta)]["goals_scored"] += sa

    print(f"Times únicos: {len(team_agg)}")
    print(f"Matchups únicos: {len(matchup_agg)}")
    print(f"Combos jogador+time: {len(pref_agg)}")

    # 3. Upsert team_stats
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    async with sf() as session:
        for team_name, s in team_agg.items():
            n = s["total_games"]
            stmt = select(TeamStats).where(TeamStats.team_name == team_name)
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                row = TeamStats(team_name=team_name)
                session.add(row)
            row.total_games = n
            row.total_goals_scored = s["goals_scored"]
            row.total_goals_conceded = s["goals_conceded"]
            row.avg_goals_scored = s["goals_scored"] / n
            row.avg_goals_conceded = s["goals_conceded"] / n
            row.over25_rate = s["over25_count"] / n
            row.last_updated = now
        await session.commit()
    print(f"  team_stats: {len(team_agg)} times atualizados")

    # 4. Upsert matchup_stats
    async with sf() as session:
        for (ta, tb), s in matchup_agg.items():
            n = s["total_games"]
            stmt = select(MatchupStats).where(
                MatchupStats.team_a == ta, MatchupStats.team_b == tb
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                row = MatchupStats(team_a=ta, team_b=tb)
                session.add(row)
            row.total_games = n
            row.avg_total_goals = s["total_goals"] / n
            row.over25_rate = s["over25_count"] / n
            row.last_updated = now
        await session.commit()
    print(f"  matchup_stats: {len(matchup_agg)} matchups atualizados")

    # 5. Upsert player_team_preferences
    # Calcular total por jogador para is_main_team
    player_total = defaultdict(int)
    for (pn, tn), s in pref_agg.items():
        player_total[pn] += s["times_used"]

    async with sf() as session:
        for (pn, tn), s in pref_agg.items():
            n = s["times_used"]
            stmt = select(PlayerTeamPreference).where(
                PlayerTeamPreference.player_name == pn,
                PlayerTeamPreference.team_name == tn,
            )
            row = (await session.execute(stmt)).scalar_one_or_none()
            if row is None:
                row = PlayerTeamPreference(player_name=pn, team_name=tn)
                session.add(row)
            row.times_used = n
            row.goals_scored_with = s["goals_scored"]
            row.avg_goals_with = s["goals_scored"] / n
            row.is_main_team = (n / player_total[pn]) > 0.5 if player_total[pn] > 0 else False
        await session.commit()
    print(f"  player_team_prefs: {len(pref_agg)} combos atualizados")

    # 6. Resumo
    async with sf() as session:
        stmt = (
            select(PlayerTeamPreference)
            .where(PlayerTeamPreference.times_used >= 10)
            .order_by(PlayerTeamPreference.avg_goals_with.desc())
            .limit(20)
        )
        result = await session.execute(stmt)
        prefs = result.scalars().all()
        print(f"\nTOP 20 combos jogador+time (min 10 jogos):")
        for p in prefs:
            main = " [MAIN]" if p.is_main_team else ""
            print(f"  {p.player_name:15s} + {p.team_name:20s}: avg={p.avg_goals_with:.2f} n={p.times_used}{main}")

        # Top times ofensivos
        stmt2 = (
            select(TeamStats)
            .where(TeamStats.total_games >= 20)
            .order_by(TeamStats.over25_rate.desc())
            .limit(15)
        )
        result2 = await session.execute(stmt2)
        teams = result2.scalars().all()
        print(f"\nTOP 15 times over 2.5 rate (min 20 jogos):")
        for t in teams:
            print(f"  {t.team_name:20s}: O2.5={t.over25_rate:.1%} avg_gf={t.avg_goals_scored:.1f} n={t.total_games}")


asyncio.run(main())
