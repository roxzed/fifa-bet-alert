"""Analise profunda: TODAS as variaveis possiveis para vitoria do perdedor em G2."""
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
            G1.hour_of_day.label("g1_hour"), G1.day_of_week.label("g1_dow"),
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
        loser_home_g2 = r.g2_ph == loser
        data.append({
            "loser": loser, "winner": winner, "lg1": lg1, "wg1": wg1,
            "lg2": lg2, "wg2": wg2, "loser_won": lg2 > wg2, "draw": lg2 == wg2,
            "margin_g1": wg1 - lg1, "total_g1": wg1 + lg1, "total_g2": lg2 + wg2,
            "lt": lt, "wt": wt, "g2_hour": r.g2_hour, "dow": r.g1_dow,
            "loser_home_g2": loser_home_g2,
            "score_g1": f"{wg1}-{lg1}",
            "diff_g2": lg2 - wg2,  # positivo = perdedor ganhou
            "loser_improved": lg2 > lg1,
            "winner_dropped": wg2 < wg1,
        })

    n = len(data)
    global_win = sum(1 for d in data if d["loser_won"]) / n

    # ==============================================
    # A. VARIAVEL: Diferenca de gols G1 do VENCEDOR (quem cedeu mais)
    # Hipotese: vencedor que goleou mais tende a relaxar mais
    # ==============================================
    print(f"TOTAL: {n} pares | Vitoria global perdedor: {global_win:.1%}")
    print(f"\n{'='*80}")
    print("A. GOLS DO VENCEDOR EM G1 -> Perdedor vence G2")
    print("   (Quem goleou mais no G1, relaxa mais no G2?)")
    print(f"{'='*80}")
    by_wg1 = defaultdict(list)
    for d in data: by_wg1[d["wg1"]].append(d)
    for g in sorted(by_wg1):
        items = by_wg1[g]; nn = len(items)
        if nn < 20: continue
        w = sum(1 for d in items if d["loser_won"]) / nn
        drop = sum(1 for d in items if d["winner_dropped"]) / nn
        avg_drop = statistics.mean(d["wg1"] - d["wg2"] for d in items)
        flag = " ***" if w >= 0.42 else (" **" if w >= 0.38 else "")
        print(f"  W fez {g}g G1: n={nn:>5} P vence={w:.1%} W cai={drop:.0%} W perde avg {avg_drop:.1f}g{flag}")

    # ==============================================
    # B. VARIAVEL: Perdedor era HOME ou AWAY em G2
    # ==============================================
    print(f"\n{'='*80}")
    print("B. PERDEDOR HOME vs AWAY em G2")
    print(f"{'='*80}")
    for label, subset in [("Home G2", [d for d in data if d["loser_home_g2"]]),
                           ("Away G2", [d for d in data if not d["loser_home_g2"]])]:
        nn = len(subset)
        w = sum(1 for d in subset if d["loser_won"]) / nn
        print(f"  {label}: n={nn:>5} Vence={w:.1%}")

    # ==============================================
    # C. VARIAVEL: Tipo de derrota (cleansheet, apertada, goleada)
    # ==============================================
    print(f"\n{'='*80}")
    print("C. PADRAO DA DERROTA G1 -> Vence G2")
    print(f"{'='*80}")
    def classify(wg, lg):
        m = wg - lg
        if lg == 0 and wg <= 2: return "cleansheet_baixo (0g, W<=2)"
        if lg == 0: return "cleansheet_alto (0g, W>2)"
        if lg >= 3 and m >= 3: return "goleada_aberta (3+g, marg>=3)"
        if lg >= 3 and m == 2: return "massacre_aberto (3+g, marg=2)"
        if lg >= 3 and m == 1: return "jogo_alto_apertado (3+g, marg=1)"
        if lg >= 2 and m == 1: return "equilibrado_alto (2+g, marg=1)"
        if lg == 1 and m == 1: return "jogo_fechado (1g, marg=1)"
        if lg == 1 and m >= 2: return "derrota_passiva (1g, marg>=2)"
        return "outro"
    by_pat = defaultdict(list)
    for d in data: by_pat[classify(d["wg1"], d["lg1"])].append(d)
    pats = [(p, len(it), sum(1 for d in it if d["loser_won"])/len(it)) for p, it in by_pat.items()]
    pats.sort(key=lambda x: x[2], reverse=True)
    for p, nn, w in pats:
        flag = " ***" if w >= 0.42 else (" **" if w >= 0.38 else "")
        print(f"  {p:<40} n={nn:>5} Vence={w:.1%}{flag}")

    # ==============================================
    # D. VARIAVEL: DIA DA SEMANA
    # ==============================================
    print(f"\n{'='*80}")
    print("D. DIA DA SEMANA -> Vence G2")
    print(f"{'='*80}")
    dias = ["Seg","Ter","Qua","Qui","Sex","Sab","Dom"]
    by_dow = defaultdict(list)
    for d in data:
        if d["dow"] is not None: by_dow[d["dow"]].append(d)
    for dow in sorted(by_dow):
        items = by_dow[dow]; nn = len(items)
        w = sum(1 for d in items if d["loser_won"]) / nn
        flag = " ***" if w >= 0.40 else ""
        print(f"  {dias[dow]}: n={nn:>5} Vence={w:.1%}{flag}")

    # ==============================================
    # E. VARIAVEL: Time do OPONENTE (quem ganhou G1)
    # Hipotese: alguns times cedem mais no G2
    # ==============================================
    print(f"\n{'='*80}")
    print("E. TIME DO VENCEDOR (oponente) -> Perdedor vence G2 (n>=30)")
    print("   (Contra qual time o perdedor mais vira?)")
    print(f"{'='*80}")
    by_wt = defaultdict(list)
    for d in data:
        if d["wt"]: by_wt[d["wt"]].append(d)
    wt_stats = [(t, len(it), sum(1 for d in it if d["loser_won"])/len(it))
                for t, it in by_wt.items() if len(it) >= 30]
    wt_stats.sort(key=lambda x: x[2], reverse=True)
    print(f"  {'Time oponente':<25} {'n':>5} {'P vence':>8}")
    for t, nn, w in wt_stats[:15]:
        flag = " ***" if w >= 0.42 else (" **" if w >= 0.38 else "")
        print(f"  {t:<25} {nn:>5} {w:>7.1%}{flag}")
    print(f"\n  PIORES (perdedor quase nunca vira):")
    for t, nn, w in wt_stats[-10:]:
        print(f"  {t:<25} {nn:>5} {w:>7.1%}")

    # ==============================================
    # F. VARIAVEL: Time do PERDEDOR
    # ==============================================
    print(f"\n{'='*80}")
    print("F. TIME DO PERDEDOR -> Vence G2 (n>=30)")
    print(f"{'='*80}")
    by_lt = defaultdict(list)
    for d in data:
        if d["lt"]: by_lt[d["lt"]].append(d)
    lt_stats = [(t, len(it), sum(1 for d in it if d["loser_won"])/len(it))
                for t, it in by_lt.items() if len(it) >= 30]
    lt_stats.sort(key=lambda x: x[2], reverse=True)
    print(f"  {'Time perdedor':<25} {'n':>5} {'Vence':>6}")
    for t, nn, w in lt_stats[:15]:
        flag = " ***" if w >= 0.42 else (" **" if w >= 0.38 else "")
        print(f"  {t:<25} {nn:>5} {w:>5.1%}{flag}")
    print(f"\n  PIORES:")
    for t, nn, w in lt_stats[-10:]:
        print(f"  {t:<25} {nn:>5} {w:>5.1%}")

    # ==============================================
    # G. VARIAVEL: Confronto de times (time perdedor vs time oponente)
    # ==============================================
    print(f"\n{'='*80}")
    print("G. CONFRONTO DE TIMES -> Perdedor vence G2 (n>=15)")
    print(f"{'='*80}")
    by_tt = defaultdict(list)
    for d in data:
        if d["lt"] and d["wt"]: by_tt[(d["lt"], d["wt"])].append(d)
    tt_stats = [(f"{lt} vs {wt}", len(it), sum(1 for d in it if d["loser_won"])/len(it))
                for (lt, wt), it in by_tt.items() if len(it) >= 15]
    tt_stats.sort(key=lambda x: x[2], reverse=True)
    print(f"  {'Confronto':<45} {'n':>4} {'Vence':>6}")
    for key, nn, w in tt_stats[:20]:
        flag = " ***" if w >= 0.50 else (" **" if w >= 0.42 else "")
        print(f"  {key:<45} {nn:>4} {w:>5.1%}{flag}")

    # ==============================================
    # H. VARIAVEL: Total de gols G1 (jogo alto = mais viradas?)
    # ==============================================
    print(f"\n{'='*80}")
    print("H. TOTAL GOLS G1 -> Perdedor vence G2")
    print(f"{'='*80}")
    by_tg1 = defaultdict(list)
    for d in data: by_tg1[d["total_g1"]].append(d)
    for t in sorted(by_tg1):
        items = by_tg1[t]; nn = len(items)
        if nn < 20: continue
        w = sum(1 for d in items if d["loser_won"]) / nn
        flag = " ***" if w >= 0.42 else (" **" if w >= 0.38 else "")
        print(f"  Total {t:>2}g: n={nn:>5} Vence={w:.1%}{flag}")

    # ==============================================
    # I. VARIAVEL: Quem era o VENCEDOR de G1 (oponente especifico)
    # Hipotese: alguns jogadores "cedem" muito no G2
    # ==============================================
    print(f"\n{'='*80}")
    print("I. VENCEDOR DE G1: Quem mais CEDE a vitoria no G2 (n>=40)")
    print(f"{'='*80}")
    by_winner = defaultdict(list)
    for d in data: by_winner[d["winner"]].append(d)
    winner_stats = [(w, len(it), sum(1 for d in it if d["loser_won"])/len(it))
                    for w, it in by_winner.items() if len(it) >= 40]
    winner_stats.sort(key=lambda x: x[2], reverse=True)
    print(f"  {'Vencedor G1':<20} {'n':>4} {'Cede no G2':>11}")
    for w, nn, rate in winner_stats[:15]:
        flag = " ***" if rate >= 0.42 else (" **" if rate >= 0.38 else "")
        print(f"  {w:<20} {nn:>4} {rate:>10.1%}{flag}")
    print(f"\n  TRANCAM (perdedor quase nunca vira contra eles):")
    for w, nn, rate in winner_stats[-10:]:
        print(f"  {w:<20} {nn:>4} {rate:>10.1%}")

    # ==============================================
    # J. VARIAVEL: Diff entre gols G1 vs G2 do VENCEDOR
    # Hipotese: quanto mais o vencedor cai, mais o perdedor ganha
    # ==============================================
    print(f"\n{'='*80}")
    print("J. QUEDA DO VENCEDOR: (golsW_G1 - golsW_G2) -> Perdedor vence?")
    print(f"{'='*80}")
    by_wdrop = defaultdict(list)
    for d in data:
        drop = d["wg1"] - d["wg2"]
        by_wdrop[drop].append(d)
    for drop in sorted(by_wdrop):
        items = by_wdrop[drop]; nn = len(items)
        if nn < 20: continue
        w = sum(1 for d in items if d["loser_won"]) / nn
        flag = " ***" if w >= 0.50 else (" **" if w >= 0.40 else "")
        label = f"W caiu {drop}g" if drop > 0 else (f"W manteve" if drop == 0 else f"W subiu {-drop}g")
        print(f"  {label:<18} n={nn:>5} P vence={w:.1%}{flag}")

    # ==============================================
    # K. VARIAVEL COMPOSTA: Ratio gols perdedor G1 / total G1
    # Hipotese: se o perdedor fez grande parte dos gols, jogo foi equilibrado
    # ==============================================
    print(f"\n{'='*80}")
    print("K. RATIO (gols_perdedor / total_G1) -> Vence G2")
    print(f"{'='*80}")
    by_ratio = defaultdict(list)
    for d in data:
        if d["total_g1"] > 0:
            ratio = d["lg1"] / d["total_g1"]
            bucket = round(ratio * 10) / 10  # 0.0, 0.1, 0.2 ... 0.5
            by_ratio[bucket].append(d)
    for r in sorted(by_ratio):
        items = by_ratio[r]; nn = len(items)
        if nn < 20: continue
        w = sum(1 for d in items if d["loser_won"]) / nn
        flag = " ***" if w >= 0.42 else (" **" if w >= 0.38 else "")
        print(f"  Ratio {r:.1f}: n={nn:>5} Vence={w:.1%}{flag}")

    # ==============================================
    # L. VARIAVEL: Score EXATO de G2 — distribuicao
    # ==============================================
    print(f"\n{'='*80}")
    print("L. DISTRIBUICAO DE VITORIAS DO PERDEDOR EM G2 (quando vence)")
    print(f"{'='*80}")
    wins_only = [d for d in data if d["loser_won"]]
    by_diff = defaultdict(int)
    for d in wins_only:
        diff = d["lg2"] - d["wg2"]
        by_diff[diff] += 1
    total_wins = len(wins_only)
    for diff in sorted(by_diff):
        count = by_diff[diff]
        bar = "#" * int(count / total_wins * 100)
        print(f"  Vence por {diff}g: {count:>5} ({count/total_wins:.1%}) {bar}")

    # ==============================================
    # M. MEGA COMBO: Todas as variaveis juntas
    # ==============================================
    print(f"\n{'='*80}")
    print("M. MEGA ANALISE: Combinando TODAS as variaveis significativas")
    print(f"{'='*80}")

    combos = [
        ("Elite + margem1 + manha",
         lambda d: d["loser"] in ELITES and d["margin_g1"] == 1 and d["g2_hour"] is not None and 3 <= d["g2_hour"] < 12),
        ("Elite + margem1 + 3g",
         lambda d: d["loser"] in ELITES and d["margin_g1"] == 1 and d["lg1"] >= 3),
        ("Elite + jogo alto (total>=7) + manha",
         lambda d: d["loser"] in ELITES and d["total_g1"] >= 7 and d["g2_hour"] is not None and 3 <= d["g2_hour"] < 12),
        ("Elite + ratio >= 0.4 (jogo equilibrado)",
         lambda d: d["loser"] in ELITES and d["total_g1"] > 0 and d["lg1"]/d["total_g1"] >= 0.40),
        ("Elite + W fez 6+g G1 (vai cair muito)",
         lambda d: d["loser"] in ELITES and d["wg1"] >= 6),
        ("DaVa/OG/dor1an (top3) qualquer condicao",
         lambda d: d["loser"] in {"DaVa", "OG", "dor1an"}),
        ("DaVa/OG/dor1an + margem 1g",
         lambda d: d["loser"] in {"DaVa", "OG", "dor1an"} and d["margin_g1"] == 1),
        ("Top3 + margem1 + manha",
         lambda d: d["loser"] in {"DaVa", "OG", "dor1an"} and d["margin_g1"] == 1 and d["g2_hour"] is not None and 3 <= d["g2_hour"] < 12),
        ("Elite + contra cedente (hrk/Kodak/hotShot/Aveaaaaa)",
         lambda d: d["loser"] in ELITES and d["winner"] in {"hrk","Kodak","hotShot","Aveaaaaa","ssstasonn","GianniKid"}),
        ("Qualquer + ratio>=0.4 + margem1 + manha",
         lambda d: d["total_g1"] > 0 and d["lg1"]/d["total_g1"] >= 0.40 and d["margin_g1"] == 1 and d["g2_hour"] is not None and 3 <= d["g2_hour"] < 12),
    ]

    print(f"  {'Combo':<55} {'n':>5} {'Vence':>6} {'ROI@2.6':>8}")
    print("  " + "-" * 78)
    for label, fn in combos:
        subset = [d for d in data if fn(d)]
        if not subset: continue
        nn = len(subset)
        w = sum(1 for d in subset if d["loser_won"]) / nn
        roi = w * 2.60 - 1
        flag = " ***" if roi >= 0.15 else (" **" if roi >= 0.05 else "")
        print(f"  {label:<55} {nn:>5} {w:>5.1%} {roi:>+7.1%}{flag}")

asyncio.run(main())
