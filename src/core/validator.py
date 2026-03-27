"""Post-game validator: checks return match results, edits Telegram alerts, exports to CSV.

Correcoes (2026-03-26):
- Sessao isolada por validacao: cada _validate_match usa sua propria sessao
  via context manager, evitando corrupcao de sessao por timeout/rollback.
- Rollback defensivo antes de cada ciclo para garantir estado limpo.
"""

from __future__ import annotations

import asyncio
import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loguru import logger


class Validator:
    """
    Polls for return match results after games end.
    - Edits the original Telegram message with GREEN/RED result
    - Updates alerts, player stats, and method stats
    - Exports results to CSV spreadsheet
    """

    CSV_PATH = Path("data/results.csv")

    def __init__(self, api_client, match_repo, alert_repo, stats_engine, notifier,
                 session_factory=None) -> None:
        self.api = api_client
        self.matches = match_repo
        self.alerts = alert_repo
        self.stats = stats_engine
        self.notifier = notifier
        self._session_factory = session_factory
        self._running = False

    async def start(self, poll_interval: int = 60) -> None:
        """Start validation loop with circuit breaker."""
        self._running = True
        self._db_failures = 0
        logger.info("Validator started")
        while self._running:
            if self._db_failures >= 5:
                logger.warning(f"Validator circuit breaker: {self._db_failures} failures, cooldown 60s")
                await asyncio.sleep(60)
                self._db_failures = 0
                self._recreate_session()
                continue

            try:
                await self._validation_cycle()
                self._db_failures = 0
            except Exception as e:
                err_str = str(e).lower()
                if "sqlalchemy" in err_str or "asyncpg" in err_str or "timeout" in err_str:
                    self._db_failures += 1
                    logger.error(f"Validator DB error ({self._db_failures}/5): {e}")
                else:
                    logger.error(f"Validator error: {e}")
                try:
                    await self.matches.session.rollback()
                except Exception:
                    self._recreate_session()
            await asyncio.sleep(poll_interval)

    def _recreate_session(self) -> None:
        """Recreate DB session after irrecoverable failure."""
        try:
            factory = self._session_factory
            if factory is None:
                from src.db.database import async_session_factory
                factory = async_session_factory
            from src.db.repositories import MatchRepository, AlertRepository
            new_session = factory()
            self.matches = MatchRepository(new_session)
            self.alerts = AlertRepository(new_session)
            logger.warning("Validator: sessao DB recriada")
        except Exception as e:
            logger.error(f"Validator: falha ao recriar sessao: {e}")

    def stop(self) -> None:
        self._running = False

    async def _validation_cycle(self) -> None:
        """Find unvalidated alerts whose return match has ended, fetch results."""
        # Rollback preventivo para garantir sessao limpa
        try:
            await self.matches.session.rollback()
        except Exception:
            pass

        unvalidated = await self.matches.get_unvalidated_return_matches()
        now = datetime.now(timezone.utc).replace(tzinfo=None)

        for return_match in unvalidated:
            # Esperar 13 min apos inicio do jogo (8 min jogo + 5 min buffer API)
            if return_match.started_at:
                elapsed = (now - return_match.started_at).total_seconds()
                if elapsed < 13 * 60:
                    remaining = 13 * 60 - elapsed
                    logger.debug(
                        f"Match {return_match.id}: aguardando "
                        f"{remaining / 60:.0f}min para validar"
                    )
                    continue

            try:
                await self._validate_match(return_match)
            except Exception as e:
                logger.error(f"Failed to validate match {return_match.id}: {e}")
                # Rollback para limpar estado e continuar com proximo match
                try:
                    await self.matches.session.rollback()
                except Exception:
                    self._recreate_session()
                    break  # sessao recriada, sair do loop e tentar no proximo ciclo

    async def _validate_match(self, return_match) -> None:
        """Fetch final score for a return match, edit alert message, export to CSV."""
        try:
            details = await self.api.get_event_details(return_match.api_event_id)
        except Exception as e:
            logger.warning(f"Could not fetch details for {return_match.id}: {e}")
            return

        # So validar quando o jogo REALMENTE terminou (status=ended / time_status=3)
        if details.status != "ended":
            return

        if details.home_score is None or details.away_score is None:
            return

        # Update match record
        await self.matches.update_result(
            match_id=return_match.id,
            score_home=details.home_score,
            score_away=details.away_score,
            status="ended",
            ended_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )

        # Find ALL alerts for this match (pode haver Over + ML)
        all_alerts = await self.alerts.get_all_by_match_id(return_match.id)
        if not all_alerts:
            # Fallback: busca unica
            single = await self.alerts.get_by_match_id(return_match.id)
            all_alerts = [single] if single else []

        for alert in all_alerts:
            if alert.validated_at:
                continue
            try:
                await self._validate_alert(alert, return_match, details)
            except Exception as e:
                logger.error(f"Failed to validate alert {alert.id}: {e}")

        # Commit tudo de uma vez
        try:
            await self.matches.session.commit()
        except Exception as e:
            logger.warning(f"Commit after validation failed: {e}")
            try:
                await self.matches.session.rollback()
            except Exception:
                pass

    async def _validate_alert(self, alert, return_match, details) -> None:
        """Validate a single alert against match result."""
        loser = alert.losing_player
        if return_match.player_home == loser:
            loser_goals = details.home_score
        else:
            loser_goals = details.away_score

        opp_goals = details.away_score if return_match.player_home == loser else details.home_score
        ml_hit = loser_goals > opp_goals

        best_line = alert.best_line or "over25"
        if best_line == "ml":
            hit = ml_hit
            line_label = "Vitoria ML"
        elif best_line == "over45":
            hit = loser_goals > 4
            line_label = "Over 4.5"
        elif best_line == "over35":
            hit = loser_goals > 3
            line_label = "Over 3.5"
        elif best_line == "over15":
            hit = loser_goals > 1
            line_label = "Over 1.5"
        else:
            hit = loser_goals > 2
            line_label = "Over 2.5"

        over15_hit = loser_goals > 1
        over25_hit = loser_goals > 2
        over35_hit = loser_goals > 3
        over45_hit = loser_goals > 4

        # P&L flat-bet
        if best_line == "ml":
            odds_used = getattr(alert, "ml_odds", None) or 2.60
        elif best_line == "over45":
            odds_used = alert.over45_odds
        elif best_line == "over35":
            odds_used = alert.over35_odds
        elif best_line == "over15":
            odds_used = alert.over15_odds
        else:
            odds_used = alert.over25_odds

        profit_flat = (odds_used - 1.0) if hit else -1.0

        # Validar o alerta NA SESSÃO DO VALIDATOR (garante commit correto)
        over25_hit = loser_goals > 2
        over35_hit = loser_goals > 3
        await self.alerts.validate(
            alert_id=alert.id,
            actual_goals=loser_goals,
            over25_hit=over25_hit,
            over35_hit=over35_hit,
            over15_hit=over15_hit,
            over45_hit=over45_hit,
        )

        try:
            alert.profit_flat = profit_flat
            alert.ml_hit = ml_hit
        except Exception:
            pass

        # Update stats (sem re-validar o alerta)
        try:
            await self.stats.update_after_validation(
                alert_id=alert.id,
                alert=alert,
                actual_goals=loser_goals,
            )
        except Exception as e:
            logger.warning(f"Stats update failed for alert {alert.id}: {e}")

        score_line = f"{details.home_score}-{details.away_score} ({loser} fez {loser_goals} gols)"

        # Edit Telegram message (or send standalone if message_id missing)
        edited = False
        if alert.telegram_message_id:
            try:
                await self.notifier.edit_alert_result(
                    message_id=alert.telegram_message_id,
                    original_data=self._rebuild_alert_data(alert, return_match),
                    hit=hit,
                    score_line=score_line,
                )
                edited = True
                logger.info(
                    f"Alert msg {alert.telegram_message_id} edited: "
                    f"{'GREEN' if hit else 'RED'} {score_line}"
                )
            except Exception as e:
                logger.warning(f"Could not edit alert message {alert.telegram_message_id}: {e}")

        if not edited:
            # Fallback: enviar mensagem standalone com resultado
            emoji = "\u2705" if hit else "\u274c"
            result_text = (
                f"{emoji} <b>RESULTADO {'GREEN' if hit else 'RED'}</b>\n\n"
                f"\U0001f464 {alert.losing_player}\n"
                f"\U0001f3af {line_label} @{odds_used:.2f}\n"
                f"\u26bd {score_line}\n"
                f"\U0001f4b0 P&L: <b>{profit_flat:+.2f}u</b>"
            )
            try:
                await self.notifier.send_message(result_text)
                logger.info(f"Standalone result sent for alert {alert.id}")
            except Exception as e:
                logger.warning(f"Could not send standalone result for alert {alert.id}: {e}")

        # Export to CSV
        try:
            self._export_to_csv(alert, return_match, details, hit, best_line, line_label, loser_goals)
        except Exception as e:
            logger.warning(f"CSV export error: {e}")

        logger.info(
            f"Validated: {alert.losing_player} {line_label} — "
            f"{'GREEN' if hit else 'RED'} ({score_line})"
        )

    def _rebuild_alert_data(self, alert, return_match) -> dict:
        """Rebuild the alert_data dict from DB fields to re-render the message."""
        kickoff = return_match.started_at

        best_line = alert.best_line or "over25"
        if best_line == "ml":
            alert_label = f"Vitoria {alert.losing_player}"
            alert_odds = getattr(alert, "ml_odds", None) or 2.60
        elif best_line == "over45":
            alert_label = "Over 4.5"
            alert_odds = alert.over45_odds
        elif best_line == "over35":
            alert_label = "Over 3.5"
            alert_odds = alert.over35_odds
        elif best_line == "over15":
            alert_label = "Over 1.5"
            alert_odds = alert.over15_odds
        else:
            alert_label = "Over 2.5"
            alert_odds = alert.over25_odds

        return {
            "star_rating": alert.star_rating or 0,
            "alert_level": alert.alert_level or "yellow",
            "alert_label": alert_label,
            "alert_odds": alert_odds,
            "game1_player_home": "",
            "game1_player_away": "",
            "game1_score_home": alert.game1_score.split("-")[0] if alert.game1_score and "-" in alert.game1_score else "?",
            "game1_score_away": alert.game1_score.split("-")[1] if alert.game1_score and "-" in alert.game1_score else "?",
            "return_player_home": return_match.player_home,
            "return_player_away": return_match.player_away,
            "return_team_home": return_match.team_home,
            "return_team_away": return_match.team_away,
            "kickoff_time": kickoff,
            "minutes_to_kickoff": 0,
            "losing_player": alert.losing_player,
            "over25_odds": alert.over25_odds,
            "over35_odds": alert.over35_odds,
            "over45_odds": alert.over45_odds,
            "ml_odds": getattr(alert, "ml_odds", None),
            "true_prob": alert.true_prob,
            "implied_prob": alert.implied_prob,
            "edge": alert.edge,
            "expected_value": alert.expected_value,
            "kelly_fraction": alert.kelly_fraction,
        }

    def _already_in_csv(self, alert_id: int) -> bool:
        """Check if alert_id already exists in CSV to prevent duplicates."""
        if not self.CSV_PATH.exists():
            return False
        try:
            with open(self.CSV_PATH, "r", encoding="utf-8") as f:
                reader = csv.reader(f)
                next(reader, None)  # skip header
                for row in reader:
                    if row and row[0] == str(alert_id):
                        return True
        except Exception:
            pass
        return False

    def _export_to_csv(self, alert, return_match, details, hit: bool, best_line: str, line_label: str, loser_goals: int) -> None:
        """Append result row to CSV spreadsheet (deduplicado por alert_id)."""
        if self._already_in_csv(alert.id):
            return

        file_exists = self.CSV_PATH.exists()
        self.CSV_PATH.parent.mkdir(parents=True, exist_ok=True)

        with open(self.CSV_PATH, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow([
                    "alert_id", "data", "hora", "jogador_perdedor", "linha", "odds",
                    "winrate", "resultado", "placar_g2", "gols_perdedor",
                    "green", "profit",
                    "player_home", "player_away", "team_home", "team_away",
                ])

            odds = None
            if best_line == "ml":
                odds = getattr(alert, "ml_odds", None)
            elif best_line == "over45":
                odds = alert.over45_odds
            elif best_line == "over35":
                odds = alert.over35_odds
            elif best_line == "over15":
                odds = alert.over15_odds
            else:
                odds = alert.over25_odds

            profit = (odds - 1.0) if hit and odds else -1.0

            try:
                from zoneinfo import ZoneInfo
                from src.config import settings
                tz_local = ZoneInfo(settings.timezone)
            except Exception:
                tz_local = timezone(timedelta(hours=-3))

            sent_local = alert.sent_at.replace(tzinfo=timezone.utc).astimezone(tz_local) if alert.sent_at else datetime.now(tz_local)

            writer.writerow([
                alert.id,
                sent_local.strftime("%Y-%m-%d"),
                sent_local.strftime("%H:%M"),
                alert.losing_player,
                line_label,
                f"{odds:.2f}" if odds else "",
                f"{alert.true_prob:.1%}" if alert.true_prob else "",
                "GREEN" if hit else "RED",
                f"{details.home_score}-{details.away_score}",
                loser_goals,
                1 if hit else 0,
                f"{profit:.2f}",
                return_match.player_home,
                return_match.player_away,
                return_match.team_home or "",
                return_match.team_away or "",
            ])
