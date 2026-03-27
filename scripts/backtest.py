"""
Full backtest analysis: loads historical data and validates the method statistically.
Run AFTER collect_history.py has finished.

Layers analisadas:
  1. Global base
  2. Player geral (min 30 amostras)
  3. Player forma recente (ultimas 20 partidas)
  4. H2H player: X perde para Y especificamente (min 15)
  5. H2H dominancia: taxa de vitoria de X contra Y (motivacao)
  6. Y pos-vitoria: Y tende a ceder gols apos ganhar G1?
  7. Player+time usado (min 15)
  8. Tipo de derrota / hora / dia
  9. Gols totais G1 como preditor
  10. Over 2.5, Over 3.5, Over 4.5 - melhor EV

Usage:
    python scripts/backtest.py
"""

import asyncio
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()


async def main() -> None:
    from src.db.database import init_db, async_session_factory
    from src.db.repositories import MatchRepository, MethodStatsRepository
    from src.core.probability import (
        classify_loss, wilson_confidence_interval, simulate_roi,
    )

    await init_db()

    print("=" * 65)
    print("   BACKTEST COMPLETO - FIFA Bet Alert")
    print("=" * 65)

    sf = async_session_factory
    match_repo = MatchRepository(sf)
    method_stats_repo = MethodStatsRepository(sf)

    if True:
        # -- Load all linked pairs (game1 + game2) --------------------------
        pairs = await match_repo.get_all_pairs()
        if not pairs:
            print("\n[ERRO] Nenhum par ida/volta encontrado.")
            print("Execute primeiro: python scripts/collect_history.py")
            return

        print(f"\n{len(pairs):,} pares ida/volta carregados\n")

        # ==================================================================
        # Build analysis dataset
        # ==================================================================
        results = []

        # Layer: stats gerais do perdedor
        player_stats = defaultdict(
            lambda: {"total": 0, "over25": 0, "over35": 0, "over45": 0, "goals": []}
        )

        # Layer: player + time usado
        player_team_stats = defaultdict(
            lambda: {"total": 0, "over25": 0, "over35": 0, "over45": 0, "goals": []}
        )

        # Layer: H2H player — X perde para Y
        h2h_stats = defaultdict(
            lambda: {"total": 0, "over25": 0, "over35": 0, "over45": 0, "goals": []}
        )

        # Para forma recente: lista ordenada de partidas por player
        player_history = defaultdict(list)

        # Para H2H dominancia: quem ganhou cada G1 entre par de jogadores
        # key=(winner, loser) → count de vezes que winner ganhou de loser em G1
        all_g1_results = defaultdict(int)

        # Y pos-vitoria: quando Y ganha G1, X (perdedor) marca X gols em G2
        y_post_win_stats = defaultdict(
            lambda: {"total": 0, "over25": 0, "over35": 0, "over45": 0, "goals": []}
        )

        # Gols totais G1 como preditor de G2
        goals_g1_stats = defaultdict(
            lambda: {"total": 0, "over25": 0, "over35": 0, "over45": 0}
        )

        # Gols do PERDEDOR em G1 como preditor (fator mais importante)
        loser_goals_g1_stats = defaultdict(
            lambda: {"total": 0, "over25": 0, "over35": 0, "over45": 0}
        )

        # Streak de derrotas consecutivas ANTES de cada jogo
        # key=jogador → lista ordenada de resultados (True=derrota)
        player_loss_history = defaultdict(list)

        # Outros segmentos
        loss_type_stats = defaultdict(
            lambda: {"total": 0, "over25": 0, "over35": 0, "over45": 0}
        )
        hour_stats = defaultdict(
            lambda: {"total": 0, "over25": 0, "over35": 0, "over45": 0}
        )
        dow_stats = defaultdict(
            lambda: {"total": 0, "over25": 0, "over35": 0, "over45": 0}
        )
        team_stats = defaultdict(
            lambda: {"total": 0, "over25": 0, "over35": 0, "over45": 0}
        )

        skipped = 0

        for game1, game2 in pairs:
            # Need both scores
            if None in (game1.score_home, game1.score_away,
                        game2.score_home, game2.score_away):
                skipped += 1
                continue

            # Determine loser of game1 (skip draws)
            if game1.score_home == game1.score_away:
                skipped += 1
                continue

            if game1.score_home < game1.score_away:
                loser      = game1.player_home
                opponent   = game1.player_away
                score_winner = game1.score_away
                score_loser  = game1.score_home
                loser_team    = game1.team_home
                opponent_team = game1.team_away
                loser_goals_g2 = (
                    game2.score_home if game2.player_home == loser else game2.score_away
                )
            else:
                loser      = game1.player_away
                opponent   = game1.player_home
                score_winner = game1.score_home
                score_loser  = game1.score_away
                loser_team    = game1.team_away
                opponent_team = game1.team_home
                loser_goals_g2 = (
                    game2.score_away if game2.player_away == loser else game2.score_home
                )

            loss_type    = classify_loss(score_winner, score_loser)
            total_goals_g1 = score_winner + score_loser
            over25_hit   = loser_goals_g2 > 2
            over35_hit   = loser_goals_g2 > 3
            over45_hit   = loser_goals_g2 > 4
            hour  = game2.hour_of_day if game2.hour_of_day is not None else 12
            dow   = game2.day_of_week if game2.day_of_week is not None else 0

            # Calcular streak de derrotas ANTES deste jogo
            prev_losses = player_loss_history[loser]
            streak_before = 0
            for was_loss in reversed(prev_losses):
                if was_loss:
                    streak_before += 1
                else:
                    break

            row = {
                "loser": loser,
                "opponent": opponent,
                "loser_team": loser_team,
                "opponent_team": opponent_team,
                "loss_type": loss_type,
                "total_goals_g1": total_goals_g1,
                "loser_goals_g1": score_loser,
                "loser_goals_g2": loser_goals_g2,
                "over25_hit": over25_hit,
                "over35_hit": over35_hit,
                "over45_hit": over45_hit,
                "hour": hour,
                "dow": dow,
                "score_g1": f"{score_winner}-{score_loser}",
                "time": game1.started_at,
                "streak_before": streak_before,
            }
            results.append(row)

            # Atualizar historico de perdas do jogador (para proximo streak)
            player_loss_history[loser].append(True)  # este jogo foi derrota
            # Tambem registrar a partida do vencedor (vitoria = False)
            player_loss_history[opponent].append(False)

            # -- Layer: player geral ----------------------------------------
            player_stats[loser]["total"]  += 1
            player_stats[loser]["over25"] += int(over25_hit)
            player_stats[loser]["over35"] += int(over35_hit)
            player_stats[loser]["over45"] += int(over45_hit)
            player_stats[loser]["goals"].append(loser_goals_g2)

            # -- Layer: player + time ---------------------------------------
            if loser_team:
                k = (loser, loser_team)
                player_team_stats[k]["total"]  += 1
                player_team_stats[k]["over25"] += int(over25_hit)
                player_team_stats[k]["over35"] += int(over35_hit)
                player_team_stats[k]["over45"] += int(over45_hit)
                player_team_stats[k]["goals"].append(loser_goals_g2)

            # -- Layer: H2H player — X perde para Y ------------------------
            k = (loser, opponent)
            h2h_stats[k]["total"]  += 1
            h2h_stats[k]["over25"] += int(over25_hit)
            h2h_stats[k]["over35"] += int(over35_hit)
            h2h_stats[k]["over45"] += int(over45_hit)
            h2h_stats[k]["goals"].append(loser_goals_g2)

            # -- Para forma recente -----------------------------------------
            player_history[loser].append({
                "time":   game1.started_at,
                "over25": over25_hit,
                "over35": over35_hit,
                "over45": over45_hit,
                "goals":  loser_goals_g2,
            })

            # -- Para H2H dominancia ----------------------------------------
            # opponent venceu loser nesse G1
            all_g1_results[(opponent, loser)] += 1

            # -- Y pos-vitoria ----------------------------------------------
            y_post_win_stats[opponent]["total"]  += 1
            y_post_win_stats[opponent]["over25"] += int(over25_hit)
            y_post_win_stats[opponent]["over35"] += int(over35_hit)
            y_post_win_stats[opponent]["over45"] += int(over45_hit)
            y_post_win_stats[opponent]["goals"].append(loser_goals_g2)

            # -- Gols totais G1 bucket --------------------------------------
            g1_bucket = f"{total_goals_g1}"
            goals_g1_stats[g1_bucket]["total"]  += 1
            goals_g1_stats[g1_bucket]["over25"] += int(over25_hit)
            goals_g1_stats[g1_bucket]["over35"] += int(over35_hit)
            goals_g1_stats[g1_bucket]["over45"] += int(over45_hit)

            # -- Gols do PERDEDOR em G1 bucket (fator mais importante) ------
            lg1_key = min(score_loser, 5)  # agrupa 5+ juntos
            loser_goals_g1_stats[lg1_key]["total"]  += 1
            loser_goals_g1_stats[lg1_key]["over25"] += int(over25_hit)
            loser_goals_g1_stats[lg1_key]["over35"] += int(over35_hit)
            loser_goals_g1_stats[lg1_key]["over45"] += int(over45_hit)

            # -- Tipo de derrota --------------------------------------------
            loss_type_stats[loss_type]["total"]  += 1
            loss_type_stats[loss_type]["over25"] += int(over25_hit)
            loss_type_stats[loss_type]["over35"] += int(over35_hit)
            loss_type_stats[loss_type]["over45"] += int(over45_hit)

            hour_slot = f"{(hour // 6) * 6:02d}-{(hour // 6) * 6 + 6:02d}h"
            hour_stats[hour_slot]["total"]  += 1
            hour_stats[hour_slot]["over25"] += int(over25_hit)
            hour_stats[hour_slot]["over35"] += int(over35_hit)
            hour_stats[hour_slot]["over45"] += int(over45_hit)

            dow_name = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sab", "Dom"][dow]
            dow_stats[dow_name]["total"]  += 1
            dow_stats[dow_name]["over25"] += int(over25_hit)
            dow_stats[dow_name]["over35"] += int(over35_hit)
            dow_stats[dow_name]["over45"] += int(over45_hit)

            if loser_team:
                team_stats[loser_team]["total"]  += 1
                team_stats[loser_team]["over25"] += int(over25_hit)
                team_stats[loser_team]["over35"] += int(over35_hit)
                team_stats[loser_team]["over45"] += int(over45_hit)

        total = len(results)
        if total == 0:
            print("[ERRO] Nenhum resultado com dados completos.")
            return

        print(f"Analisados: {total:,} pares  |  Ignorados (sem score): {skipped}\n")

        # ==================================================================
        # Post-loop: computar forma recente e dominancia H2H
        # ==================================================================

        # Forma recente: para cada player, sort por tempo e pegar ultimas 20
        recent_form_stats = {}
        for player, history in player_history.items():
            history.sort(
                key=lambda x: x["time"] if x["time"] else datetime.min.replace(tzinfo=timezone.utc)
            )
            last20 = history[-20:]
            if len(last20) >= 10:
                n = len(last20)
                recent_form_stats[player] = {
                    "n": n,
                    "over25": sum(e["over25"] for e in last20),
                    "over35": sum(e["over35"] for e in last20),
                    "over45": sum(e["over45"] for e in last20),
                    "rate25": sum(e["over25"] for e in last20) / n,
                    "rate35": sum(e["over35"] for e in last20) / n,
                    "rate45": sum(e["over45"] for e in last20) / n,
                    "avg_goals": sum(e["goals"] for e in last20) / n,
                }

        # H2H dominancia: taxa de vitoria do perdedor vs esse oponente
        # all_g1_results[(winner, loser)] = vezes que winner venceu loser
        h2h_win_rates = {}
        for (w, l), count in all_g1_results.items():
            l_wins = all_g1_results.get((l, w), 0)
            total_h2h = count + l_wins
            if total_h2h >= 5:
                h2h_win_rates[(l, w)] = {
                    "l_wins": l_wins,
                    "w_wins": count,
                    "total": total_h2h,
                    "l_win_rate": l_wins / total_h2h,
                }

        # ==================================================================
        # Global stats
        # ==================================================================
        global_over25 = sum(r["over25_hit"] for r in results)
        global_over35 = sum(r["over35_hit"] for r in results)
        global_over45 = sum(r["over45_hit"] for r in results)
        global_rate25 = global_over25 / total
        global_rate35 = global_over35 / total
        global_rate45 = global_over45 / total
        avg_goals = sum(r["loser_goals_g2"] for r in results) / total

        ci25 = wilson_confidence_interval(global_over25, total)
        ci35 = wilson_confidence_interval(global_over35, total)
        ci45 = wilson_confidence_interval(global_over45, total)

        method_validated = global_rate25 > 0.55 and ci25[0] > 0.50

        print("-" * 65)
        print("RESULTADO GLOBAL")
        print("-" * 65)
        print(f"  Over 2.5 apos derrota: {global_over25}/{total} "
              f"({global_rate25:.1%})  IC95%: [{ci25[0]:.1%}-{ci25[1]:.1%}]")
        print(f"  Over 3.5 apos derrota: {global_over35}/{total} "
              f"({global_rate35:.1%})  IC95%: [{ci35[0]:.1%}-{ci35[1]:.1%}]")
        print(f"  Over 4.5 apos derrota: {global_over45}/{total} "
              f"({global_rate45:.1%})  IC95%: [{ci45[0]:.1%}-{ci45[1]:.1%}]")
        print(f"  Media de gols do perdedor em G2: {avg_goals:.2f}")
        print()
        if method_validated:
            print("  METODO VALIDADO - edge estatistico confirmado")
        else:
            print("  METODO NAO VALIDADO - taxa abaixo de 55% ou IC inclui 50%")

        # -- ROI Simulation -------------------------------------------------
        print("\n" + "-" * 65)
        print("ROI SIMULATION (odds media @1.80 over2.5 | @2.50 over3.5 | @4.00 over4.5)")
        print("-" * 65)

        for label, odds, rate, hits in [
            ("Over 2.5 @1.80", 1.80, global_rate25, global_over25),
            ("Over 3.5 @2.50", 2.50, global_rate35, global_over35),
            ("Over 4.5 @4.00", 4.00, global_rate45, global_over45),
        ]:
            n = len(results)
            # ROI por unidade: (ganhos - perdas) / total apostado
            # ganhos = hits * (odds - 1), perdas = (n - hits) * 1
            profit_units = hits * (odds - 1) - (n - hits)
            roi_pct = profit_units / n * 100  # ROI % por unidade apostada
            breakeven = 1 / odds
            sign = "+" if roi_pct >= 0 else ""
            print(f"  {label}: Win: {rate:.1%}  ROI: {sign}{roi_pct:.1f}%/u  "
                  f"Breakeven: {breakeven:.1%}  Lucro: {profit_units:+.0f}u em {n} apostas")

        # -- Por tipo de derrota --------------------------------------------
        print("\n" + "-" * 65)
        print("POR TIPO DE DERROTA")
        print("-" * 65)
        for lt, st in sorted(loss_type_stats.items(), key=lambda x: -x[1]["total"]):
            n = st["total"]
            if n < 5:
                continue
            r25 = st["over25"] / n
            r35 = st["over35"] / n
            r45 = st["over45"] / n
            ci = wilson_confidence_interval(st["over25"], n)
            print(f"  {lt:<22} n={n:<6} "
                  f"O2.5:{r25:.1%}  O3.5:{r35:.1%}  O4.5:{r45:.1%}  "
                  f"IC:[{ci[0]:.1%}-{ci[1]:.1%}]")

        # -- Por faixa horaria ----------------------------------------------
        print("\n" + "-" * 65)
        print("POR FAIXA HORARIA")
        print("-" * 65)
        for slot, st in sorted(hour_stats.items()):
            n = st["total"]
            if n < 10:
                continue
            r25 = st["over25"] / n
            r35 = st["over35"] / n
            print(f"  {slot}  n={n:<6} O2.5:{r25:.1%}  O3.5:{r35:.1%}")

        # -- Por dia da semana ----------------------------------------------
        print("\n" + "-" * 65)
        print("POR DIA DA SEMANA")
        print("-" * 65)
        for dname, st in sorted(dow_stats.items()):
            n = st["total"]
            if n < 10:
                continue
            r25 = st["over25"] / n
            r35 = st["over35"] / n
            print(f"  {dname}   n={n:<6} O2.5:{r25:.1%}  O3.5:{r35:.1%}")

        # -- Gols totais G1 como preditor -----------------------------------
        print("\n" + "-" * 65)
        print("GOLS TOTAIS G1 → PREDICAO G2 (gols do perdedor em G2)")
        print("-" * 65)
        for g1_total in sorted(goals_g1_stats.keys(), key=lambda x: int(x)):
            st = goals_g1_stats[g1_total]
            n = st["total"]
            if n < 10:
                continue
            r25 = st["over25"] / n
            r35 = st["over35"] / n
            r45 = st["over45"] / n
            print(f"  G1={g1_total} gols  n={n:<6} O2.5:{r25:.1%}  O3.5:{r35:.1%}  O4.5:{r45:.1%}")

        # -- Gols do perdedor em G1 (FATOR MAIS IMPORTANTE) ----------------
        print("\n" + "=" * 65)
        print("GOLS DO PERDEDOR EM G1 → PREDICAO G2 (FATOR MAIS IMPORTANTE)")
        print("  Quanto mais o perdedor marcou em G1, maior o over em G2")
        print("=" * 65)
        for lg1_key in sorted(loser_goals_g1_stats.keys()):
            st = loser_goals_g1_stats[lg1_key]
            n = st["total"]
            if n < 5:
                continue
            r25 = st["over25"] / n
            r35 = st["over35"] / n
            r45 = st["over45"] / n
            ci = wilson_confidence_interval(st["over25"], n)
            label = f"{lg1_key}+" if lg1_key == 5 else str(lg1_key)
            flag = " *** MELHOR" if r25 >= 0.60 else ""
            print(f"  Perdedor marcou {label} gol(s)  n={n:<6} "
                  f"O2.5:{r25:.1%}  O3.5:{r35:.1%}  O4.5:{r45:.1%}  "
                  f"IC:[{ci[0]:.1%}-{ci[1]:.1%}]{flag}")

        # -- Analise de streak de derrotas ----------------------------------
        print("\n" + "-" * 65)
        print("STREAK DE DERROTAS CONSECUTIVAS ANTES DO JOGO")
        print("  Mais derrotas seguidas = menor taxa de over")
        print("-" * 65)
        streak_stats: dict = defaultdict(lambda: {"total": 0, "over25": 0, "over35": 0, "over45": 0})
        for r in results:
            sb = r.get("streak_before", 0)
            streak_key = min(sb, 5)  # agrupa 5+ juntos
            streak_stats[streak_key]["total"]  += 1
            streak_stats[streak_key]["over25"] += int(r["over25_hit"])
            streak_stats[streak_key]["over35"] += int(r["over35_hit"])
            streak_stats[streak_key]["over45"] += int(r["over45_hit"])

        for sk in sorted(streak_stats.keys()):
            st = streak_stats[sk]
            n = st["total"]
            if n < 5:
                continue
            r25 = st["over25"] / n
            r35 = st["over35"] / n
            label = f"{sk}+" if sk == 5 else str(sk)
            flag = " *** EVITAR" if r25 < 0.45 else ""
            print(f"  Streak {label} derrotas  n={n:<6} O2.5:{r25:.1%}  O3.5:{r35:.1%}{flag}")

        # -- Top jogadores gerais -------------------------------------------
        MIN_PLAYER = 10
        print("\n" + "-" * 65)
        print(f"TOP JOGADORES (min {MIN_PLAYER} jogos apos derrota)")
        print("-" * 65)
        reliable = [(p, s) for p, s in player_stats.items() if s["total"] >= MIN_PLAYER]
        top = sorted(reliable, key=lambda x: x[1]["over25"] / x[1]["total"], reverse=True)[:15]

        print("  CONFIAVEL - maior taxa over 2.5:")
        print(f"  {'Jogador':<22} {'O2.5':>10}  {'O3.5':>10}  {'O4.5':>10}  Avg  Streak")
        print("  " + "-" * 72)
        for name, st in top:
            r25 = st["over25"] / st["total"]
            r35 = st["over35"] / st["total"]
            r45 = st["over45"] / st["total"]
            avg_g = sum(st["goals"]) / len(st["goals"])
            # Streak: consecutivos over25 recentes
            hist = player_history[name]
            hist.sort(key=lambda x: x["time"] if x["time"] else datetime.min.replace(tzinfo=timezone.utc))
            streak = 0
            for e in reversed(hist):
                if e["over25"]:
                    streak += 1
                else:
                    break
            form = recent_form_stats.get(name, {})
            form_str = f" (forma:{form.get('rate25', 0):.0%})" if form else ""
            print(f"  {name:<22} {st['over25']:>3}/{st['total']:<6} {r25:.1%}"
                  f"  {st['over35']:>3}/{st['total']:<6} {r35:.1%}"
                  f"  {st['over45']:>3}/{st['total']:<6} {r45:.1%}"
                  f"  {avg_g:.1f}  streak={streak}{form_str}")

        print("\n  EVITAR - menor taxa:")
        bottom = sorted(reliable, key=lambda x: x[1]["over25"] / x[1]["total"])[:10]
        for name, st in bottom:
            r25 = st["over25"] / st["total"]
            print(f"    {name:<22} {st['over25']:>3}/{st['total']:<6} {r25:.1%}")

        # -- H2H player (min 15) --------------------------------------------
        MIN_H2H = 15
        print("\n" + "=" * 65)
        print(f"TOP COMBINACOES H2H (min {MIN_H2H} jogos) - X perde para Y")
        print("=" * 65)

        h2h_reliable = [
            (pair, st) for pair, st in h2h_stats.items() if st["total"] >= MIN_H2H
        ]
        h2h_top = sorted(h2h_reliable, key=lambda x: x[1]["over25"] / x[1]["total"], reverse=True)[:20]

        print(f"\n  MELHORES:")
        print(f"  {'Perdedor':<20} {'Oponente':<20} {'O2.5':>10}  {'O3.5':>10}  {'O4.5':>10}")
        print("  " + "-" * 72)
        for (loser, opp), st in h2h_top:
            r25 = st["over25"] / st["total"]
            r35 = st["over35"] / st["total"]
            r45 = st["over45"] / st["total"]
            ci = wilson_confidence_interval(st["over25"], st["total"])
            dom = h2h_win_rates.get((loser, opp), {})
            dom_str = f" (win_rate:{dom.get('l_win_rate', 0):.0%})" if dom else ""
            flag = " ***" if r25 >= 0.70 and ci[0] >= 0.55 else ""
            print(f"  {loser:<20} {opp:<20} {st['over25']:>3}/{st['total']:<6} {r25:.1%}"
                  f"  {st['over35']:>3}/{st['total']:<6} {r35:.1%}"
                  f"  {st['over45']:>3}/{st['total']:<6} {r45:.1%}"
                  f"{dom_str}{flag}")

        h2h_bottom = sorted(h2h_reliable, key=lambda x: x[1]["over25"] / x[1]["total"])[:10]
        print(f"\n  EVITAR (baixa taxa pos-derrota):")
        for (loser, opp), st in h2h_bottom:
            r25 = st["over25"] / st["total"]
            print(f"    {loser:<20} {opp:<20} {st['over25']:>3}/{st['total']:<6} {r25:.1%}")

        print(f"\n  Total pares H2H com >= {MIN_H2H} jogos: {len(h2h_reliable)}")

        # -- H2H dominancia insights ----------------------------------------
        print("\n" + "-" * 65)
        print("H2H DOMINANCIA (taxa de vitoria do perdedor vs oponente em G1)")
        print("  Alta dominancia inversa (perdedor raramente vence) = mais motivacao")
        print("-" * 65)
        dominated_pairs = [
            (pair, d) for pair, d in h2h_win_rates.items()
            if d["total"] >= 10 and d["l_win_rate"] <= 0.25
        ]
        dominated_pairs.sort(key=lambda x: x[1]["l_win_rate"])
        for (loser, opponent), d in dominated_pairs[:15]:
            h2h = h2h_stats.get((loser, opponent), {})
            r25_str = ""
            if h2h and h2h.get("total", 0) >= 5:
                r25_str = f"  O2.5:{h2h['over25']/h2h['total']:.1%}"
            print(f"  {loser:<20} win_rate:{d['l_win_rate']:.0%} vs {opponent:<20} "
                  f"({d['l_wins']}/{d['total']}){r25_str}")

        # -- Y pos-vitoria --------------------------------------------------
        print("\n" + "-" * 65)
        print("Y POS-VITORIA (quando Y ganha G1, perdedor marca X em G2)")
        print("  Alto = Y tende a ceder gols apos ganhar = bom para over")
        print("-" * 65)
        y_reliable = [(opp, st) for opp, st in y_post_win_stats.items() if st["total"] >= 15]
        y_top = sorted(y_reliable, key=lambda x: x[1]["over25"] / x[1]["total"], reverse=True)[:15]
        print(f"  {'Vencedor G1':<22} {'O2.5':>10}  {'O3.5':>10}  Avg gols cedidos")
        print("  " + "-" * 60)
        for opp, st in y_top:
            r25 = st["over25"] / st["total"]
            r35 = st["over35"] / st["total"]
            avg_g = sum(st["goals"]) / len(st["goals"])
            print(f"  {opp:<22} {st['over25']:>3}/{st['total']:<6} {r25:.1%}"
                  f"  {st['over35']:>3}/{st['total']:<6} {r35:.1%}  {avg_g:.2f}")

        # -- Times com maior over 2.5 ---------------------------------------
        if team_stats:
            print("\n" + "-" * 65)
            print("TIMES COM MAIOR OVER 2.5 (min 10 jogos)")
            print("-" * 65)
            team_ranked = [(t, s) for t, s in team_stats.items() if s["total"] >= 10]
            team_ranked.sort(key=lambda x: x[1]["over25"] / x[1]["total"], reverse=True)
            for team, st in team_ranked[:15]:
                r25 = st["over25"] / st["total"]
                r35 = st["over35"] / st["total"]
                print(f"  {team:<25} {st['over25']:>4}/{st['total']:<6} O2.5:{r25:.1%}  O3.5:{r35:.1%}")

        # ==================================================================
        # Salvar stats no banco
        # ==================================================================
        print("\n" + "-" * 65)
        print("Salvando stats no banco de dados...")

        # Global
        await method_stats_repo.upsert("global", {
            "total_samples": total,
            "over25_hits": global_over25,
            "over35_hits": global_over35,
            "over45_hits": global_over45,
            "hit_rate_25": global_rate25,
            "hit_rate_35": global_rate35,
            "hit_rate_45": global_rate45,
            "avg_goals": avg_goals,
        }, stat_type="global")

        # Tipo de derrota
        for lt, st in loss_type_stats.items():
            if st["total"] >= 5:
                n = st["total"]
                await method_stats_repo.upsert(f"loss_{lt}", {
                    "total_samples": n,
                    "over25_hits": st["over25"],
                    "over35_hits": st["over35"],
                    "over45_hits": st["over45"],
                    "hit_rate_25": st["over25"] / n,
                    "hit_rate_35": st["over35"] / n,
                    "hit_rate_45": st["over45"] / n,
                }, stat_type="loss_type")

        # Hora
        for slot, st in hour_stats.items():
            if st["total"] >= 10:
                n = st["total"]
                await method_stats_repo.upsert(f"time_{slot}", {
                    "total_samples": n,
                    "over25_hits": st["over25"],
                    "over35_hits": st["over35"],
                    "over45_hits": st["over45"],
                    "hit_rate_25": st["over25"] / n,
                    "hit_rate_35": st["over35"] / n,
                    "hit_rate_45": st["over45"] / n,
                }, stat_type="time_slot")

        # H2H player (min 15)
        h2h_saved = 0
        MIN_H2H_SAVE = 5  # salvar com min 5, engine so usa com 15
        for (loser, opponent), st in h2h_stats.items():
            if st["total"] >= MIN_H2H_SAVE:
                n = st["total"]
                key = f"h2h_{loser}_vs_{opponent}"
                await method_stats_repo.upsert(key, {
                    "total_samples": n,
                    "over25_hits": st["over25"],
                    "over35_hits": st["over35"],
                    "over45_hits": st["over45"],
                    "hit_rate_25": st["over25"] / n,
                    "hit_rate_35": st["over35"] / n,
                    "hit_rate_45": st["over45"] / n,
                    "avg_goals": sum(st["goals"]) / n,
                }, stat_type="h2h_player")
                h2h_saved += 1

        # Player + time (min 5)
        pt_saved = 0
        for (loser, team), st in player_team_stats.items():
            if st["total"] >= 5:
                n = st["total"]
                key = f"player_team_{loser}_{team}"
                await method_stats_repo.upsert(key, {
                    "total_samples": n,
                    "over25_hits": st["over25"],
                    "over35_hits": st["over35"],
                    "over45_hits": st["over45"],
                    "hit_rate_25": st["over25"] / n,
                    "hit_rate_35": st["over35"] / n,
                    "hit_rate_45": st["over45"] / n,
                }, stat_type="player_team")
                pt_saved += 1

        # Forma recente (ultimas 20)
        rf_saved = 0
        for player, rf in recent_form_stats.items():
            n = rf["n"]
            key = f"recent_form_{player}"
            await method_stats_repo.upsert(key, {
                "total_samples": n,
                "over25_hits": rf["over25"],
                "over35_hits": rf["over35"],
                "over45_hits": rf["over45"],
                "hit_rate_25": rf["rate25"],
                "hit_rate_35": rf["rate35"],
                "hit_rate_45": rf["rate45"],
                "avg_goals": rf["avg_goals"],
            }, stat_type="recent_form")
            rf_saved += 1

        # Y pos-vitoria (min 10)
        ypw_saved = 0
        for opponent, st in y_post_win_stats.items():
            if st["total"] >= 10:
                n = st["total"]
                key = f"y_post_win_{opponent}"
                await method_stats_repo.upsert(key, {
                    "total_samples": n,
                    "over25_hits": st["over25"],
                    "over35_hits": st["over35"],
                    "over45_hits": st["over45"],
                    "hit_rate_25": st["over25"] / n,
                    "hit_rate_35": st["over35"] / n,
                    "hit_rate_45": st["over45"] / n,
                    "avg_goals": sum(st["goals"]) / n,
                }, stat_type="y_post_win")
                ypw_saved += 1

        # H2H dominancia (min 5 jogos totais)
        dom_saved = 0
        for (loser, opponent), d in h2h_win_rates.items():
            if d["total"] >= 5:
                key = f"h2h_dom_{loser}_vs_{opponent}"
                await method_stats_repo.upsert(key, {
                    "total_samples": d["total"],
                    "hit_rate_25": d["l_win_rate"],  # taxa de vitoria do perdedor
                }, stat_type="h2h_dominance")
                dom_saved += 1

        # Gols do perdedor em G1 (por bucket 0-5+)
        g1goals_saved = 0
        for lg1_key, st in loser_goals_g1_stats.items():
            if st["total"] >= 5:
                n = st["total"]
                key = f"g1_goals_{lg1_key}"
                await method_stats_repo.upsert(key, {
                    "total_samples": n,
                    "over25_hits": st["over25"],
                    "over35_hits": st["over35"],
                    "over45_hits": st["over45"],
                    "hit_rate_25": st["over25"] / n,
                    "hit_rate_35": st["over35"] / n,
                    "hit_rate_45": st["over45"] / n,
                }, stat_type="g1_goals")
                g1goals_saved += 1

        print(f"  Stats salvas:")
        print(f"     H2H player: {h2h_saved}")
        print(f"     Player+time: {pt_saved}")
        print(f"     Forma recente: {rf_saved}")
        print(f"     Y pos-vitoria: {ypw_saved}")
        print(f"     H2H dominancia: {dom_saved}")
        print(f"     Gols perdedor G1: {g1goals_saved}")
        print("  Sistema pronto para operar.")

        # -- Final recommendation -------------------------------------------
        print("\n" + "=" * 65)
        if method_validated:
            print("METODO VALIDADO")
            print(f"   Taxa global O2.5: {global_rate25:.1%}  IC95%: [{ci25[0]:.1%}-{ci25[1]:.1%}]")
            print(f"   Taxa global O3.5: {global_rate35:.1%}  IC95%: [{ci35[0]:.1%}-{ci35[1]:.1%}]")
            print(f"   Taxa global O4.5: {global_rate45:.1%}  IC95%: [{ci45[0]:.1%}-{ci45[1]:.1%}]")
            print(f"\n   Proximo passo:")
            print(f"   1. python -m src.main  (modo shadow por 2 semanas)")
            print(f"   2. Depois ativar apostas reais")
        else:
            print("METODO NAO VALIDADO nos dados historicos coletados")
            print(f"   Taxa: {global_rate25:.1%}  (IC inclui 50%: {ci25[0]:.1%}-{ci25[1]:.1%})")
            print(f"   Recomendacao: coletar mais dados ou revisar parametros")
        print("=" * 65)


if __name__ == "__main__":
    asyncio.run(main())
