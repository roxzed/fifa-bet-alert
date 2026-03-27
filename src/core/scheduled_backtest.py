"""Backtest agendado: roda semanalmente sobre dados coletados e notifica via Telegram.

Executa uma versao resumida do backtest completo (scripts/backtest.py) e envia
um relatorio compacto via Telegram com as metricas principais.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from loguru import logger

from src.core.probability import classify_loss, wilson_confidence_interval, simulate_roi


class ScheduledBacktest:
    """Roda backtest periodico e notifica resultados via Telegram."""

    def __init__(self, match_repo, method_stats_repo, notifier):
        self.matches = match_repo
        self.method_stats = method_stats_repo
        self.notifier = notifier

    async def run_and_notify(self) -> dict:
        """Executa backtest completo e envia relatorio via Telegram.

        Returns dict com metricas principais para uso programatico.
        """
        report = await self._run_backtest()

        if report["status"] == "error":
            logger.error(f"Backtest failed: {report.get('message', 'unknown')}")
            return report

        if report["total_pairs"] < 50:
            logger.info(f"Backtest: apenas {report['total_pairs']} pares (min 50)")
            return report

        # Enviar relatorio compacto via Telegram
        await self._send_telegram_report(report)

        # Atualizar method_stats no banco
        await self._update_method_stats(report)

        return report

    async def _run_backtest(self) -> dict:
        """Executa analise estatistica sobre todos os pares coletados."""
        try:
            pairs = await self.matches.get_all_pairs()
            if not pairs:
                return {"status": "error", "message": "Nenhum par encontrado"}

            player_stats: dict = defaultdict(
                lambda: {"total": 0, "over25": 0, "over35": 0, "over45": 0, "goals": []}
            )
            loss_type_stats: dict = defaultdict(
                lambda: {"total": 0, "over25": 0, "over35": 0, "over45": 0}
            )
            hour_stats: dict = defaultdict(
                lambda: {"total": 0, "over25": 0, "over35": 0, "over45": 0}
            )
            loser_goals_g1_stats: dict = defaultdict(
                lambda: {"total": 0, "over25": 0, "over35": 0, "over45": 0}
            )

            results = []
            skipped = 0

            for game1, game2 in pairs:
                if None in (game1.score_home, game1.score_away,
                            game2.score_home, game2.score_away):
                    skipped += 1
                    continue

                if game1.score_home == game1.score_away:
                    skipped += 1
                    continue

                if game1.score_home < game1.score_away:
                    loser = game1.player_home
                    score_winner = game1.score_away
                    score_loser = game1.score_home
                    loser_goals_g2 = (
                        game2.score_home if game2.player_home == loser else game2.score_away
                    )
                else:
                    loser = game1.player_away
                    score_winner = game1.score_home
                    score_loser = game1.score_away
                    loser_goals_g2 = (
                        game2.score_away if game2.player_away == loser else game2.score_home
                    )

                loss_type = classify_loss(score_winner, score_loser)
                over25_hit = loser_goals_g2 > 2
                over35_hit = loser_goals_g2 > 3
                over45_hit = loser_goals_g2 > 4
                hour = game2.hour_of_day if game2.hour_of_day is not None else 12

                # ML: perdedor venceu G2?
                if game2.player_home == loser:
                    opp_goals_g2 = game2.score_away
                else:
                    opp_goals_g2 = game2.score_home
                ml_hit = loser_goals_g2 > opp_goals_g2

                # Determinar opponent
                if game1.score_home < game1.score_away:
                    opponent = game1.player_away
                else:
                    opponent = game1.player_home

                results.append({
                    "loser": loser,
                    "opponent": opponent,
                    "loser_goals_g2": loser_goals_g2,
                    "over25_hit": over25_hit,
                    "over35_hit": over35_hit,
                    "over45_hit": over45_hit,
                    "ml_hit": ml_hit,
                    "loss_type": loss_type,
                    "loser_goals_g1": score_loser,
                    "margin": score_winner - score_loser,
                })

                # Acumular stats
                player_stats[loser]["total"] += 1
                player_stats[loser]["over25"] += int(over25_hit)
                player_stats[loser]["over35"] += int(over35_hit)
                player_stats[loser]["over45"] += int(over45_hit)
                player_stats[loser]["goals"].append(loser_goals_g2)

                loss_type_stats[loss_type]["total"] += 1
                loss_type_stats[loss_type]["over25"] += int(over25_hit)
                loss_type_stats[loss_type]["over35"] += int(over35_hit)

                hour_slot = f"{(hour // 6) * 6:02d}-{(hour // 6) * 6 + 6:02d}h"
                hour_stats[hour_slot]["total"] += 1
                hour_stats[hour_slot]["over25"] += int(over25_hit)

                lg1_key = min(score_loser, 5)
                loser_goals_g1_stats[lg1_key]["total"] += 1
                loser_goals_g1_stats[lg1_key]["over25"] += int(over25_hit)
                loser_goals_g1_stats[lg1_key]["over35"] += int(over35_hit)
                loser_goals_g1_stats[lg1_key]["over45"] += int(over45_hit)

            total = len(results)
            if total == 0:
                return {"status": "error", "message": "Nenhum resultado valido"}

            global_over25 = sum(r["over25_hit"] for r in results)
            global_over35 = sum(r["over35_hit"] for r in results)
            global_over45 = sum(r["over45_hit"] for r in results)
            global_ml = sum(r["ml_hit"] for r in results)
            avg_goals = sum(r["loser_goals_g2"] for r in results) / total

            ci25 = wilson_confidence_interval(global_over25, total)
            ci35 = wilson_confidence_interval(global_over35, total)
            ci45 = wilson_confidence_interval(global_over45, total)
            ci_ml = wilson_confidence_interval(global_ml, total)

            method_validated = (global_over25 / total) > 0.50 and ci25[0] > 0.48

            # Top/bottom players (Over)
            reliable = [(p, s) for p, s in player_stats.items() if s["total"] >= 10]
            top_players = sorted(
                reliable, key=lambda x: x[1]["over25"] / x[1]["total"], reverse=True
            )[:5]
            bottom_players = sorted(
                reliable, key=lambda x: x[1]["over25"] / x[1]["total"]
            )[:5]

            # ML stats por jogador
            ml_player_stats: dict = defaultdict(lambda: {"total": 0, "wins": 0})
            for r in results:
                ml_player_stats[r["loser"]]["total"] += 1
                ml_player_stats[r["loser"]]["wins"] += int(r["ml_hit"])

            ml_reliable = [(p, s) for p, s in ml_player_stats.items() if s["total"] >= 10]
            ml_top = sorted(ml_reliable, key=lambda x: x[1]["wins"] / x[1]["total"], reverse=True)[:5]
            ml_bottom = sorted(ml_reliable, key=lambda x: x[1]["wins"] / x[1]["total"])[:5]

            # ML por margem
            ml_margin_stats: dict = defaultdict(lambda: {"total": 0, "wins": 0})
            for r in results:
                m = min(r.get("margin", 1), 6)
                ml_margin_stats[m]["total"] += 1
                ml_margin_stats[m]["wins"] += int(r["ml_hit"])

            # ROI simulation
            roi_25 = (global_over25 * (1.80 - 1) - (total - global_over25)) / total * 100
            roi_35 = (global_over35 * (2.50 - 1) - (total - global_over35)) / total * 100
            roi_45 = (global_over45 * (4.00 - 1) - (total - global_over45)) / total * 100
            roi_ml = (global_ml * (2.60 - 1) - (total - global_ml)) / total * 100

            return {
                "status": "ok",
                "total_pairs": total,
                "skipped": skipped,
                "global_rate_25": global_over25 / total,
                "global_rate_35": global_over35 / total,
                "global_rate_45": global_over45 / total,
                "global_rate_ml": global_ml / total,
                "global_over25": global_over25,
                "global_over35": global_over35,
                "global_over45": global_over45,
                "global_ml": global_ml,
                "avg_goals": avg_goals,
                "ci25": ci25,
                "ci35": ci35,
                "ci45": ci45,
                "ci_ml": ci_ml,
                "method_validated": method_validated,
                "roi_25": roi_25,
                "roi_35": roi_35,
                "roi_45": roi_45,
                "roi_ml": roi_ml,
                "top_players": top_players,
                "bottom_players": bottom_players,
                "loss_type_stats": dict(loss_type_stats),
                "loser_goals_g1_stats": dict(loser_goals_g1_stats),
                "player_stats": dict(player_stats),
                "hour_stats": dict(hour_stats),
                "ml_top": ml_top,
                "ml_bottom": ml_bottom,
                "ml_margin_stats": dict(ml_margin_stats),
                "_results_raw": results,
            }

        except Exception as e:
            logger.error(f"Backtest error: {e}")
            return {"status": "error", "message": str(e)}

    async def _send_telegram_report(self, report: dict) -> None:
        """Envia relatorio resumido do backtest via Telegram."""
        total = report["total_pairs"]
        r25 = report["global_rate_25"]
        r35 = report["global_rate_35"]
        r45 = report["global_rate_45"]
        ci25 = report["ci25"]
        validated = report["method_validated"]

        status_icon = "\u2705" if validated else "\u26a0\ufe0f"
        status_text = "VALIDADO" if validated else "NAO VALIDADO"

        # Top players
        top_lines = []
        for name, st in report["top_players"]:
            rate = st["over25"] / st["total"]
            top_lines.append(f"  {name}: {rate:.0%} ({st['total']}j)")

        # Bottom players
        bottom_lines = []
        for name, st in report["bottom_players"]:
            rate = st["over25"] / st["total"]
            bottom_lines.append(f"  {name}: {rate:.0%} ({st['total']}j)")

        # Loss type breakdown
        lt_lines = []
        for lt, st in sorted(report["loss_type_stats"].items(), key=lambda x: -x[1]["total"]):
            if st["total"] >= 5:
                rate = st["over25"] / st["total"]
                lt_lines.append(f"  {lt}: {rate:.0%} (n={st['total']})")

        # ML stats
        r_ml = report.get("global_rate_ml", 0)
        ci_ml = report.get("ci_ml", (0, 0))

        # ML top players
        ml_top_lines = []
        for name, st in report.get("ml_top", []):
            rate = st["wins"] / st["total"]
            ml_top_lines.append(f"  {name}: {rate:.0%} ({st['total']}j)")

        # ROI
        roi25_sign = "+" if report["roi_25"] >= 0 else ""
        roi35_sign = "+" if report["roi_35"] >= 0 else ""
        roi_ml_sign = "+" if report.get("roi_ml", 0) >= 0 else ""

        msg = (
            f"{status_icon} <b>Backtest Semanal</b> — {status_text}\n\n"
            f"<b>Amostra:</b> {total:,} pares\n"
            f"<b>Media gols G2:</b> {report['avg_goals']:.2f}\n\n"
            f"<b>Taxas globais (Over):</b>\n"
            f"  O2.5: {r25:.1%} IC[{ci25[0]:.1%}-{ci25[1]:.1%}]\n"
            f"  O3.5: {r35:.1%}\n"
            f"  O4.5: {r45:.1%}\n\n"
            f"<b>Taxa ML (vitoria perdedor):</b>\n"
            f"  ML: {r_ml:.1%} IC[{ci_ml[0]:.1%}-{ci_ml[1]:.1%}]\n\n"
            f"<b>ROI simulado:</b>\n"
            f"  O2.5@1.80: {roi25_sign}{report['roi_25']:.1f}%\n"
            f"  O3.5@2.50: {roi35_sign}{report['roi_35']:.1f}%\n"
            f"  ML@2.60: {roi_ml_sign}{report.get('roi_ml', 0):.1f}%\n\n"
            f"<b>Por tipo derrota:</b>\n" + "\n".join(lt_lines) + "\n\n"
            f"<b>Top jogadores (O2.5):</b>\n" + "\n".join(top_lines) + "\n\n"
            f"<b>Top jogadores (ML):</b>\n" + "\n".join(ml_top_lines) + "\n\n"
            f"<b>Evitar (Over):</b>\n" + "\n".join(bottom_lines)
        )

        try:
            await self.notifier.send_message(msg)
            logger.info(f"Backtest report sent to Telegram ({total} pairs)")
        except Exception as e:
            logger.error(f"Failed to send backtest report: {e}")

    async def _update_method_stats(self, report: dict) -> None:
        """Atualiza method_stats no banco com dados do backtest."""
        try:
            total = report["total_pairs"]

            # Global
            await self.method_stats.upsert("global", {
                "total_samples": total,
                "over25_hits": report["global_over25"],
                "over35_hits": report["global_over35"],
                "over45_hits": report["global_over45"],
                "hit_rate_25": report["global_rate_25"],
                "hit_rate_35": report["global_rate_35"],
                "hit_rate_45": report["global_rate_45"],
                "avg_goals": report["avg_goals"],
            }, stat_type="global")

            # Loss types
            for lt, st in report["loss_type_stats"].items():
                if st["total"] >= 5:
                    n = st["total"]
                    await self.method_stats.upsert(f"loss_{lt}", {
                        "total_samples": n,
                        "over25_hits": st["over25"],
                        "over35_hits": st["over35"],
                        "over45_hits": st["over45"],
                        "hit_rate_25": st["over25"] / n,
                        "hit_rate_35": st["over35"] / n,
                        "hit_rate_45": st["over45"] / n,
                    }, stat_type="loss_type")

            # G1 loser goals
            for lg1_key, st in report["loser_goals_g1_stats"].items():
                if st["total"] >= 5:
                    n = st["total"]
                    await self.method_stats.upsert(f"g1_goals_{lg1_key}", {
                        "total_samples": n,
                        "over25_hits": st["over25"],
                        "over35_hits": st["over35"],
                        "over45_hits": st["over45"],
                        "hit_rate_25": st["over25"] / n,
                        "hit_rate_35": st["over35"] / n,
                        "hit_rate_45": st["over45"] / n,
                    }, stat_type="g1_goals")

            # Player stats
            for player, st in report["player_stats"].items():
                if st["total"] >= 10:
                    n = st["total"]
                    await self.method_stats.upsert(f"player_{player}", {
                        "total_samples": n,
                        "over25_hits": st["over25"],
                        "over35_hits": st["over35"],
                        "over45_hits": st["over45"],
                        "hit_rate_25": st["over25"] / n,
                        "hit_rate_35": st["over35"] / n,
                        "hit_rate_45": st["over45"] / n,
                        "avg_goals": sum(st["goals"]) / n,
                    }, stat_type="player")

            # Hour stats
            for slot, st in report["hour_stats"].items():
                if st["total"] >= 10:
                    n = st["total"]
                    await self.method_stats.upsert(f"time_{slot}", {
                        "total_samples": n,
                        "over25_hits": st["over25"],
                        "hit_rate_25": st["over25"] / n,
                    }, stat_type="time_slot")

            # === ML Stats ===
            results_list = report.get("_results_raw", [])
            if results_list:
                # ML player: taxa de vitoria ML por jogador
                ml_by_player: dict = defaultdict(lambda: {"total": 0, "wins": 0})
                # ML H2H: X perde para Y, X vence G2?
                ml_h2h: dict = defaultdict(lambda: {"total": 0, "wins": 0})
                # ML cedente: quando X vence G1, perdedor vence G2?
                ml_cedente: dict = defaultdict(lambda: {"total": 0, "wins": 0})

                for r in results_list:
                    loser = r["loser"]
                    opponent = r.get("opponent", "")
                    ml_hit = r.get("ml_hit", False)

                    ml_by_player[loser]["total"] += 1
                    ml_by_player[loser]["wins"] += int(ml_hit)

                    if opponent:
                        ml_h2h[(loser, opponent)]["total"] += 1
                        ml_h2h[(loser, opponent)]["wins"] += int(ml_hit)

                        ml_cedente[opponent]["total"] += 1
                        ml_cedente[opponent]["wins"] += int(ml_hit)

                # Salvar ML player stats
                for player, st in ml_by_player.items():
                    if st["total"] >= 10:
                        n = st["total"]
                        await self.method_stats.upsert(f"ml_player_{player}", {
                            "total_samples": n,
                            "over25_hits": st["wins"],
                            "hit_rate_25": st["wins"] / n,
                        }, stat_type="ml_player")

                # Salvar ML H2H stats
                ml_h2h_saved = 0
                for (loser, opp), st in ml_h2h.items():
                    if st["total"] >= 5:
                        n = st["total"]
                        await self.method_stats.upsert(f"ml_h2h_{loser}_vs_{opp}", {
                            "total_samples": n,
                            "over25_hits": st["wins"],
                            "hit_rate_25": st["wins"] / n,
                        }, stat_type="ml_h2h")
                        ml_h2h_saved += 1

                # Salvar ML cedente stats
                for opp, st in ml_cedente.items():
                    if st["total"] >= 15:
                        n = st["total"]
                        await self.method_stats.upsert(f"ml_cedente_{opp}", {
                            "total_samples": n,
                            "over25_hits": st["wins"],
                            "hit_rate_25": st["wins"] / n,
                        }, stat_type="ml_cedente")

                logger.info(
                    f"Backtest ML stats: {len(ml_by_player)} players, "
                    f"{ml_h2h_saved} H2H pairs, {len(ml_cedente)} cedentes"
                )

            logger.info("Backtest: method_stats atualizadas no banco")

        except Exception as e:
            logger.error(f"Backtest: falha ao atualizar method_stats: {e}")
