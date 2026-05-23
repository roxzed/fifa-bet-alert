"""Alert Engine V2: avalia oportunidades com StatsEngineV2 e envia para grupo M2."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from loguru import logger

from src.core.h2h_tier import compute_h2h_tier_v2


# Filtros calibrados em 2026-05-22 apos backtest sobre maio 2026.
# Justificativa: M2 saiu de pico +24u (06/05) para flat (+0.6u) ate 22/05.
# Backtest mostrou que 2 cortes cirurgicos recuperam +16.80u no mes.

# Horarios BRT onde C2 historicamente sangra. 03h BRT teve 27 tips com
# ROI -42.5% (-11.48u) em maio — fora da banda de variancia normal.
M2_C2_BAD_HOURS_BRT: frozenset[int] = frozenset({3})

# Combos H2H cronicamente negativos no M2 (player x opp x linha) que nao
# foram pegos pelo blocked_lines_v2 por terem amostra pequena ou volatil.
# 3 combos tohi4 que somaram -5.32u em 24 tips (ROI -22.2%) em maio.
M2_HARD_BLOCKED_COMBOS: frozenset[tuple[str, str, str]] = frozenset({
    ("tohi4", "Kavviro", "over15"),
    ("tohi4", "Snow", "over15"),
    ("tohi4", "Snow", "over25"),
})


class AlertEngineV2:
    """
    Recebe oportunidade do OddsMonitor, avalia com StatsEngineV2,
    e envia alerta para o grupo Telegram do Method 2.
    """

    def __init__(
        self, stats_engine_v2, alert_v2_repo, notifier_v2, blocked_repo_v2=None
    ) -> None:
        self.stats = stats_engine_v2
        self.alerts = alert_v2_repo
        self.notifier = notifier_v2
        # 2026-04-26: SHADOW protocol M2 — auto-block per (player, line)
        self.blocked = blocked_repo_v2

    async def evaluate_and_alert(
        self,
        return_match,
        game1_match,
        loser: str,
        winner: str,
        over25_odds: float,
        over35_odds: float | None,
        over45_odds: float | None = None,
        over15_odds: float | None = None,
        minutes_to_kickoff: int = 0,
        loser_goals_g1: int = 0,
        bet365_url: str = "",
    ) -> bool:
        """Avalia e envia alerta M2 se alguma camada disparar.

        Returns True se alerta foi criado no DB (independente do Telegram).
        """
        score_home = game1_match.score_home or 0
        score_away = game1_match.score_away or 0

        # Determinar times e scores
        if game1_match.player_home == loser:
            loser_team = game1_match.team_home or ""
            opponent_team = game1_match.team_away or ""
        else:
            loser_team = game1_match.team_away or ""
            opponent_team = game1_match.team_home or ""

        odds_dict = {
            "over15": over15_odds,
            "over25": over25_odds if over25_odds else None,
            "over35": over35_odds,
            "over45": over45_odds,
        }

        evaluation = await self.stats.evaluate_opportunity(
            loser=loser,
            opponent=winner,
            loser_team=loser_team,
            opp_team=opponent_team,
            odds_dict=odds_dict,
        )

        if not evaluation.should_alert:
            logger.debug(f"M2 no alert for {loser}: {evaluation.reason}")
            return False

        # Resolver odds da best_line
        line_odds_map = {
            "over15": over15_odds,
            "over25": over25_odds,
            "over35": over35_odds,
            "over45": over45_odds,
        }
        alert_odds = line_odds_map.get(evaluation.best_line) or 0.0

        # Label
        line_labels = {
            "over15": "Over 1.5", "over25": "Over 2.5",
            "over35": "Over 3.5", "over45": "Over 4.5",
        }
        alert_label = line_labels.get(evaluation.best_line, evaluation.best_line)

        # G1 score string
        if game1_match.player_home == loser:
            g1_score = f"{score_home}-{score_away}"
        else:
            g1_score = f"{score_away}-{score_home}"

        # Save to DB
        alert = await self.alerts.create(
            match_id=return_match.id,
            losing_player=loser,
            opponent_player=winner,
            game1_score=g1_score,
            camada=evaluation.camada,
            best_line=evaluation.best_line,
            over15_odds=over15_odds,
            over25_odds=over25_odds if over25_odds else None,
            over35_odds=over35_odds,
            over45_odds=over45_odds,
            prob=evaluation.prob,
            sample_size=evaluation.sample_size,
            prob_4elem=evaluation.prob_4elem or None,
            prob_3elem=evaluation.prob_3elem or None,
            sample_4elem=evaluation.sample_4elem or None,
            sample_3elem=evaluation.sample_3elem or None,
        )

        # Build Telegram message data
        kickoff = return_match.started_at
        alert_data = {
            "camada": evaluation.camada,
            "best_line": evaluation.best_line,
            "alert_label": f"{alert_label} gols {loser}",
            "alert_odds": alert_odds,
            "prob": evaluation.prob,
            "sample_size": evaluation.sample_size,
            "prob_4elem": evaluation.prob_4elem,
            "prob_3elem": evaluation.prob_3elem,
            "sample_4elem": evaluation.sample_4elem,
            "sample_3elem": evaluation.sample_3elem,
            "losing_player": loser,
            "loser_team": loser_team,
            "opponent_player": winner,
            "opponent_team": opponent_team,
            "game1_player_home": game1_match.player_home,
            "game1_player_away": game1_match.player_away,
            "game1_score_home": game1_match.score_home,
            "game1_score_away": game1_match.score_away,
            "game1_team_home": game1_match.team_home,
            "game1_team_away": game1_match.team_away,
            "return_player_home": return_match.player_home,
            "return_player_away": return_match.player_away,
            "return_team_home": return_match.team_home,
            "return_team_away": return_match.team_away,
            "kickoff_time": kickoff,
            "minutes_to_kickoff": minutes_to_kickoff,
            "over15_odds": over15_odds,
            "over25_odds": over25_odds,
            "over35_odds": over35_odds,
            "over45_odds": over45_odds,
            "bet365_url": bet365_url,
        }

        # H2H tier M2 — sempre computa (alimenta GIRANDO check + mostra letra na msg)
        tier_res = None
        if evaluation.best_line:
            try:
                tier_res = await compute_h2h_tier_v2(
                    self.alerts, self.blocked, loser, evaluation.best_line, winner
                )
                alert_data["h2h_tier"] = tier_res.tier
                alert_data["h2h_tier_n"] = tier_res.n
                alert_data["h2h_tier_roi"] = tier_res.roi
                alert_data["h2h_tier_pl"] = tier_res.pl
            except Exception as e:
                logger.warning(f"M2 compute_h2h_tier_v2 falhou ({loser}/{evaluation.best_line}/vs.{winner}): {e}")
                alert_data["h2h_tier"] = "?"

        # SHADOW protocol M2 granular — checa auto-block por (loser, line, opponent)
        shadow_suppressed = False
        if self.blocked is not None and evaluation.best_line:
            try:
                shadow_suppressed = await self.blocked.is_suppressed(
                    loser, evaluation.best_line, winner
                )
            except Exception as e:
                logger.warning(
                    f"M2 is_suppressed check failed for {loser}/{evaluation.best_line}/vs.{winner}: {e}"
                )

        # GIRANDO filter M2: tier D (ROI 0-5%, n>=3) suprime sem enviar
        girando_suppressed = False
        if not shadow_suppressed and tier_res is not None and tier_res.tier == "D":
            girando_suppressed = True
            logger.bind(category="alert_v2").info(
                f"M2 GIRANDO suppressed: {loser} {evaluation.best_line} vs {winner} "
                f"tier=D ROI={tier_res.roi:+.1f}% n={tier_res.n} — salvo sem enviar"
            )

        # M2 C2 BAD HOURS filter: 03h BRT sangrou -11.48u/27 tips em maio 2026
        bad_hour_suppressed = False
        if (not shadow_suppressed and not girando_suppressed
                and evaluation.camada == "C2"):
            kickoff_brt = kickoff
            if isinstance(kickoff, datetime):
                if kickoff.tzinfo is not None:
                    kickoff_brt = kickoff.astimezone(timezone(timedelta(hours=-3)))
                else:
                    kickoff_brt = kickoff - timedelta(hours=3)
                hour_brt = kickoff_brt.hour
                if hour_brt in M2_C2_BAD_HOURS_BRT:
                    bad_hour_suppressed = True
                    logger.bind(category="alert_v2").info(
                        f"M2 BAD_HOUR suppressed: {loser} {evaluation.best_line} vs "
                        f"{winner} — C2 {hour_brt:02d}h BRT bloqueada (drainer historico)"
                    )

        # M2 HARD BLOCKED COMBOS: combos cronicamente negativos manual blocklist
        hard_blocked_suppressed = False
        combo_key = (loser, winner, evaluation.best_line or "")
        if (not shadow_suppressed and not girando_suppressed and not bad_hour_suppressed
                and combo_key in M2_HARD_BLOCKED_COMBOS):
            hard_blocked_suppressed = True
            logger.bind(category="alert_v2").info(
                f"M2 HARD_BLOCKED suppressed: {loser} {evaluation.best_line} vs "
                f"{winner} — combo na blocklist manual"
            )

        suppressed = (shadow_suppressed or girando_suppressed
                      or bad_hour_suppressed or hard_blocked_suppressed)
        if suppressed:
            try:
                await self.alerts.mark_suppressed(alert.id)
            except Exception as e:
                logger.warning(
                    f"M2 mark_suppressed failed for alert {alert.id}: {e}"
                )
            if shadow_suppressed:
                logger.bind(category="alert_v2").info(
                    f"M2 alert SUPPRESSED (auto-block): {loser} {evaluation.best_line} "
                    f"@{alert_odds:.2f} — alerta v2 salvo no DB com suppressed=TRUE"
                )
            return True  # alerta no DB pra shadow tracking

        # Send to M2 Telegram group
        message_id = await self.notifier.send_alert_v2(alert_data)
        if message_id:
            try:
                await self.alerts.update_telegram_message_id(alert.id, message_id)
            except Exception as e:
                logger.warning(f"Could not save M2 message_id for alert {alert.id}: {e}")
            logger.bind(category="alert_v2").info(
                f"M2 alert sent (msg_id={message_id}): {loser} "
                f"{evaluation.camada} {alert_label} @{alert_odds:.2f} "
                f"prob={evaluation.prob:.0%} n={evaluation.sample_size}"
            )

        return True
