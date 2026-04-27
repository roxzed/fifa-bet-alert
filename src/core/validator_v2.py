"""Validator V2: valida resultados dos alertas do Method 2.

Mesma logica do Validator original mas opera sobre alerts_v2 e envia
resultados para o grupo M2.
"""

from __future__ import annotations

import asyncio
import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from loguru import logger


class ValidatorV2:
    """Valida alertas M2 apos o termino dos jogos de volta."""

    CSV_PATH = Path("data/results_v2.csv")
    MAX_RETRIES = 10
    MIN_ELAPSED_SECS = 13 * 60  # 13 min
    STALE_HOURS = 48

    def __init__(self, api_client, match_repo, alert_v2_repo, notifier_v2,
                 session_factory=None) -> None:
        self.api = api_client
        self.matches = match_repo
        self.alerts = alert_v2_repo
        self.notifier = notifier_v2
        self._running = False
        self._retry_counts: dict[int, int] = {}

    async def start(self, poll_interval: int = 60) -> None:
        self._running = True
        self._db_failures = 0
        logger.info("ValidatorV2 started")
        while self._running:
            if self._db_failures >= 5:
                logger.warning(f"ValidatorV2 circuit breaker: {self._db_failures} failures, cooldown 60s")
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
                    logger.error(f"ValidatorV2 DB error ({self._db_failures}/5): {e}")
                else:
                    logger.error(f"ValidatorV2 error: {e}")
            await asyncio.sleep(poll_interval)

    def stop(self) -> None:
        self._running = False

    async def _validation_cycle(self) -> None:
        """Busca jogos de volta com alertas M2 nao validados."""
        unvalidated = await self.matches.get_unvalidated_return_matches_v2()
        now = datetime.now(timezone.utc).replace(tzinfo=None)

        for return_match in unvalidated:
            if return_match.started_at:
                age_hours = (now - return_match.started_at).total_seconds() / 3600
                if age_hours > self.STALE_HOURS:
                    await self._abandon_match_alerts(return_match.id)
                    continue
                elapsed = (now - return_match.started_at).total_seconds()
                if elapsed < self.MIN_ELAPSED_SECS:
                    continue

            # Check if this match has any M2 alerts
            m2_alerts = await self.alerts.get_all_by_match_id(return_match.id)
            if not m2_alerts:
                continue

            # Check if any need validation
            pending = [a for a in m2_alerts if a.validated_at is None]
            if not pending:
                continue

            try:
                await self._validate_match(return_match, pending)
            except Exception as e:
                logger.error(f"ValidatorV2 failed to validate match {return_match.id}: {e}")
                if "sqlalchemy" in str(e).lower() or "asyncpg" in str(e).lower():
                    self._db_failures += 1

    async def _abandon_match_alerts(self, match_id: int) -> None:
        all_alerts = await self.alerts.get_all_by_match_id(match_id)
        for alert in all_alerts:
            if alert.validated_at:
                continue
            try:
                await self.alerts.validate(
                    alert_id=alert.id, actual_goals=-1, hit=False, profit_flat=0.0,
                )
                logger.info(f"M2 abandoned stale alert {alert.id} (match {match_id})")
            except Exception as e:
                logger.error(f"M2 failed to abandon alert {alert.id}: {e}")

    async def _validate_match(self, return_match, pending_alerts) -> None:
        """Busca placar e valida alertas M2 pendentes."""
        home_score = None
        away_score = None

        # 1. API
        try:
            details = await self.api.get_event_details(return_match.api_event_id)
            if details.status == "ended" and details.home_score is not None:
                home_score = details.home_score
                away_score = details.away_score
        except Exception as e:
            logger.warning(f"M2 could not fetch details for {return_match.id}: {e}")

        # 2. Fallback DB
        if home_score is None and return_match.score_home is not None and return_match.status == "ended":
            home_score = return_match.score_home
            away_score = return_match.score_away

        if home_score is None or away_score is None:
            return

        for alert in pending_alerts:
            retries = self._retry_counts.get(alert.id, 0)
            if retries >= self.MAX_RETRIES:
                logger.error(f"M2 alert {alert.id} exceeded {self.MAX_RETRIES} retries")
                try:
                    await self.alerts.validate(alert_id=alert.id, actual_goals=-1, hit=False, profit_flat=0.0)
                except Exception:
                    pass
                self._retry_counts.pop(alert.id, None)
                continue

            try:
                await self._validate_alert(alert, return_match, home_score, away_score)
                self._retry_counts.pop(alert.id, None)
            except Exception as e:
                self._retry_counts[alert.id] = retries + 1
                logger.error(f"M2 failed to validate alert {alert.id} (attempt {retries + 1}): {e}")

    async def _validate_alert(self, alert, return_match, home_score, away_score) -> None:
        loser = alert.losing_player
        if return_match.player_home == loser:
            loser_goals = home_score
        else:
            loser_goals = away_score

        best_line = alert.best_line or "over25"
        if best_line == "over45":
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

        # Resolve odds
        if best_line == "over45":
            odds = alert.over45_odds
        elif best_line == "over35":
            odds = alert.over35_odds
        elif best_line == "over15":
            odds = alert.over15_odds
        else:
            odds = alert.over25_odds
        profit_flat = (odds - 1.0) if hit and odds else (-1.0 if odds else 0.0)

        # Persist
        await self.alerts.validate(
            alert_id=alert.id,
            actual_goals=loser_goals,
            hit=hit,
            profit_flat=profit_flat,
        )

        # Edit Telegram message
        score_line = f"{home_score}-{away_score} ({loser} fez {loser_goals} gols)"
        await self._send_result_notification(alert, return_match, hit, score_line, line_label, odds, profit_flat)

        # CSV
        try:
            self._export_to_csv(alert, return_match, hit, loser_goals, line_label, odds, profit_flat, home_score, away_score)
        except Exception as e:
            logger.warning(f"M2 CSV export error: {e}")

        logger.info(
            f"M2 validated: {loser} {line_label} {alert.camada} — "
            f"{'GREEN' if hit else 'RED'} ({score_line})"
        )

    async def _send_result_notification(self, alert, return_match, hit, score_line,
                                        line_label, odds, profit_flat) -> None:
        # 2026-04-26 fix: alertas v2 suprimidos (auto-block SHADOW/PERMANENT) NAO
        # podem ter resultado enviado pro grupo M2 — mensagem original nunca foi
        # enviada. Mesmo bug do M1 corrigido em validator.py.
        if getattr(alert, "suppressed", False):
            logger.info(
                f"M2 validator skip notification: alert#{alert.id} "
                f"{alert.losing_player} {alert.best_line} esta suppressed "
                f"(auto-block) — sem mensagem no grupo"
            )
            return

        edited = False
        if alert.telegram_message_id:
            try:
                await self.notifier.edit_alert_v2_result(
                    message_id=alert.telegram_message_id,
                    alert=alert,
                    return_match=return_match,
                    hit=hit,
                    score_line=score_line,
                )
                edited = True
            except Exception as e:
                logger.warning(f"M2 could not edit message {alert.telegram_message_id}: {e}")

        if not edited:
            emoji = "\u2705" if hit else "\u274c"
            odds_str = f"{odds:.2f}" if odds else "?.??"
            profit_str = f"{profit_flat:+.2f}" if profit_flat is not None else "+0.00"
            result_text = (
                f"{emoji} <b>M2 {'GREEN' if hit else 'RED'}</b> [{alert.camada}]\n\n"
                f"\U0001f464 {alert.losing_player}\n"
                f"\U0001f3af {line_label} @{odds_str}\n"
                f"\u26bd {score_line}\n"
                f"\U0001f4b0 P&L: <b>{profit_str}u</b>"
            )
            try:
                await self.notifier.send_message_v2(result_text)
            except Exception as e:
                logger.warning(f"M2 could not send standalone result for alert {alert.id}: {e}")

    def _export_to_csv(self, alert, return_match, hit, loser_goals, line_label, odds, profit_flat, home_score, away_score) -> None:
        self.CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
        file_exists = self.CSV_PATH.exists()

        try:
            from zoneinfo import ZoneInfo
            from src.config import settings
            tz_local = ZoneInfo(settings.timezone)
        except Exception:
            tz_local = timezone(timedelta(hours=-3))

        sent_at = alert.sent_at
        if sent_at:
            sent_local = sent_at.replace(tzinfo=timezone.utc).astimezone(tz_local)
        else:
            sent_local = datetime.now(tz_local)

        row = {
            "alert_id": alert.id,
            "date": sent_local.strftime("%Y-%m-%d"),
            "time": sent_local.strftime("%H:%M"),
            "losing_player": alert.losing_player,
            "opponent": alert.opponent_player or "",
            "camada": alert.camada,
            "line": line_label,
            "odds": f"{odds:.2f}" if odds else "",
            "prob": f"{alert.prob:.1%}" if alert.prob else "",
            "result": "GREEN" if hit else "RED",
            "score": f"{home_score}-{away_score}",
            "loser_goals": loser_goals,
            "profit": f"{profit_flat:.2f}" if profit_flat is not None else "0.00",
            "player_home": return_match.player_home or "",
            "player_away": return_match.player_away or "",
            "team_home": return_match.team_home or "",
            "team_away": return_match.team_away or "",
        }

        with open(self.CSV_PATH, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=row.keys())
            if not file_exists:
                writer.writeheader()
            writer.writerow(row)
