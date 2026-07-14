"""Method 3 — validação pós-jogo: GREEN/RED por linha + edição da mensagem.

Mesmo espírito do ValidatorV2 (loop de polling, task nomeada, cancelamento
via flag + task.cancel()) mas operando sobre alerts_v3 (uma row por linha,
sem `best_line`/`camada`) e editando a mensagem única do privado do owner.
"""

from __future__ import annotations

import asyncio

from loguru import logger

from src.core.stats_engine_v3 import M3_LINE_LABELS

_THRESHOLDS = {"over15": 1.5, "over25": 2.5, "over35": 3.5, "over45": 4.5}


class ValidatorV3:
    """Valida alertas M3 (uma row por linha) apos o termino dos jogos de volta."""

    def __init__(self, match_repo, alert_v3_repo, notifier, poll_seconds: int = 60) -> None:
        self.matches = match_repo
        self.alerts = alert_v3_repo
        self.notifier = notifier
        self.poll_seconds = poll_seconds
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="validator_v3")
        logger.info("ValidatorV3 started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()

    async def _loop(self) -> None:
        while self._running:
            try:
                for match in await self.matches.get_unvalidated_return_matches_v3():
                    await self.validate_match(match)
            except Exception as e:
                logger.error(f"ValidatorV3 cycle error: {e}")
            await asyncio.sleep(self.poll_seconds)

    async def validate_match(self, match) -> None:
        """Valida (GREEN/RED por linha) e edita a mensagem M3 de um jogo de volta."""
        alerts = [a for a in await self.alerts.get_all_by_match_id(match.id) if a.hit is None]
        if not alerts:
            return
        loser = alerts[0].losing_player
        loser_goals = match.score_home if match.player_home == loser else match.score_away
        if loser_goals is None:
            return

        results = []
        message_id = None
        for alert in alerts:
            hit = loser_goals > _THRESHOLDS[alert.line]
            profit = (alert.odds - 1.0) if (hit and alert.odds) else -1.0
            await self.alerts.validate(
                alert_id=alert.id, actual_goals=loser_goals, hit=hit, profit_flat=profit
            )
            results.append({
                "line_label": M3_LINE_LABELS[alert.line],
                "hit": hit,
                "actual_goals": loser_goals,
            })
            message_id = message_id or alert.telegram_message_id
            logger.bind(category="alert_v3").info(
                f"M3 validado: {loser} {alert.line} → {'GREEN' if hit else 'RED'} "
                f"({loser_goals} gols)"
            )

        if message_id:
            alert_data = {
                "player_home": match.player_home,
                "player_away": match.player_away,
                "target_player": loser,
                "game1_score": alerts[0].game1_score,
                "lines": [
                    {
                        "line": a.line,
                        "line_label": M3_LINE_LABELS[a.line],
                        "rate": a.rate,
                        "hits": a.hits,
                        "n": a.n_h2h,
                        "recent_hits": a.recent_hits,
                        "recent_n": 7,
                        "odds": a.odds,
                    }
                    for a in alerts
                ],
            }
            await self.notifier.edit_alert_v3_result(message_id, alert_data, results)
