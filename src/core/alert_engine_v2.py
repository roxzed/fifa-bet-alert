"""Alert Engine V2: avalia oportunidades com StatsEngineV2 e envia para grupo M2."""

from __future__ import annotations

from datetime import datetime, timezone

from loguru import logger


class AlertEngineV2:
    """
    Recebe oportunidade do OddsMonitor, avalia com StatsEngineV2,
    e envia alerta para o grupo Telegram do Method 2.
    """

    def __init__(self, stats_engine_v2, alert_v2_repo, notifier_v2) -> None:
        self.stats = stats_engine_v2
        self.alerts = alert_v2_repo
        self.notifier = notifier_v2

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
