"""Modelo FREE — validacao pos-jogo: GREEN/RED/VOID + edicao da mensagem."""

from __future__ import annotations

import asyncio

from loguru import logger

from src.config import settings
from src.core.free_status import LINE_LABELS, decide_status


class ValidatorFree:
    def __init__(self, match_repo, alert_free_repo, notifier) -> None:
        self.matches = match_repo
        self.alerts = alert_free_repo
        self.notifier = notifier
        self._running = False

    async def start(self, poll_interval: int = 60) -> None:
        self._running = True
        logger.info("ValidatorFree started")
        while self._running:
            try:
                for match in await self.matches.get_unvalidated_return_matches_free():
                    await self.validate_match(match)
            except Exception as e:
                logger.error(f"ValidatorFree cycle error: {e}")
            await asyncio.sleep(poll_interval)

    def stop(self) -> None:
        self._running = False

    async def validate_match(self, match) -> None:
        pending = [a for a in await self.alerts.get_all_by_match_id(match.id) if a.hit is None]
        if not pending:
            return
        loser = pending[0].losing_player
        loser_goals = match.score_home if match.player_home == loser else match.score_away
        if loser_goals is None:
            return
        for a in pending:
            status, hit = decide_status(a.entry_odd, loser_goals, a.line, settings.free_min_odd)
            await self.alerts.validate(a.id, actual_goals=loser_goals, hit=hit, status=status)
            logger.bind(category="free_model").info(
                f"FREE validado: {loser} {a.line} -> {status.upper()} "
                f"({loser_goals} gols, entry_odd={a.entry_odd})"
            )
            if a.telegram_message_id:
                data = {
                    "player": loser,
                    "line_label": LINE_LABELS[a.line],
                    "actual_goals": loser_goals,
                    "entry_odd": a.entry_odd,
                }
                await self.notifier.edit_free_result(a.telegram_message_id, data, status)
