"""Popula a tabela matchup_stats com dados historicos de todos os pares G1->G2."""
import asyncio
from collections import defaultdict

async def main():
    from src.db.database import async_session_factory
    from src.db.models import Match, MatchupStats
    from sqlalchemy import select
    from sqlalchemy.orm import aliased

    session = async_session_factory()

    G2 = aliased(Match)
    G1 = aliased(Match)
    stmt = (
        select(
            G1.team_home.label("g1_th"), G1.team_away.label("g1_ta"),
            G1.score_home.label("g1_sh"), G1.score_away.label("g1_sa"),
            G1.player_home.label("g1_ph"),
            G2.score_home.label("g2_sh"), G2.score_away.label("g2_sa"),
            G2.player_home.label("g2_ph"),
        )
        .join(G1, G2.pair_match_id == G1.id)
        .where(
            G2.is_return_match == True,
            G2.score_home != None, G2.score_away != None,
            G1.score_home != None, G1.score_away != None,
            G1.score_home != G1.score_away,
            G1.team_home != None, G1.team_away != None,
        )
    )
    result = await session.execute(stmt)
    rows = result.all()

    # Agregar por matchup (time_perdedor, time_oponente)
    matchups = defaultdict(lambda: {"total": 0, "over25": 0, "total_goals": 0})

    for r in rows:
        if r.g1_sh < r.g1_sa:
            loser_team, opp_team = r.g1_th, r.g1_ta
            loser = r.g1_ph
        else:
            loser_team, opp_team = r.g1_ta, r.g1_th
            # loser eh g1_pa (away)
            loser = None  # nao precisamos do nome aqui

        if not loser_team or not opp_team:
            continue

        # Gols do perdedor em G2 — como nao temos loser name facilmente,
        # usamos a abordagem: time do perdedor em G2 = mesmo de G1
        # Entao se g2_ph joga com loser_team, score_home sao gols dele
        # Senao score_away sao gols dele
        # Na verdade, no G2 os times sao os mesmos. Se loser jogava de home em G1
        # com loser_team, em G2 pode estar em qualquer posicao.
        # Vamos calcular pelo player name.
        if r.g1_sh < r.g1_sa:
            loser = r.g1_ph
        else:
            # loser eh away player de G1. Para saber o nome, precisamos de g1_pa
            # que nao temos no select. Vamos usar uma abordagem diferente.
            pass

    # Resetar e usar abordagem completa com player names
    await session.close()

    session2 = async_session_factory()
    stmt2 = (
        select(
            G1.player_home.label("g1_ph"), G1.player_away.label("g1_pa"),
            G1.score_home.label("g1_sh"), G1.score_away.label("g1_sa"),
            G1.team_home.label("g1_th"), G1.team_away.label("g1_ta"),
            G2.player_home.label("g2_ph"),
            G2.score_home.label("g2_sh"), G2.score_away.label("g2_sa"),
        )
        .join(G1, G2.pair_match_id == G1.id)
        .where(
            G2.is_return_match == True,
            G2.score_home != None, G2.score_away != None,
            G1.score_home != None, G1.score_away != None,
            G1.score_home != G1.score_away,
            G1.team_home != None, G1.team_away != None,
        )
    )
    result2 = await session2.execute(stmt2)
    rows2 = result2.all()

    matchups = defaultdict(lambda: {"total": 0, "over25": 0, "total_goals": 0})

    for r in rows2:
        if r.g1_sh < r.g1_sa:
            loser, loser_team, opp_team = r.g1_ph, r.g1_th, r.g1_ta
        else:
            loser, loser_team, opp_team = r.g1_pa, r.g1_ta, r.g1_th

        lg2 = r.g2_sh if r.g2_ph == loser else r.g2_sa

        # Chave ordenada para evitar duplicatas (A vs B == B vs A)
        key = tuple(sorted([loser_team, opp_team]))
        matchups[key]["total"] += 1
        matchups[key]["total_goals"] += lg2
        if lg2 > 2:
            matchups[key]["over25"] += 1

    # Inserir no banco
    inserted = 0
    for (team_a, team_b), stats in matchups.items():
        if stats["total"] < 5:
            continue
        rate = stats["over25"] / stats["total"]
        avg_goals = stats["total_goals"] / stats["total"]
        # Upsert: atualizar se já existe, criar se não
        existing_stmt = select(MatchupStats).where(
            MatchupStats.team_a == team_a, MatchupStats.team_b == team_b
        )
        existing = (await session2.execute(existing_stmt)).scalar_one_or_none()
        if existing:
            existing.total_games = stats["total"]
            existing.over25_rate = rate
            existing.avg_total_goals = avg_goals
        else:
            ms = MatchupStats(
                team_a=team_a,
                team_b=team_b,
                total_games=stats["total"],
                over25_rate=rate,
                avg_total_goals=avg_goals,
            )
            session2.add(ms)
        inserted += 1

    await session2.commit()
    print(f"Inseridos {inserted} matchups com >= 5 jogos")

    # Top 10
    top_stmt = select(MatchupStats).order_by(MatchupStats.over25_rate.desc()).limit(15)
    top = await session2.execute(top_stmt)
    print("\nTOP 15 matchups:")
    for m in top.scalars().all():
        print(f"  {m.team_a} vs {m.team_b}: n={m.total_games} O2.5={m.over25_rate:.1%}")

    await session2.close()

asyncio.run(main())
