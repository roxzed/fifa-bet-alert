"""Method 3 — validação pós-jogo: GREEN/RED por linha + edição da mensagem.

Mesmo espírito do ValidatorV2 (loop de polling BLOQUEANTE rodado sob
_supervised_task no main.py, com crash-restart automático; `stop()` apenas
sinaliza uma flag síncrona) mas operando sobre alerts_v3 (uma row por linha,
sem `best_line`/`camada`) e editando a mensagem única do privado do owner.
"""

from __future__ import annotations

import asyncio

from loguru import logger

from src.core.stats_engine_v3 import M3_LINE_LABELS

_THRESHOLDS = {"over15": 1.5, "over25": 2.5, "over35": 3.5, "over45": 4.5}


class ValidatorV3:
    """Valida alertas M3 (uma row por linha) apos o termino dos jogos de volta."""

    def __init__(self, match_repo, alert_v3_repo, notifier) -> None:
        self.matches = match_repo
        self.alerts = alert_v3_repo
        self.notifier = notifier
        self._running = False

    async def start(self, poll_interval: int = 60) -> None:
        self._running = True
        logger.info("ValidatorV3 started")
        while self._running:
            try:
                for match in await self.matches.get_unvalidated_return_matches_v3():
                    await self.validate_match(match)
            except Exception as e:
                logger.error(f"ValidatorV3 cycle error: {e}")
            await asyncio.sleep(poll_interval)

    def stop(self) -> None:
        self._running = False

    async def validate_match(self, match) -> None:
        """Valida (GREEN/RED por linha) e edita a mensagem M3 de um jogo de volta.

        Idempotente: o edit sempre reflete TODAS as linhas do match (graduando
        cada uma pelo placar), mesmo que um retry parcial tenha deixado algumas
        já validadas em ciclos anteriores. Só grava (`validate`) as pendentes.
        """
        all_alerts = list(await self.alerts.get_all_by_match_id(match.id))
        pending = [a for a in all_alerts if a.hit is None]
        if not pending:
            return
        loser = all_alerts[0].losing_player
        loser_goals = match.score_home if match.player_home == loser else match.score_away
        if loser_goals is None:
            return

        # WRITE: só grava as linhas ainda não validadas (não regrava as graduadas)
        for alert in pending:
            hit = loser_goals > _THRESHOLDS[alert.line]
            profit = (alert.odds - 1.0) if (hit and alert.odds) else -1.0
            await self.alerts.validate(
                alert_id=alert.id, actual_goals=loser_goals, hit=hit, profit_flat=profit
            )
            logger.bind(category="alert_v3").info(
                f"M3 validado: {loser} {alert.line} → {'GREEN' if hit else 'RED'} "
                f"({loser_goals} gols)"
            )

        # PAYLOAD do edit: construído a partir de TODOS os alertas do match, para
        # que a mensagem sempre mostre todas as linhas (não some a já graduada).
        message_id = None
        for alert in all_alerts:
            message_id = message_id or alert.telegram_message_id

        if message_id:
            results = [
                {
                    "line_label": M3_LINE_LABELS[a.line],
                    "hit": loser_goals > _THRESHOLDS[a.line],
                    "actual_goals": loser_goals,
                }
                for a in all_alerts
            ]
            alert_data = {
                "player_home": match.player_home,
                "player_away": match.player_away,
                "target_player": loser,
                "game1_score": all_alerts[0].game1_score,
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
                    for a in all_alerts
                ],
            }
            await self.notifier.edit_alert_v3_result(message_id, alert_data, results)
