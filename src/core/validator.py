"""Post-game validator: checks return match results, edits Telegram alerts, exports to CSV.

Robustez (2026-03-27):
- Retry com backoff individual por match/alert em caso de falha parcial.
- Limite de tentativas (MAX_RETRIES) para evitar loop infinito em alertas quebrados.
- Proteção contra odds None no cálculo de P&L.
- CSV export isolado: copia atributos antes de acessar fora da session.
- Greenlet-safe: nenhum acesso lazy a ORM objects fora de contexto async.
"""

from __future__ import annotations

import asyncio
import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from loguru import logger


class Validator:
    """
    Polls for return match results after games end.
    - Edits the original Telegram message with GREEN/RED result
    - Updates alerts, player stats, and method stats
    - Exports results to CSV spreadsheet
    """

    CSV_PATH = Path("data/results.csv")
    MAX_RETRIES = 10  # max validation attempts per alert before giving up
    MIN_ELAPSED_SECS = 13 * 60  # 13 min after kickoff before validating
    STALE_HOURS = 48  # skip matches older than this

    def __init__(self, api_client, match_repo, alert_repo, stats_engine, notifier,
                 session_factory=None) -> None:
        self.api = api_client
        self.matches = match_repo
        self.alerts = alert_repo
        self.stats = stats_engine
        self.notifier = notifier
        self._running = False
        # Track per-alert retry counts to avoid infinite loops
        self._retry_counts: dict[int, int] = {}
        self._drawdown_notified: bool = False  # evita spam de notificacao de drawdown

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
            await asyncio.sleep(poll_interval)

    def stop(self) -> None:
        self._running = False

    async def _validation_cycle(self) -> None:
        """Find unvalidated alerts whose return match has ended, fetch results."""
        unvalidated = await self.matches.get_unvalidated_return_matches()
        now = datetime.now(timezone.utc).replace(tzinfo=None)

        for return_match in unvalidated:
            # Skip stale matches that have been stuck too long
            if return_match.started_at:
                age_hours = (now - return_match.started_at).total_seconds() / 3600
                if age_hours > self.STALE_HOURS:
                    logger.warning(
                        f"Match {return_match.id}: stale ({age_hours:.0f}h old), "
                        f"marking alerts as abandoned"
                    )
                    await self._abandon_match_alerts(return_match.id)
                    continue

                elapsed = (now - return_match.started_at).total_seconds()
                if elapsed < self.MIN_ELAPSED_SECS:
                    continue

            try:
                await self._validate_match(return_match)
            except Exception as e:
                logger.error(f"Failed to validate match {return_match.id}: {e}")
                err_str = str(e).lower()
                if "sqlalchemy" in err_str or "asyncpg" in err_str or "timeout" in err_str:
                    self._db_failures += 1

    async def _abandon_match_alerts(self, match_id: int) -> None:
        """Mark stale alerts as validated with 0 profit to stop retrying."""
        all_alerts = await self.alerts.get_all_by_match_id(match_id)
        for alert in all_alerts:
            if alert.validated_at:
                continue
            try:
                await self.alerts.validate(
                    alert_id=alert.id,
                    actual_goals=-1,
                    over25_hit=False,
                    over35_hit=False,
                    over15_hit=False,
                    over45_hit=False,
                    profit_flat=0.0,
                    ml_hit=False,
                )
                logger.info(f"Abandoned stale alert {alert.id} (match {match_id})")
            except Exception as e:
                logger.error(f"Failed to abandon alert {alert.id}: {e}")

    async def _validate_match(self, return_match) -> None:
        """Fetch final score for a return match, edit alert message, export to CSV."""
        home_score = None
        away_score = None

        # 1. Try API first
        try:
            details = await self.api.get_event_details(return_match.api_event_id)
            if details.status == "ended" and details.home_score is not None and details.away_score is not None:
                home_score = details.home_score
                away_score = details.away_score
        except Exception as e:
            logger.warning(f"Could not fetch details for {return_match.id}: {e}")

        # 2. Fallback: use DB score if game_watcher already recorded it
        #    (some eSoccer events get stuck at time_status=2 in the API)
        if home_score is None and return_match.score_home is not None and return_match.score_away is not None:
            if return_match.status == "ended":
                home_score = return_match.score_home
                away_score = return_match.score_away
                logger.info(
                    f"Match {return_match.id}: using DB score {home_score}-{away_score} "
                    f"(API stuck at time_status!=3)"
                )

        if home_score is None or away_score is None:
            return

        # Build a simple details-like object for downstream use
        from types import SimpleNamespace
        details = SimpleNamespace(
            home_score=home_score, away_score=away_score, status="ended",
        )

        # Update match record
        await self.matches.update_result(
            match_id=return_match.id,
            score_home=home_score,
            score_away=away_score,
            ended_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )

        # Find ALL alerts for this match (pode haver Over + ML)
        all_alerts = await self.alerts.get_all_by_match_id(return_match.id)
        if not all_alerts:
            # Fallback: busca unica
            single = await self.alerts.get_by_match_id(return_match.id)
            all_alerts = [single] if single else []

        # Rastrear quais best_lines ja foram notificadas para evitar spam de duplicatas
        # (mesmo match_id + best_line = alerta duplicado, so notifica uma vez)
        notified_lines: set[str] = set()

        for alert in all_alerts:
            if alert.validated_at:
                continue

            # Check retry limit
            retries = self._retry_counts.get(alert.id, 0)
            if retries >= self.MAX_RETRIES:
                logger.error(
                    f"Alert {alert.id} exceeded {self.MAX_RETRIES} retries, abandoning"
                )
                try:
                    await self.alerts.validate(
                        alert_id=alert.id,
                        actual_goals=-1,
                        over25_hit=False,
                        over35_hit=False,
                        over15_hit=False,
                        over45_hit=False,
                        profit_flat=0.0,
                        ml_hit=False,
                    )
                except Exception:
                    pass
                self._retry_counts.pop(alert.id, None)
                continue

            # Determinar se deve enviar notificacao Telegram para este alerta
            # Duplicatas (mesmo best_line) nao devem gerar notificacao extra
            best_line_key = alert.best_line or "over25"
            should_notify = best_line_key not in notified_lines

            try:
                await self._validate_alert(alert, return_match, details, send_notification=should_notify)
                notified_lines.add(best_line_key)
                # Success — clean up retry counter
                self._retry_counts.pop(alert.id, None)
            except Exception as e:
                self._retry_counts[alert.id] = retries + 1
                logger.error(
                    f"Failed to validate alert {alert.id} "
                    f"(attempt {retries + 1}/{self.MAX_RETRIES}): {e}"
                )

    async def _validate_alert(self, alert, return_match, details, send_notification: bool = True) -> None:
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

        # Resolve odds (protect against None)
        odds_used = self._get_odds_for_line(alert, best_line)
        profit_flat = (odds_used - 1.0) if hit and odds_used else (-1.0 if odds_used else 0.0)

        # Persist validation — this is the critical DB write
        await self.alerts.validate(
            alert_id=alert.id,
            actual_goals=loser_goals,
            over25_hit=over25_hit,
            over35_hit=over35_hit,
            over15_hit=over15_hit,
            over45_hit=over45_hit,
            profit_flat=profit_flat,
            ml_hit=ml_hit,
        )

        # === From here on, everything is best-effort (non-critical) ===
        # Copy all values we need BEFORE any further DB/async calls
        # to avoid greenlet_spawn errors from lazy ORM attribute access
        csv_data = self._extract_csv_data(
            alert, return_match, details, hit, best_line, line_label,
            loser_goals, odds_used, profit_flat,
        )

        # Update stats
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
        # send_notification=False para alertas duplicados (mesmo match+linha) — evita spam
        if send_notification:
            await self._send_result_notification(
                alert, return_match, hit, score_line, line_label, odds_used, profit_flat,
            )
        else:
            logger.debug(
                f"Alert {alert.id} ({alert.best_line}): resultado registrado no DB "
                f"sem notificacao Telegram (duplicata da linha)"
            )

        # Export to CSV (sync, uses pre-extracted data)
        try:
            self._export_to_csv(csv_data)
        except Exception as e:
            logger.warning(f"CSV export error: {e}")

        logger.info(
            f"Validated: {alert.losing_player} {line_label} — "
            f"{'GREEN' if hit else 'RED'} ({score_line})"
        )

        # Drawdown auto-pause after RED
        if not hit:
            try:
                await self._check_drawdown()
            except Exception as e:
                logger.debug(f"Drawdown check failed: {e}")

    def _get_odds_for_line(self, alert, best_line: str) -> Optional[float]:
        """Extract the correct odds value for the alert's best line."""
        if best_line == "ml":
            return getattr(alert, "ml_odds", None) or 2.60
        elif best_line == "over45":
            return getattr(alert, "over45_odds", None)
        elif best_line == "over35":
            return getattr(alert, "over35_odds", None)
        elif best_line == "over15":
            return getattr(alert, "over15_odds", None)
        else:
            return getattr(alert, "over25_odds", None)

    def _extract_csv_data(self, alert, return_match, details, hit, best_line,
                          line_label, loser_goals, odds_used, profit_flat) -> dict:
        """Extract all data needed for CSV export while ORM objects are still alive.

        This prevents greenlet_spawn errors from lazy attribute access later.
        """
        try:
            from zoneinfo import ZoneInfo
            from src.config import settings
            tz_local = ZoneInfo(settings.timezone)
        except Exception:
            tz_local = timezone(timedelta(hours=-3))

        sent_at = getattr(alert, "sent_at", None)
        if sent_at:
            sent_local = sent_at.replace(tzinfo=timezone.utc).astimezone(tz_local)
        else:
            sent_local = datetime.now(tz_local)

        return {
            "alert_id": alert.id,
            "date": sent_local.strftime("%Y-%m-%d"),
            "time": sent_local.strftime("%H:%M"),
            "losing_player": alert.losing_player,
            "line_label": line_label,
            "odds": f"{odds_used:.2f}" if odds_used else "",
            "true_prob": f"{alert.true_prob:.1%}" if alert.true_prob else "",
            "result": "GREEN" if hit else "RED",
            "score": f"{details.home_score}-{details.away_score}",
            "loser_goals": loser_goals,
            "green": 1 if hit else 0,
            "profit": f"{profit_flat:.2f}" if profit_flat is not None else "0.00",
            "player_home": return_match.player_home,
            "player_away": return_match.player_away,
            "team_home": return_match.team_home or "",
            "team_away": return_match.team_away or "",
        }

    async def _send_result_notification(self, alert, return_match, hit, score_line,
                                        line_label, odds_used, profit_flat) -> None:
        """Edit original Telegram message or send standalone result."""
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
            # Fallback (2026-04-25): usar mesmo formato do alerta original + linha
            # de resultado, em vez de mensagem simplificada que confundia visualmente.
            # Cenario: telegram_message_id ficou None (bug raro do send timeout) OU
            # edicao falhou. Nesse caso enviamos mensagem nova mas com look-and-feel
            # identico ao que seria a edicao.
            try:
                from src.telegram.messages import format_alert
                original_data = self._rebuild_alert_data(alert, return_match)
                original_text = format_alert(original_data)
                if hit:
                    result_line = f"\n\n\u2705 GREEN \u2014 {score_line}"
                else:
                    result_line = f"\n\n\u274c RED \u2014 {score_line}"
                full_text = original_text + result_line
                await self.notifier.send_message(full_text)
                logger.info(
                    f"Fallback result sent (formato original) for alert {alert.id}: "
                    f"{'GREEN' if hit else 'RED'} {score_line}"
                )
            except Exception as e:
                logger.warning(f"Could not send fallback result for alert {alert.id}: {e}")

    async def _check_drawdown(self) -> None:
        """Notifica sobre drawdown severo (5+ losses seguidas ou -5u). Nao pausa automaticamente."""
        streak = await self.alerts.get_recent_streak(20)
        consec = streak.get("consecutive_losses", 0)
        recent_profit = streak.get("recent_profit", 0.0)
        current_streak = streak.get("streak", 0)

        # Trigger: 5+ losses seguidas OU -5u nos ultimos 20 alertas
        if current_streak <= -5 or recent_profit <= -5.0:
            if self._drawdown_notified:
                return  # ja notificou, nao spammar
            self._drawdown_notified = True
            msg = (
                "\u26a0\ufe0f <b>DRAWDOWN ALERT</b>\n\n"
                f"Streak atual: {abs(current_streak)}L seguidas\n"
                f"P&L ultimos 20: <b>{recent_profit:+.1f}u</b>\n"
                f"Pior sequencia: {consec}L\n\n"
                "Alertas continuam ativos.\n"
                "Use /pause se quiser pausar manualmente."
            )
            try:
                await self.notifier.send_admin_message(msg)
            except Exception:
                pass
            logger.warning(
                f"DRAWDOWN ALERT: {abs(current_streak)}L, "
                f"{recent_profit:+.1f}u nos ultimos 20 (sem pausa automatica)"
            )
        else:
            # Saiu do drawdown — resetar flag para notificar se entrar novamente
            self._drawdown_notified = False

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

    def _export_to_csv(self, data: dict) -> None:
        """Append result row to CSV spreadsheet (deduplicado por alert_id).

        Receives a plain dict (no ORM objects) to avoid greenlet issues.
        """
        if self._already_in_csv(data["alert_id"]):
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

            writer.writerow([
                data["alert_id"],
                data["date"],
                data["time"],
                data["losing_player"],
                data["line_label"],
                data["odds"],
                data["true_prob"],
                data["result"],
                data["score"],
                data["loser_goals"],
                data["green"],
                data["profit"],
                data["player_home"],
                data["player_away"],
                data["team_home"],
                data["team_away"],
            ])
