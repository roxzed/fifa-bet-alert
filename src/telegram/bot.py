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
import sys
import time

from loguru import logger
from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import TelegramError


def _sanitize_text(text: str) -> str:
    """Remove surrogates invalidos que causam UnicodeEncodeError.

    O erro 'surrogates not allowed' acontece quando strings contem
    UTF-16 surrogates soltos (ex: \\ud83d\\udd04) em vez do codepoint
    correto (ex: \\U0001f504). Emojis reais (UTF-8) passam normalmente.
    """
    try:
        text.encode("utf-8")
        return text
    except UnicodeEncodeError:
        return text.encode("utf-8", errors="replace").decode("utf-8")


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

    def __init__(self, token: str, chat_id: str, group_chat_id: str = "",
                 v2_group_id: str = "") -> None:
        self.bot = Bot(token=token)
        self.chat_id = chat_id
        self._group_chat_id = group_chat_id
        self._v2_group_id = v2_group_id  # Grupo do Method 2
        self._paused = False
        self._breaker = _CircuitBreaker(failure_threshold=5, cooldown_seconds=60.0)
        self._breaker_v2 = _CircuitBreaker(failure_threshold=5, cooldown_seconds=60.0)
        # Mapeia chat_message_id -> group_message_id para editar resultado no grupo
        self._group_msg_map: dict[int, int] = {}

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
        text = _sanitize_text(text)

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
        text = _sanitize_text(text)

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
        msg_id = await self.send_message(text)

        # Enviar tambem no grupo, se configurado
        if self._group_chat_id and msg_id:
            try:
                group_msg = await self.bot.send_message(
                    chat_id=self._group_chat_id,
                    text=_sanitize_text(text),
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                self._group_msg_map[msg_id] = group_msg.message_id
            except Exception as e:
                logger.warning(f"Failed to send alert to group: {e}")

        return msg_id

    async def edit_alert_result(self, message_id: int, original_data: dict, hit: bool, score_line: str) -> bool:
        """Edit the original alert message to show the result (chat + grupo)."""
        from src.telegram.messages import format_alert
        original_text = format_alert(original_data)

        if hit:
            result_line = f"\n\n\u2705 GREEN — {score_line}"
        else:
            result_line = f"\n\n\u274C RED — {score_line}"

        full_text = original_text + result_line
        success = await self.edit_message(message_id, full_text)

        # Editar tambem no grupo
        group_msg_id = self._group_msg_map.pop(message_id, None)
        if group_msg_id and self._group_chat_id:
            try:
                await self.bot.edit_message_text(
                    chat_id=self._group_chat_id,
                    message_id=group_msg_id,
                    text=_sanitize_text(full_text),
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                logger.info(f"Group message {group_msg_id} edited with result")
            except TelegramError as e:
                logger.warning(f"Failed to edit group message {group_msg_id}: {e}")

        return success

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

    # --- Method 2 (M2) group methods ---

    async def send_alert_v2(self, alert_data: dict) -> int | None:
        """Format and send M2 alert to the V2 group. Returns message_id."""
        v2_group = getattr(self, '_v2_group_id', None)
        if not v2_group:
            logger.debug("M2 group not configured, skipping send_alert_v2")
            return None
        if not self._breaker_v2.allow_request():
            logger.warning(
                f"M2 circuit breaker OPEN — skipping send_alert_v2 "
                f"(retry in {self._breaker_v2.seconds_until_retry:.0f}s)"
            )
            return None
        from src.telegram.messages import format_alert_v2
        text = _sanitize_text(format_alert_v2(alert_data))
        try:
            msg = await self.bot.send_message(
                chat_id=v2_group,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            self._breaker_v2.record_success()
            logger.bind(category="alert_v2").info(
                f"M2 alert sent to group: {alert_data.get('losing_player')} "
                f"{alert_data.get('camada')} {alert_data.get('alert_label')}"
            )
            return msg.message_id
        except TelegramError as e:
            self._breaker_v2.record_failure()
            logger.error(f"Failed to send M2 alert to group: {e}")
            return None

    async def edit_alert_v2_result(self, message_id: int, alert, return_match,
                                   hit: bool, score_line: str) -> bool:
        """Edit M2 alert message with result."""
        v2_group = getattr(self, '_v2_group_id', None)
        if not v2_group:
            return False
        from src.telegram.messages import format_alert_v2

        # Reconstruir dados do G1 a partir do jogo de volta (G1 e G2 trocam home/away,
        # mas cada jogador mantém o mesmo time em ambos os jogos).
        loser = alert.losing_player
        rm_ph = getattr(return_match, 'player_home', '') or ''
        rm_pa = getattr(return_match, 'player_away', '') or ''
        rm_th = getattr(return_match, 'team_home', '') or ''
        rm_ta = getattr(return_match, 'team_away', '') or ''

        # Separar placar G1 armazenado como "loser_goals-opp_goals"
        g1_parts = (alert.game1_score or "?-?").split("-")
        loser_g1 = g1_parts[0] if len(g1_parts) >= 2 else "?"
        opp_g1 = g1_parts[1] if len(g1_parts) >= 2 else "?"

        if rm_ph == loser:
            # G2: loser(home) vs opp(away) → G1: opp(home) vs loser(away)
            g1_player_home = rm_pa
            g1_player_away = loser
            g1_team_home = rm_ta
            g1_team_away = rm_th
            g1_score_home = opp_g1
            g1_score_away = loser_g1
        else:
            # G2: opp(home) vs loser(away) → G1: loser(home) vs opp(away)
            g1_player_home = loser
            g1_player_away = rm_ph
            g1_team_home = rm_ta
            g1_team_away = rm_th
            g1_score_home = loser_g1
            g1_score_away = opp_g1

        # Rebuild alert_data from alert object
        alert_data = {
            "camada": alert.camada,
            "best_line": alert.best_line,
            "alert_label": self._line_label(alert.best_line, alert.losing_player),
            "alert_odds": self._line_odds(alert),
            "prob": alert.prob,
            "sample_size": alert.sample_size,
            "prob_4elem": alert.prob_4elem,
            "prob_3elem": alert.prob_3elem,
            "sample_4elem": alert.sample_4elem,
            "sample_3elem": alert.sample_3elem,
            "losing_player": alert.losing_player,
            "game1_player_home": g1_player_home,
            "game1_player_away": g1_player_away,
            "game1_score_home": g1_score_home,
            "game1_score_away": g1_score_away,
            "game1_team_home": g1_team_home,
            "game1_team_away": g1_team_away,
            "return_player_home": rm_ph,
            "return_player_away": rm_pa,
            "kickoff_time": getattr(return_match, 'started_at', None),
            "minutes_to_kickoff": 0,
            "bet365_url": "",
        }
        original_text = format_alert_v2(alert_data)
        result_line = f"\n\n\u2705 GREEN — {score_line}" if hit else f"\n\n\u274c RED — {score_line}"
        full_text = _sanitize_text(original_text + result_line)
        try:
            await self.bot.edit_message_text(
                chat_id=v2_group,
                message_id=message_id,
                text=full_text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            self._breaker_v2.record_success()
            return True
        except TelegramError as e:
            self._breaker_v2.record_failure()
            logger.warning(f"Failed to edit M2 message {message_id}: {e}")
            return False

    async def send_message_v2(self, text: str) -> int | None:
        """Send a raw message to the M2 group."""
        v2_group = getattr(self, '_v2_group_id', None)
        if not v2_group:
            return None
        if not self._breaker_v2.allow_request():
            logger.warning(
                f"M2 circuit breaker OPEN — skipping send_message_v2 "
                f"(retry in {self._breaker_v2.seconds_until_retry:.0f}s)"
            )
            return None
        text = _sanitize_text(text)
        try:
            msg = await self.bot.send_message(
                chat_id=v2_group,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            self._breaker_v2.record_success()
            return msg.message_id
        except TelegramError as e:
            self._breaker_v2.record_failure()
            logger.error(f"Failed to send M2 message: {e}")
            return None

    @staticmethod
    def _line_label(best_line: str | None, player: str) -> str:
        labels = {"over15": "Over 1.5", "over25": "Over 2.5",
                  "over35": "Over 3.5", "over45": "Over 4.5"}
        return f"{labels.get(best_line or 'over25', best_line)} gols {player}"

    @staticmethod
    def _line_odds(alert) -> float:
        bl = alert.best_line or "over25"
        if bl == "over45":
            return alert.over45_odds or 0
        elif bl == "over35":
            return alert.over35_odds or 0
        elif bl == "over15":
            return alert.over15_odds or 0
        else:
            return alert.over25_odds or 0
