"""Alert Engine: evaluates opportunities and sends Telegram alerts."""

from __future__ import annotations

from datetime import datetime, timezone

from loguru import logger


class AlertEngine:
    """
    Receives a potential opportunity from OddsMonitor,
    evaluates with StatsEngine, and sends alerts via Telegram.
    """

    def __init__(self, stats_engine, alert_repo, notifier) -> None:
        self.stats = stats_engine
        self.alerts = alert_repo
        self.notifier = notifier
        self._recalibrator = None  # injetado apos construcao

    def set_recalibrator(self, recalibrator) -> None:
        """Injeta referencia ao AutoRecalibrator para checar pausa de alertas."""
        self._recalibrator = recalibrator

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
        ml_odds: float | None = None,
        over25_opening: float | None = None,
        minutes_to_kickoff: int = 0,
        odds_history: list | None = None,
        loser_goals_g1: int = 0,
        bet365_url: str = "",
    ) -> bool:
        """
        Full evaluation pipeline:
        1. Get all context (teams, score)
        2. Run stats engine evaluation
        3. If should_alert, save to DB and send Telegram
        Returns True if alert was sent.
        """
        score_home = game1_match.score_home or 0
        score_away = game1_match.score_away or 0

        # Determine winner/loser scores and teams
        if game1_match.player_home == loser:
            score_winner = score_away
            score_loser = score_home
            loser_team = game1_match.team_home
            opponent_team = game1_match.team_away
            loser_was_home_g1 = True
        else:
            score_winner = score_home
            score_loser = score_away
            loser_team = game1_match.team_away
            opponent_team = game1_match.team_home
            loser_was_home_g1 = False

        kickoff = return_match.started_at

        evaluation = await self.stats.evaluate_opportunity(
            losing_player=loser,
            opponent_player=winner,
            game1_score_winner=score_winner,
            game1_score_loser=score_loser,
            over25_odds=over25_odds,
            over35_odds=over35_odds,
            over45_odds=over45_odds,
            over15_odds=over15_odds,
            ml_odds=ml_odds,
            match_time=kickoff or datetime.now(timezone.utc),
            loser_team=loser_team,
            opponent_team=opponent_team,
            odds_history=odds_history or [],
            loser_goals_g1=loser_goals_g1,
            loser_was_home_g1=loser_was_home_g1,
        )

        if not evaluation.should_alert:
            logger.info(f"No alert for {loser}: {evaluation.reason}")
            return False

        # Separar linhas Over e ML
        over_lines = []
        ml_line = None
        line_labels = {"over15": "O1.5", "over25": "O2.5", "over35": "O3.5", "over45": "O4.5", "ml": "ML Vitória"}
        for le in [evaluation.line_over15, evaluation.line_over25,
                    evaluation.line_over35, evaluation.line_over45]:
            if le and le.should_alert:
                over_lines.append({
                    "line": le.line,
                    "label": line_labels.get(le.line, le.line),
                    "odds": le.odds,
                    "true_prob": le.true_prob,
                    "edge": le.edge_val,
                    "ev": le.ev_val,
                    "stars": le.stars,
                })
        if evaluation.line_ml and evaluation.line_ml.should_alert:
            ml_line = {
                "line": "ml",
                "label": "ML Vitória",
                "odds": evaluation.line_ml.odds,
                "true_prob": evaluation.line_ml.true_prob,
                "edge": evaluation.line_ml.edge_val,
                "ev": evaluation.line_ml.ev_val,
                "stars": evaluation.line_ml.stars,
            }

        if not over_lines and not ml_line:
            logger.info(f"No alert for {loser}: no lines with edge")
            return False

        # Best over line para o DB
        best_over = max(over_lines, key=lambda l: l["ev"]) if over_lines else None
        best_line = best_over["line"] if best_over else "ml"

        if best_line == "over45":
            alert_odds = over45_odds
            alert_label = "Over 4.5"
        elif best_line == "over35":
            alert_odds = over35_odds
            alert_label = "Over 3.5"
        elif best_line == "over15":
            alert_odds = over15_odds
            alert_label = "Over 1.5"
        elif best_line == "ml":
            alert_odds = ml_odds
            alert_label = "Vitória"
        else:
            alert_odds = over25_odds
            alert_label = "Over 2.5"

        # Save alert to database
        alert = await self.alerts.create(
            match_id=return_match.id,
            losing_player=loser,
            game1_score=f"{score_winner}-{score_loser}",
            loss_margin=evaluation.loss_margin,
            loss_type=evaluation.loss_type,
            best_line=best_line,
            over15_odds=over15_odds,
            over25_odds=over25_odds,
            over35_odds=over35_odds,
            over45_odds=over45_odds,
            ml_odds=ml_odds,
            implied_prob=evaluation.implied_prob,
            true_prob=evaluation.true_prob,
            true_prob_conservative=evaluation.true_prob_conservative,
            edge=evaluation.edge_val,
            expected_value=evaluation.expected_value_val,
            kelly_fraction=evaluation.kelly_fraction_val,
            star_rating=evaluation.star_rating_val,
            p_base=evaluation.p_base,
            p_loss_type=evaluation.p_loss_type,
            p_player=evaluation.p_player,
            p_recent_form=evaluation.p_recent_form,
            p_h2h=evaluation.p_h2h,
            p_y_post_win=evaluation.p_y_post_win,
            p_time_slot=evaluation.p_time_slot,
            p_market_adj=evaluation.p_market_adj,
            player_sample_size=evaluation.player_sample_size,
            h2h_sample_size=evaluation.h2h_sample_size,
            recent_form_sample=evaluation.recent_form_sample,
            global_sample_size=evaluation.global_sample_size,
            loss_type_sample_size=evaluation.loss_type_sample_size,
            team_sample_size=evaluation.team_sample_size,
            confidence_interval_low=evaluation.confidence_interval[0],
            confidence_interval_high=evaluation.confidence_interval[1],
            alert_level=self._level_from_stars(evaluation.star_rating_val),
            loser_goals_g1=evaluation.loser_goals_g1,
            streak=evaluation.streak,
            p_g1_goals=evaluation.p_g1_goals,
        )

        # Dados base compartilhados entre Over e ML
        base_data = {
            "star_rating": evaluation.star_rating_val,
            "alert_level": self._level_from_stars(evaluation.star_rating_val),
            "game1_player_home": game1_match.player_home,
            "game1_player_away": game1_match.player_away,
            "game1_score_home": game1_match.score_home,
            "game1_score_away": game1_match.score_away,
            "game1_team_home": game1_match.team_home,
            "game1_team_away": game1_match.team_away,
            "loss_type": evaluation.loss_type,
            "return_player_home": return_match.player_home,
            "return_player_away": return_match.player_away,
            "return_team_home": return_match.team_home,
            "return_team_away": return_match.team_away,
            "kickoff_time": kickoff,
            "minutes_to_kickoff": minutes_to_kickoff,
            "losing_player": loser,
            "loser_team": loser_team,
            "over15_odds": over15_odds,
            "over25_odds": over25_odds,
            "over25_opening_odds": over25_opening,
            "over35_odds": over35_odds,
            "over45_odds": over45_odds,
            "ml_odds": ml_odds,
            "implied_prob": evaluation.implied_prob,
            "true_prob": evaluation.true_prob,
            "edge": evaluation.edge_val,
            "expected_value": evaluation.expected_value_val,
            "kelly_fraction": evaluation.kelly_fraction_val,
            "p_base": evaluation.p_base,
            "p_player": evaluation.p_player,
            "p_recent_form": evaluation.p_recent_form,
            "p_h2h": evaluation.p_h2h,
            "p_y_post_win": evaluation.p_y_post_win,
            "p_loss_type": evaluation.p_loss_type,
            "p_time_slot": evaluation.p_time_slot,
            "p_team": evaluation.p_team,
            "global_sample_size": evaluation.global_sample_size,
            "player_sample_size": evaluation.player_sample_size,
            "h2h_sample_size": evaluation.h2h_sample_size,
            "recent_form_sample": evaluation.recent_form_sample,
            "loss_type_sample_size": evaluation.loss_type_sample_size,
            "team_sample_size": evaluation.team_sample_size,
            "confidence_interval_low": evaluation.confidence_interval[0],
            "confidence_interval_high": evaluation.confidence_interval[1],
            "hour_of_day": (kickoff.hour if kickoff else 0),
            "loser_goals_g1": evaluation.loser_goals_g1,
            "streak": evaluation.streak,
            "streak_factor": evaluation.streak_factor,
            "p_g1_goals": evaluation.p_g1_goals,
            "winrate": evaluation.p_base,
            "total_g1_goals": evaluation.total_g1_goals,
            "total_g1_factor": evaluation.total_g1_factor,
            "game_pattern": evaluation.game_pattern,
            "game_pattern_factor": evaluation.game_pattern_factor,
            "player_flag": evaluation.player_flag,
            "bet365_url": bet365_url,
        }

        # Session-per-method: alert.create() already committed
        # Alerta criado no DB — retornar True mesmo se Telegram falhar/pausado
        # para evitar que o OddsMonitor crie duplicatas a cada poll

        message_id = None

        # 1) Enviar alerta OVER GOLS (se houver linhas com edge)
        if over_lines:
            over_data = {**base_data}
            over_data["best_line"] = best_over["line"]
            over_data["alert_label"] = f"{alert_label} gols {loser}" if best_line != "ml" else f"Over gols {loser}"
            over_data["alert_odds"] = best_over["odds"]
            over_data["true_prob"] = best_over["true_prob"]
            over_data["all_lines"] = over_lines
            message_id = await self.notifier.send_alert(over_data)
            if message_id:
                try:
                    await self.alerts.update_telegram_message_id(alert.id, message_id)
                except Exception as e:
                    logger.warning(f"Could not save message_id for alert {alert.id}: {e}")
                logger.bind(category="alert").info(
                    f"OVER alert sent (msg_id={message_id}): {loser} {best_over['label']} @{best_over['odds']:.2f}"
                )

        # 2) Enviar alerta ML SEPARADO (se houver edge)
        if ml_line:
            ml_data = {**base_data}
            ml_data["best_line"] = "ml"
            ml_data["alert_label"] = f"Vitória {loser}"
            ml_data["alert_odds"] = ml_line["odds"]
            ml_data["true_prob"] = ml_line["true_prob"]
            ml_data["star_rating"] = ml_line["stars"]
            ml_data["edge"] = ml_line["edge"]
            ml_data["expected_value"] = ml_line["ev"]
            ml_data["all_lines"] = [ml_line]
            ml_msg_id = await self.notifier.send_alert(ml_data)
            if ml_msg_id:
                logger.bind(category="alert").info(
                    f"ML alert sent (msg_id={ml_msg_id}): {loser} ML @{ml_line['odds']:.2f}"
                )
                if not message_id:
                    message_id = ml_msg_id
                    try:
                        await self.alerts.update_telegram_message_id(alert.id, ml_msg_id)
                    except Exception as e:
                        logger.warning(f"Could not save ML message_id for alert {alert.id}: {e}")

        # Retorna True se alerta foi criado no DB (independente do Telegram)
        # Isso evita que OddsMonitor crie duplicatas quando alertas estao pausados
        return True

    @staticmethod
    def _level_from_stars(stars: int) -> str:
        if stars >= 5:
            return "red_special"
        if stars >= 3:
            return "green"
        return "yellow"
