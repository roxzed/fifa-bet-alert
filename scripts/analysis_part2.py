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
            G2.player_home.label("g2_ph"), G2.player_away.label("g2_pa"),
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
        else:
            loser, winner, lg1, wg1 = r.g1_pa, r.g1_ph, r.g1_sa, r.g1_sh
        lg2 = r.g2_sh if r.g2_ph == loser else r.g2_sa
        wg2 = r.g2_sh if r.g2_ph == winner else r.g2_sa
        data.append({"loser":loser,"winner":winner,"lg1":lg1,"wg1":wg1,"lg2":lg2,"wg2":wg2,
                      "margin":wg1-lg1,"total_g1":wg1+lg1,"g2_hour":r.g2_hour})

    n = len(data)
    o25g = sum(1 for d in data if d["lg2"]>2)/n

    # 8. VENCEDOR CAI?
    print("="*80)
    print("8. VENCEDOR EM G1: ELE CAI NO G2?")
    print("="*80)
    by_wg1 = defaultdict(list)
    for d in data: by_wg1[d["wg1"]].append(d)
    print(f"  {'GolsW G1':>9} {'n':>5} {'AvgW G2':>8} {'Delta':>6} {'PerdeG2':>8} {'CedeAoPerded':>13}")
    for g in sorted(by_wg1):
        items=by_wg1[g]; nn=len(items)
        if nn < 10: continue
        avg_wg2=statistics.mean(d["wg2"] for d in items)
        avg_lg2=statistics.mean(d["lg2"] for d in items)
        perde=sum(1 for d in items if d["lg2"]>d["wg2"])/nn
        print(f"  {g:>7}g  {nn:>5} {avg_wg2:>8.2f} {avg_wg2-g:>+5.2f} {perde:>8.1%} {avg_lg2:>13.2f}")

    # 9. H2H
    print(f"\n{'='*80}")
    print("9. TOP MATCHUPS H2H: O2.5 do perdedor (min 15)")
    print(f"{'='*80}")
    h2h = defaultdict(list)
    for d in data:
        h2h[f"{d['loser']} perde p/ {d['winner']}"].append(d["lg2"])
    h2h_stats = []
    for key, goals in h2h.items():
        if len(goals) < 15: continue
        r25 = sum(1 for g in goals if g > 2) / len(goals)
        r35 = sum(1 for g in goals if g > 3) / len(goals)
        h2h_stats.append((key, len(goals), r25, r35, statistics.mean(goals)))
    h2h_stats.sort(key=lambda x: x[2], reverse=True)
    print(f"  {'Matchup':<45} {'n':>4} {'O2.5':>5} {'O3.5':>5} {'AvgG2':>6}")
    for key, nn, r25, r35, avg in h2h_stats[:25]:
        flag = " ***" if r25 >= 0.70 else (" **" if r25 >= 0.60 else "")
        print(f"  {key:<45} {nn:>4} {r25:>5.0%} {r35:>5.0%} {avg:>6.2f}{flag}")

    # 10. TOTAL G1 -> G2
    print(f"\n{'='*80}")
    print("10. TOTAL GOLS G1 -> Performance perdedor G2")
    print(f"{'='*80}")
    by_tg1 = defaultdict(list)
    for d in data: by_tg1[d["total_g1"]].append(d)
    print(f"  {'TotalG1':>8} {'n':>5} {'TotalG2':>8} {'AvgLG2':>7} {'O2.5':>5} {'O3.5':>5}")
    for t in sorted(by_tg1):
        items=by_tg1[t]; nn=len(items)
        if nn < 20: continue
        atg2=statistics.mean(d["lg2"]+d["wg2"] for d in items)
        alg2=statistics.mean(d["lg2"] for d in items)
        r25=sum(1 for d in items if d["lg2"]>2)/nn
        r35=sum(1 for d in items if d["lg2"]>3)/nn
        print(f"  {t:>6}g  {nn:>5} {atg2:>8.2f} {alg2:>7.2f} {r25:>5.1%} {r35:>5.1%}")

    # 12. PADRAO JOGO
    print(f"\n{'='*80}")
    print("12. PADRAO DO JOGO G1 -> O2.5 em G2")
    print(f"{'='*80}")
    def classify(wg, lg):
        m = wg - lg
        if lg == 0 and wg + lg <= 2: return "cleansheet_baixo (0g, tot<=2)"
        if lg == 0: return "cleansheet_alto (0g, tot>2)"
        if lg >= 3 and m >= 3: return "goleada_aberta (3+g, marg>=3)"
        if lg >= 3 and m >= 2: return "massacre_aberto (3+g, marg=2)"
        if lg >= 3 and m == 1: return "jogo_alto_apertado (3+g, marg=1)"
        if lg >= 2 and m == 1: return "equilibrado (2+g, marg=1)"
        if lg == 1 and m == 1: return "fechado (1g, marg=1)"
        if lg == 1 and m >= 2: return "derrota_passiva (1g, marg>=2)"
        return "outro"
    by_pat = defaultdict(list)
    for d in data: by_pat[classify(d["wg1"], d["lg1"])].append(d)
    pats = []
    for p, items in by_pat.items():
        nn = len(items)
        r25 = sum(1 for d in items if d["lg2"]>2)/nn
        r35 = sum(1 for d in items if d["lg2"]>3)/nn
        pats.append((p, nn, r25, r35, statistics.mean(d["lg2"] for d in items)))
    pats.sort(key=lambda x: x[2], reverse=True)
    print(f"  {'Padrao':<42} {'n':>5} {'O2.5':>5} {'O3.5':>5} {'AvgG2':>6}")
    for p, nn, r25, r35, avg in pats:
        flag = " ***" if r25-o25g >= 0.06 else (" **" if r25-o25g >= 0.03 else "")
        print(f"  {p:<42} {nn:>5} {r25:>5.1%} {r35:>5.1%} {avg:>6.2f}{flag}")

    # 13. SWEET SPOTS
    print(f"\n{'='*80}")
    print("13. SWEET SPOTS: lg1 x total_g1 x periodo com O2.5 >= 55% (n>=30)")
    print(f"{'='*80}")
    combos3 = defaultdict(list)
    for d in data:
        if d["g2_hour"] is None: continue
        h = d["g2_hour"]
        slot = "manha" if 3 <= h < 12 else ("tarde" if 12 <= h < 18 else "noite")
        combos3[(d["lg1"], d["total_g1"], slot)].append(d["lg2"])
    results = []
    for (lg1, tg1, slot), goals in combos3.items():
        if len(goals) < 30: continue
        r25 = sum(1 for g in goals if g > 2) / len(goals)
        r35 = sum(1 for g in goals if g > 3) / len(goals)
        if r25 >= 0.55:
            results.append((lg1, tg1, slot, len(goals), r25, r35))
    results.sort(key=lambda x: x[4], reverse=True)
    print(f"  {'GolsG1':>7} {'TotalG1':>8} {'Periodo':>8} {'n':>5} {'O2.5':>5} {'O3.5':>5}")
    for lg1, tg1, slot, nn, r25, r35 in results:
        flag = " ***" if r25 >= 0.65 else " **"
        print(f"  {lg1:>5}g   {tg1:>6}g  {slot:>8} {nn:>5} {r25:>5.1%} {r35:>5.1%}{flag}")

asyncio.run(main())
