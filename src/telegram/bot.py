"""Telegram bot setup and message sending.

MELHORIA 20 (2026-03-25): Circuit breaker integrado.
- Após 5 falhas consecutivas, o circuit breaker abre e bloqueia envios por 60s.
- Após o cooldown, entra em modo "half-open" e testa com 1 mensagem.
- Se o teste funcionar, o circuito fecha e retoma operação normal.
- Backoff exponencial com jitter nas retentativas.
"""

from __future__ import annotations

import asyncio
import random
import time

from loguru import logger
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError


class _CircuitBreaker:
    """Simple circuit breaker for Telegram API calls.

    States:
        CLOSED  → normal operation, all calls go through
        OPEN    → too many failures, calls are blocked for cooldown_seconds
        HALF_OPEN → cooldown expired, allow one test call
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(
        self,
        failure_threshold: int = 5,
        cooldown_seconds: float = 60.0,
    ) -> None:
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._state = self.CLOSED
        self._consecutive_failures = 0
        self._opened_at: float = 0.0

    @property
    def state(self) -> str:
        if self._state == self.OPEN:
            elapsed = time.monotonic() - self._opened_at
            if elapsed >= self._cooldown_seconds:
                self._state = self.HALF_OPEN
                logger.info("Circuit breaker → HALF_OPEN (testing)")
        return self._state

    def record_success(self) -> None:
        """Record a successful call. Resets the breaker."""
        if self._state != self.CLOSED:
            logger.info("Circuit breaker → CLOSED (recovered)")
        self._consecutive_failures = 0
        self._state = self.CLOSED

    def record_failure(self) -> None:
        """Record a failed call. May trip the breaker."""
        self._consecutive_failures += 1
        if self._consecutive_failures >= self._failure_threshold:
            if self._state != self.OPEN:
                logger.warning(
                    f"Circuit breaker → OPEN after {self._consecutive_failures} "
                    f"consecutive failures (cooldown={self._cooldown_seconds}s)"
                )
            self._state = self.OPEN
            self._opened_at = time.monotonic()

    def allow_request(self) -> bool:
        """Check if a request is allowed through the breaker."""
        s = self.state
        if s == self.CLOSED:
            return True
        if s == self.HALF_OPEN:
            return True  # allow one test call
        return False  # OPEN → blocked

    @property
    def seconds_until_retry(self) -> float:
        """How many seconds until the breaker allows a retry."""
        if self._state != self.OPEN:
            return 0.0
        elapsed = time.monotonic() - self._opened_at
        remaining = self._cooldown_seconds - elapsed
        return max(0.0, remaining)


class TelegramNotifier:
    """Sends formatted messages to a Telegram chat.

    MELHORIA 20: Integra circuit breaker para evitar tentativas
    repetidas quando o Telegram está fora do ar.
    """

    def __init__(self, token: str, chat_id: str) -> None:
        self.bot = Bot(token=token)
        self.chat_id = chat_id
        self._paused = False
        self._breaker = _CircuitBreaker(failure_threshold=5, cooldown_seconds=60.0)

    def pause(self) -> None:
        self._paused = True
        logger.info("Telegram alerts paused")

    def resume(self) -> None:
        self._paused = False
        logger.info("Telegram alerts resumed")

    async def send_message(self, text: str, parse_mode: str = ParseMode.HTML) -> int | None:
        """Send a raw message with retry logic + circuit breaker.

        Returns message_id on success, None on failure.
        """
        if not self._breaker.allow_request():
            remaining = self._breaker.seconds_until_retry
            logger.warning(
                f"Circuit breaker OPEN — skipping Telegram send "
                f"(retry in {remaining:.0f}s)"
            )
            return None

        for attempt in range(3):
            try:
                msg = await self.bot.send_message(
                    chat_id=self.chat_id,
                    text=text,
                    parse_mode=parse_mode,
                    disable_web_page_preview=True,
                )
                self._breaker.record_success()
                return msg.message_id
            except TelegramError as e:
                logger.warning(f"Telegram send failed (attempt {attempt + 1}/3): {e}")
                self._breaker.record_failure()
                if attempt < 2:
                    # Backoff exponencial com jitter
                    base_wait = 2 ** attempt
                    jitter = random.uniform(0, base_wait * 0.5)
                    await asyncio.sleep(base_wait + jitter)

                    # Se o breaker abriu durante os retries, abortar
                    if not self._breaker.allow_request():
                        logger.warning("Circuit breaker tripped during retries — aborting")
                        break

        logger.error("Failed to send Telegram message after retries")
        return None

    async def edit_message(self, message_id: int, text: str, parse_mode: str = ParseMode.HTML) -> bool:
        """Edit an existing message with circuit breaker. Returns True on success."""
        if not self._breaker.allow_request():
            remaining = self._breaker.seconds_until_retry
            logger.warning(
                f"Circuit breaker OPEN — skipping Telegram edit "
                f"(retry in {remaining:.0f}s)"
            )
            return False

        for attempt in range(3):
            try:
                await self.bot.edit_message_text(
                    chat_id=self.chat_id,
                    message_id=message_id,
                    text=text,
                    parse_mode=parse_mode,
                    disable_web_page_preview=True,
                )
                self._breaker.record_success()
                return True
            except TelegramError as e:
                logger.warning(f"Telegram edit failed (attempt {attempt + 1}/3): {e}")
                self._breaker.record_failure()
                if attempt < 2:
                    base_wait = 2 ** attempt
                    jitter = random.uniform(0, base_wait * 0.5)
                    await asyncio.sleep(base_wait + jitter)

                    if not self._breaker.allow_request():
                        logger.warning("Circuit breaker tripped during retries — aborting")
                        break

        logger.error(f"Failed to edit message {message_id} after retries")
        return False

    async def send_alert(self, alert_data: dict) -> int | None:
        """Format and send a betting opportunity alert. Returns message_id."""
        if self._paused:
            logger.debug("Alerts paused, skipping send_alert")
            return None
        from src.telegram.messages import format_alert
        text = format_alert(alert_data)
        logger.bind(category="alert").info(
            f"Sending alert: {alert_data.get('losing_player')} "
            f"{alert_data.get('alert_label')} {alert_data.get('alert_odds')}"
        )
        return await self.send_message(text)

    async def edit_alert_result(self, message_id: int, original_data: dict, hit: bool, score_line: str) -> bool:
        """Edit the original alert message to show the result."""
        from src.telegram.messages import format_alert
        original_text = format_alert(original_data)

        if hit:
            result_line = f"\n\n\u2705 GREEN — {score_line}"
        else:
            result_line = f"\n\n\u274C RED — {score_line}"

        return await self.edit_message(message_id, original_text + result_line)

    async def send_validation(self, validation_data: dict) -> int | None:
        """Send post-game result validation."""
        from src.telegram.messages import format_validation
        return await self.send_message(format_validation(validation_data))

    async def send_daily_report(self, report_data: dict) -> int | None:
        """Send daily performance summary."""
        from src.telegram.messages import format_daily_report
        return await self.send_message(format_daily_report(report_data))

    async def send_cold_start_progress(self, progress_data: dict) -> int | None:
        """Send cold start collection progress update."""
        from src.telegram.messages import format_cold_start_progress
        return await self.send_message(format_cold_start_progress(progress_data))

    async def send_regime_warning(self, regime_data: dict) -> int | None:
        """Send regime degradation warning."""
        from src.telegram.messages import format_regime_warning
        return await self.send_message(format_regime_warning(regime_data))

    async def send_system_status(self, status_data: dict) -> int | None:
        """Send system health status."""
        from src.telegram.messages import format_system_status
        return await self.send_message(format_system_status(status_data))
