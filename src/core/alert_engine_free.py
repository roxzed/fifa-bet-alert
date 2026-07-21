"""Modelo FREE — orquestra o pre-alerta publico (linha de maior taxa)."""

from __future__ import annotations

from typing import Any

from loguru import logger

from src.core.free_status import LINE_LABELS


class AlertEngineFree:
    def __init__(self, stats_engine_v3: Any, alert_free_repo: Any, notifier: Any) -> None:
        self.stats = stats_engine_v3
        self.alerts = alert_free_repo
        self.notifier = notifier

    async def prealert(
        self, return_match: Any, game1_match: Any, loser: str, winner: str, kickoff_str: str
    ) -> str | None:
        """Se o criterio M3 qualifica, envia pre-alerta FREE da linha de maior
        taxa. Retorna a `line` alertada (pra o monitor rastrear a odd) ou None."""
        if await self.alerts.exists_for_match(return_match.id):
            return None
        evaluation = await self.stats.evaluate(loser, winner)
        if not evaluation.should_alert or not evaluation.lines:
            return None
        best = max(evaluation.lines, key=lambda le: le.rate)
        if game1_match.player_home == loser:
            g1_score = f"{game1_match.score_home}-{game1_match.score_away}"
        else:
            g1_score = f"{game1_match.score_away}-{game1_match.score_home}"
        alert = await self.alerts.create(
            match_id=return_match.id,
            losing_player=loser,
            opponent_player=winner,
            game1_score=g1_score,
            line=best.line,
            rate=best.rate,
            hits=best.hits,
            n_h2h=best.n,
            recent_hits=best.recent_hits,
        )
        data = {"player": loser, "line_label": LINE_LABELS[best.line], "kickoff_str": kickoff_str}
        msg_id = await self.notifier.send_watch_free(data)
        if msg_id:
            await self.alerts.update_telegram_message_id(alert.id, msg_id)
        logger.info(
            f"FREE pre-alerta {loser} {best.line} rate={best.rate:.0%} (match {return_match.id})"
        )
        return best.line  # type: ignore
