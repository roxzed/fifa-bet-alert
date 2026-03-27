"""Analise profunda: variaveis ocultas para gerar mais entradas 24h."""
import asyncio
from collections import defaultdict
import statistics

async def main():
    from src.db.database import async_session_factory
    from src.db.models import Match
    from sqlalchemy import select
    from sqlalchemy.orm import aliased

    session = async_session_factory()
    G2 = aliased(Match)
    G1 = aliased(Match)
    stmt = (
        select(
            G1.player_home.label("g1_ph"), G1.player_away.label("g1_pa"),
            G1.score_home.label("g1_sh"), G1.score_away.label("g1_sa"),
            G1.team_home.label("g1_th"), G1.team_away.label("g1_ta"),
            G1.hour_of_day.label("g1_hour"),
            G2.player_home.label("g2_ph"), G2.player_away.label("g2_pa"),
            G2.score_home.label("g2_sh"), G2.score_away.label("g2_sa"),
            G2.team_home.label("g2_th"), G2.team_away.label("g2_ta"),
            G2.hour_of_day.label("g2_hour"),
        )
        .join(G1, G2.pair_match_id == G1.id)
        .where(G2.is_return_match == True, G2.score_home != None, G2.score_away != None,
               G1.score_home != None, G1.score_away != None, G1.score_home != G1.score_away)
    )
    result = await session.execute(stmt)
    rows = result.all()
    await session.close()

    data = []
    for r in rows:
        if r.g1_sh < r.g1_sa:
            loser, winner, lg1, wg1 = r.g1_ph, r.g1_pa, r.g1_sh, r.g1_sa
            lt, wt = r.g1_th, r.g1_ta
        else:
            loser, winner, lg1, wg1 = r.g1_pa, r.g1_ph, r.g1_sa, r.g1_sh
            lt, wt = r.g1_ta, r.g1_th
        lg2 = r.g2_sh if r.g2_ph == loser else r.g2_sa
        wg2 = r.g2_sh if r.g2_ph == winner else r.g2_sa
        # Em G2 os times sao os mesmos de G1
        data.append({
            "loser": loser, "winner": winner, "lg1": lg1, "wg1": wg1,
            "lg2": lg2, "wg2": wg2,
            "margin": wg1 - lg1, "total_g1": wg1 + lg1, "total_g2": lg2 + wg2,
            "loser_team": lt, "winner_team": wt,
            "g2_hour": r.g2_hour,
            "loser_home_g2": r.g2_ph == loser,
        })

    n = len(data)
    o25g = sum(1 for d in data if d["lg2"] > 2) / n
    o35g = sum(1 for d in data if d["lg2"] > 3) / n
    print(f"TOTAL: {n} pares | Global O2.5: {o25g:.1%} | O3.5: {o35g:.1%}")

    # ================================================================
    # A. JOGADOR + TIME DO PERDEDOR (combo)
    # ================================================================
    print(f"\n{'='*80}")
    print("A. JOGADOR + TIME (combo perdedor): O2.5 em G2 (min 10)")
    print(f"{'='*80}")
    pt = defaultdict(list)
    for d in data:
        if d["loser_team"]:
            pt[(d["loser"], d["loser_team"])].append(d["lg2"])
    pt_stats = []
    for (player, team), goals in pt.items():
        nn = len(goals)
        if nn < 10: continue
        r25 = sum(1 for g in goals if g > 2) / nn
        r35 = sum(1 for g in goals if g > 3) / nn
        pt_stats.append((player, team, nn, r25, r35, statistics.mean(goals)))
    pt_stats.sort(key=lambda x: x[3], reverse=True)
    print(f"  {'Jogador + Time':<40} {'n':>4} {'O2.5':>5} {'O3.5':>5} {'Avg':>5}")
    for player, team, nn, r25, r35, avg in pt_stats[:30]:
        diff = r25 - o25g
        flag = " ***" if diff >= 0.15 else (" **" if diff >= 0.08 else "")
        print(f"  {player} + {team:<25} {nn:>4} {r25:>5.0%} {r35:>5.0%} {avg:>5.2f}{flag}")
    print(f"\n  PIORES combos:")
    for player, team, nn, r25, r35, avg in pt_stats[-15:]:
        diff = r25 - o25g
        print(f"  {player} + {team:<25} {nn:>4} {r25:>5.0%} {r35:>5.0%} {avg:>5.2f}")

    # ================================================================
    # B. TIME DO OPONENTE (VENCEDOR) — quem cede mais gols ao perdedor
    # ================================================================
    print(f"\n{'='*80}")
    print("B. TIME DO VENCEDOR (oponente): Quem CEDE mais ao perdedor em G2 (min 30)")
    print(f"{'='*80}")
    wt_stats = defaultdict(list)
    for d in data:
        if d["winner_team"]:
            wt_stats[d["winner_team"]].append(d["lg2"])
    wt_sorted = []
    for team, goals in wt_stats.items():
        nn = len(goals)
        if nn < 30: continue
        r25 = sum(1 for g in goals if g > 2) / nn
        wt_sorted.append((team, nn, r25, statistics.mean(goals)))
    wt_sorted.sort(key=lambda x: x[2], reverse=True)
    print(f"  {'Time Oponente':<30} {'n':>5} {'O2.5 cedido':>11} {'Avg cedido':>11}")
    for team, nn, r25, avg in wt_sorted[:20]:
        flag = " ***" if r25 - o25g >= 0.06 else (" **" if r25 - o25g >= 0.03 else "")
        print(f"  {team:<30} {nn:>5} {r25:>11.1%} {avg:>11.2f}{flag}")
    print(f"\n  MELHORES DEFESAS (cedem menos):")
    for team, nn, r25, avg in wt_sorted[-10:]:
        print(f"  {team:<30} {nn:>5} {r25:>11.1%} {avg:>11.2f}")

    # ================================================================
    # C. TIME DO PERDEDOR — com qual time ele marca mais
    # ================================================================
    print(f"\n{'='*80}")
    print("C. TIME DO PERDEDOR: Com qual time ele marca mais em G2 (min 30)")
    print(f"{'='*80}")
    lt_stats = defaultdict(list)
    for d in data:
        if d["loser_team"]:
            lt_stats[d["loser_team"]].append(d["lg2"])
    lt_sorted = []
    for team, goals in lt_stats.items():
        nn = len(goals)
        if nn < 30: continue
        r25 = sum(1 for g in goals if g > 2) / nn
        lt_sorted.append((team, nn, r25, statistics.mean(goals)))
    lt_sorted.sort(key=lambda x: x[2], reverse=True)
    print(f"  {'Time Perdedor':<30} {'n':>5} {'O2.5':>5} {'Avg':>5}")
    for team, nn, r25, avg in lt_sorted[:20]:
        flag = " ***" if r25 - o25g >= 0.06 else (" **" if r25 - o25g >= 0.03 else "")
        print(f"  {team:<30} {nn:>5} {r25:>5.1%} {avg:>5.2f}{flag}")
    print(f"\n  PIORES times para o perdedor:")
    for team, nn, r25, avg in lt_sorted[-10:]:
        print(f"  {team:<30} {nn:>5} {r25:>5.1%} {avg:>5.2f}")

    # ================================================================
    # D. HOME vs AWAY do perdedor em G2
    # ================================================================
    print(f"\n{'='*80}")
    print("D. PERDEDOR HOME vs AWAY em G2")
    print(f"{'='*80}")
    home = [d for d in data if d["loser_home_g2"]]
    away = [d for d in data if not d["loser_home_g2"]]
    for label, items in [("Home (mandante)", home), ("Away (visitante)", away)]:
        nn = len(items)
        r25 = sum(1 for d in items if d["lg2"] > 2) / nn
        r35 = sum(1 for d in items if d["lg2"] > 3) / nn
        avg = statistics.mean(d["lg2"] for d in items)
        won = sum(1 for d in items if d["lg2"] > d["wg2"]) / nn
        print(f"  {label:<22} n={nn:>5} O2.5={r25:.1%} O3.5={r35:.1%} Avg={avg:.2f} VenceG2={won:.1%}")

    # ================================================================
    # E. RELACAO GOLS VENCEDOR G1 vs GOLS CEDIDOS AO PERDEDOR G2
    # ================================================================
    print(f"\n{'='*80}")
    print("E. GOLS DO VENCEDOR EM G1 -> Gols CEDIDOS ao perdedor em G2")
    print(f"{'='*80}")
    by_wg1 = defaultdict(list)
    for d in data: by_wg1[d["wg1"]].append(d)
    print(f"  {'W gols G1':>10} {'n':>5} {'Cede ao P':>10} {'O2.5 P':>7} {'O3.5 P':>7}")
    for g in sorted(by_wg1):
        items = by_wg1[g]; nn = len(items)
        if nn < 20: continue
        avg_lg2 = statistics.mean(d["lg2"] for d in items)
        r25 = sum(1 for d in items if d["lg2"] > 2) / nn
        r35 = sum(1 for d in items if d["lg2"] > 3) / nn
        print(f"  {g:>8}g  {nn:>5} {avg_lg2:>10.2f} {r25:>7.1%} {r35:>7.1%}")

    # ================================================================
    # F. COMBO: Time perdedor x Time oponente (confronto de times)
    # ================================================================
    print(f"\n{'='*80}")
    print("F. CONFRONTO DE TIMES: Time perdedor vs Time oponente -> O2.5 (min 15)")
    print(f"{'='*80}")
    tt = defaultdict(list)
    for d in data:
        if d["loser_team"] and d["winner_team"]:
            tt[(d["loser_team"], d["winner_team"])].append(d["lg2"])
    tt_stats = []
    for (lt, wt), goals in tt.items():
        nn = len(goals)
        if nn < 15: continue
        r25 = sum(1 for g in goals if g > 2) / nn
        tt_stats.append((lt, wt, nn, r25, statistics.mean(goals)))
    tt_stats.sort(key=lambda x: x[3], reverse=True)
    print(f"  {'Time Perdedor vs Oponente':<50} {'n':>4} {'O2.5':>5} {'Avg':>5}")
    for lt, wt, nn, r25, avg in tt_stats[:25]:
        flag = " ***" if r25 - o25g >= 0.15 else (" **" if r25 - o25g >= 0.08 else "")
        print(f"  {lt} vs {wt:<35} {nn:>4} {r25:>5.0%} {avg:>5.2f}{flag}")

    # ================================================================
    # G. SCORE G2 DO JOGO COMPLETO (total) — jogo explosivo?
    # ================================================================
    print(f"\n{'='*80}")
    print("G. DIFERENCA DE GOLS G1 DO VENCEDOR vs GOLS PERDEDOR G1 (score combo)")
    print(f"{'='*80}")
    # Placar exato -> distribuicao de gols perdedor G2
    by_score = defaultdict(list)
    for d in data:
        by_score[(d["wg1"], d["lg1"])].append(d)
    score_stats = []
    for (wg, lg), items in by_score.items():
        nn = len(items)
        if nn < 30: continue
        r25 = sum(1 for d in items if d["lg2"] > 2) / nn
        r35 = sum(1 for d in items if d["lg2"] > 3) / nn
        won_g2 = sum(1 for d in items if d["lg2"] > d["wg2"]) / nn
        score_stats.append((f"{wg}-{lg}", nn, r25, r35, won_g2, statistics.mean(d["lg2"] for d in items)))
    score_stats.sort(key=lambda x: x[2], reverse=True)
    print(f"  {'G1 Score':>9} {'n':>5} {'O2.5':>5} {'O3.5':>5} {'VenceG2':>8} {'AvgG2':>6}")
    for sc, nn, r25, r35, won, avg in score_stats:
        flag = " ***" if r25 >= 0.60 else (" **" if r25 >= 0.55 else "")
        print(f"  {sc:>9} {nn:>5} {r25:>5.1%} {r35:>5.1%} {won:>8.1%} {avg:>6.2f}{flag}")

    # ================================================================
    # H. NOITE (18-06h UTC): o que funciona fora da manha?
    # ================================================================
    print(f"\n{'='*80}")
    print("H. FORA DA MANHA (12-24h UTC): O que eleva O2.5?")
    print(f"{'='*80}")
    noite = [d for d in data if d["g2_hour"] is not None and (d["g2_hour"] >= 12 or d["g2_hour"] < 3)]
    nn_noite = len(noite)
    o25_noite = sum(1 for d in noite if d["lg2"] > 2) / nn_noite
    print(f"  Base noite/tarde (12-03h UTC): n={nn_noite} O2.5={o25_noite:.1%}")

    # Dentro da noite, filtrar por lg1
    for lg1_min in [2, 3, 4]:
        subset = [d for d in noite if d["lg1"] >= lg1_min]
        if not subset: continue
        nns = len(subset)
        r25 = sum(1 for d in subset if d["lg2"] > 2) / nns
        r35 = sum(1 for d in subset if d["lg2"] > 3) / nns
        print(f"  + Perdedor {lg1_min}+g em G1: n={nns:>5} O2.5={r25:.1%} O3.5={r35:.1%} (vs noite base {o25_noite:.1%})")

    # Noite + total G1 alto
    for tg1_min in [7, 8, 9]:
        subset = [d for d in noite if d["total_g1"] >= tg1_min]
        if not subset: continue
        nns = len(subset)
        r25 = sum(1 for d in subset if d["lg2"] > 2) / nns
        print(f"  + Total G1 >= {tg1_min}g: n={nns:>5} O2.5={r25:.1%}")

    # Noite + jogador elite
    elites = {"KraftVK","Bomb1to","DaVa","LaikingDast","dor1an","OG","V1nn","DangerDim77","tonexo","Wboy","RuBIX"}
    elite_noite = [d for d in noite if d["loser"] in elites]
    if elite_noite:
        nne = len(elite_noite)
        r25e = sum(1 for d in elite_noite if d["lg2"] > 2) / nne
        print(f"  + Jogador elite: n={nne:>5} O2.5={r25e:.1%}")

    # Noite + elite + 3g
    en3 = [d for d in elite_noite if d["lg1"] >= 3]
    if en3:
        nne3 = len(en3)
        r25e3 = sum(1 for d in en3 if d["lg2"] > 2) / nne3
        print(f"  + Elite + 3+g G1: n={nne3:>5} O2.5={r25e3:.1%}")

asyncio.run(main())
