"""Analise: perdedor do G1 VENCER o G2 (mercado ML / moneyline)."""
import asyncio
import sys
from collections import defaultdict
import statistics

sys.stdout.reconfigure(encoding="utf-8")

ELITES = {
    "KraftVK","Bomb1to","DaVa","LaikingDast","dor1an","OG","V1nn",
    "DangerDim77","tonexo","Wboy","RuBIX","Uncle","Kray",
}

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
            G2.player_home.label("g2_ph"),
            G2.score_home.label("g2_sh"), G2.score_away.label("g2_sa"),
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
        data.append({
            "loser": loser, "winner": winner, "lg1": lg1, "wg1": wg1,
            "lg2": lg2, "wg2": wg2, "loser_won": lg2 > wg2, "draw": lg2 == wg2,
            "margin_g1": wg1 - lg1, "total_g1": wg1 + lg1,
            "lt": lt, "wt": wt, "g2_hour": r.g2_hour, "score_g1": f"{wg1}-{lg1}",
        })

    n = len(data)
    wins = sum(1 for d in data if d["loser_won"])
    draws = sum(1 for d in data if d["draw"])

    print(f"TOTAL PARES: {n}")
    print(f"\n{'='*80}")
    print("1. RESULTADO GLOBAL DO PERDEDOR EM G2")
    print(f"{'='*80}")
    print(f"  VENCE:  {wins:>5} ({wins/n:.1%})")
    print(f"  EMPATE: {draws:>5} ({draws/n:.1%})")
    print(f"  PERDE:  {n-wins-draws:>5} ({(n-wins-draws)/n:.1%})")
    print(f"\n  Se odds ML ~2.60 (implied 38.5%): edge = {wins/n - 0.385:+.1%}, ROI = {(wins/n*2.60-1)*100:+.1f}%")

    # 2. POR GOLS DO PERDEDOR EM G1
    print(f"\n{'='*80}")
    print("2. GOLS DO PERDEDOR EM G1 -> VENCE G2")
    print(f"{'='*80}")
    by_lg1 = defaultdict(list)
    for d in data: by_lg1[d["lg1"]].append(d)
    print(f"  {'Gols':>6} {'n':>5} {'Vence':>6} {'Empate':>7} {'AvgLG2':>7} {'AvgWG2':>7}")
    for g in sorted(by_lg1):
        items = by_lg1[g]; nn = len(items)
        w = sum(1 for d in items if d["loser_won"]) / nn
        dr = sum(1 for d in items if d["draw"]) / nn
        alg = statistics.mean(d["lg2"] for d in items)
        awg = statistics.mean(d["wg2"] for d in items)
        flag = " ***" if w >= 0.42 else (" **" if w >= 0.38 else "")
        print(f"  {g:>4}g  {nn:>5} {w:>5.1%} {dr:>6.1%} {alg:>7.2f} {awg:>7.2f}{flag}")

    # 3. PLACAR EXATO
    print(f"\n{'='*80}")
    print("3. PLACAR EXATO G1 -> VENCE G2 (n>=30)")
    print(f"{'='*80}")
    by_score = defaultdict(list)
    for d in data: by_score[d["score_g1"]].append(d)
    stats_list = [(sc, len(it), sum(1 for d in it if d["loser_won"])/len(it))
                  for sc, it in by_score.items() if len(it) >= 30]
    stats_list.sort(key=lambda x: x[2], reverse=True)
    print(f"  {'Score':>9} {'n':>5} {'Vence':>6}")
    for sc, nn, w in stats_list:
        flag = " ***" if w >= 0.42 else (" **" if w >= 0.38 else "")
        print(f"  {sc:>9} {nn:>5} {w:>5.1%}{flag}")

    # 4. MARGEM
    print(f"\n{'='*80}")
    print("4. MARGEM DE DERROTA -> VENCE G2")
    print(f"{'='*80}")
    by_m = defaultdict(list)
    for d in data: by_m[d["margin_g1"]].append(d)
    for m in sorted(by_m):
        items = by_m[m]; nn = len(items)
        w = sum(1 for d in items if d["loser_won"]) / nn
        flag = " ***" if w >= 0.40 else ""
        print(f"  Margem {m:>2}g: n={nn:>5} Vence={w:.1%}{flag}")

    # 5. HORA
    print(f"\n{'='*80}")
    print("5. HORA DO DIA G2 (slots 3h)")
    print(f"{'='*80}")
    by_slot = defaultdict(list)
    for d in data:
        if d["g2_hour"] is not None: by_slot[(d["g2_hour"]//3)*3].append(d)
    for s in sorted(by_slot):
        items = by_slot[s]; nn = len(items)
        w = sum(1 for d in items if d["loser_won"]) / nn
        flag = " ***" if w >= 0.40 else ""
        print(f"  {s:02d}-{s+3:02d}h: n={nn:>5} Vence={w:.1%}{flag}")

    # 6. TOP JOGADORES
    print(f"\n{'='*80}")
    print("6. TOP JOGADORES (n>=40)")
    print(f"{'='*80}")
    by_p = defaultdict(list)
    for d in data: by_p[d["loser"]].append(d)
    pstats = [(p, len(it), sum(1 for d in it if d["loser_won"])/len(it))
              for p, it in by_p.items() if len(it) >= 40]
    pstats.sort(key=lambda x: x[2], reverse=True)
    print(f"  {'Jogador':<20} {'n':>4} {'Vence':>6}")
    for p, nn, w in pstats[:20]:
        flag = " ***" if w >= 0.45 else (" **" if w >= 0.40 else "")
        print(f"  {p:<20} {nn:>4} {w:>5.1%}{flag}")

    # 7. H2H
    print(f"\n{'='*80}")
    print("7. TOP H2H (n>=15)")
    print(f"{'='*80}")
    h2h = defaultdict(list)
    for d in data: h2h[(d["loser"], d["winner"])].append(d)
    h2stats = [(f"{l} perde p/ {w}", len(it), sum(1 for d in it if d["loser_won"])/len(it))
               for (l, w), it in h2h.items() if len(it) >= 15]
    h2stats.sort(key=lambda x: x[2], reverse=True)
    print(f"  {'Matchup':<45} {'n':>4} {'Vence':>6}")
    for key, nn, w in h2stats[:20]:
        flag = " ***" if w >= 0.50 else (" **" if w >= 0.45 else "")
        print(f"  {key:<45} {nn:>4} {w:>5.1%}{flag}")

    # 8. JOGADOR + TIME
    print(f"\n{'='*80}")
    print("8. JOGADOR + TIME (n>=10)")
    print(f"{'='*80}")
    pt = defaultdict(list)
    for d in data:
        if d["lt"]: pt[(d["loser"], d["lt"])].append(d)
    ptstats = [(f"{p} + {t}", len(it), sum(1 for d in it if d["loser_won"])/len(it))
               for (p, t), it in pt.items() if len(it) >= 10]
    ptstats.sort(key=lambda x: x[2], reverse=True)
    print(f"  {'Combo':<40} {'n':>4} {'Vence':>6}")
    for key, nn, w in ptstats[:25]:
        flag = " ***" if w >= 0.55 else (" **" if w >= 0.45 else "")
        print(f"  {key:<40} {nn:>4} {w:>5.1%}{flag}")

    # 9. SWEET SPOTS
    print(f"\n{'='*80}")
    print("9. SWEET SPOTS: lg1 x total_g1 x periodo (n>=30, Vence>=40%)")
    print(f"{'='*80}")
    combos = defaultdict(list)
    for d in data:
        if d["g2_hour"] is None: continue
        slot = "manha" if 3 <= d["g2_hour"] < 12 else ("tarde" if 12 <= d["g2_hour"] < 18 else "noite")
        combos[(d["lg1"], d["total_g1"], slot)].append(d)
    results = [(lg1, tg1, sl, len(it), sum(1 for d in it if d["loser_won"])/len(it))
               for (lg1, tg1, sl), it in combos.items() if len(it) >= 30 and sum(1 for d in it if d["loser_won"])/len(it) >= 0.38]
    results.sort(key=lambda x: x[4], reverse=True)
    print(f"  {'GolsG1':>7} {'TotalG1':>8} {'Periodo':>8} {'n':>5} {'Vence':>6}")
    for lg1, tg1, sl, nn, w in results[:20]:
        flag = " ***" if w >= 0.48 else " **"
        print(f"  {lg1:>5}g   {tg1:>6}g  {sl:>8} {nn:>5} {w:>5.1%}{flag}")

    # 10. SIMULACAO ROI
    print(f"\n{'='*80}")
    print("10. SIMULACAO ROI @2.60 (odds tipica ML perdedor)")
    print(f"{'='*80}")
    odds = 2.60
    filters = [
        ("Todos", data),
        ("Margem 1g", [d for d in data if d["margin_g1"] == 1]),
        ("3+g em G1", [d for d in data if d["lg1"] >= 3]),
        ("4+g em G1", [d for d in data if d["lg1"] >= 4]),
        ("Elite", [d for d in data if d["loser"] in ELITES]),
        ("Elite + 3g", [d for d in data if d["loser"] in ELITES and d["lg1"] >= 3]),
        ("Elite + margem1", [d for d in data if d["loser"] in ELITES and d["margin_g1"] == 1]),
        ("Manha", [d for d in data if d["g2_hour"] is not None and 3 <= d["g2_hour"] < 12]),
        ("Elite + manha", [d for d in data if d["loser"] in ELITES and d["g2_hour"] is not None and 3 <= d["g2_hour"] < 12]),
        ("Elite+3g+manha", [d for d in data if d["loser"] in ELITES and d["lg1"] >= 3 and d["g2_hour"] is not None and 3 <= d["g2_hour"] < 12]),
    ]
    print(f"  {'Filtro':<22} {'n':>5} {'Vence%':>7} {'Profit':>8} {'ROI':>7}")
    print("  " + "-" * 52)
    for label, subset in filters:
        if not subset: continue
        nn = len(subset)
        w = sum(1 for d in subset if d["loser_won"]) / nn
        profit = nn * (w * odds - 1)
        roi = w * odds - 1
        flag = " ***" if roi > 0.05 else (" **" if roi > 0 else "")
        print(f"  {label:<22} {nn:>5} {w:>6.1%} {profit:>+8.1f}u {roi:>+6.1%}{flag}")

asyncio.run(main())
