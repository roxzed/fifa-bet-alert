"""Method 3 — engine de alerta. Persiste (uma row por linha) e envia UMA
mensagem no privado do owner com todas as linhas qualificadas cujas odds
estão no gate. Nunca interfere em M1/M2."""

from __future__ import annotations

from loguru import logger

from src.config import settings
from src.core.stats_engine_v3 import M3_LINE_LABELS


class AlertEngineV3:
    def __init__(self, stats_engine_v3, alert_v3_repo, notifier) -> None:
        self.stats = stats_engine_v3
        self.alerts = alert_v3_repo
        self.notifier = notifier

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
        minutes_to_kickoff: int = 0,
        loser_goals_g1: int = 0,
        bet365_url: str = "",
    ) -> bool:
        evaluation = await self.stats.evaluate(loser, winner)
        if not evaluation.should_alert:
            return False

        odds_map = {
            "over15": over15_odds,
            "over25": over25_odds or None,
            "over35": over35_odds,
            "over45": over45_odds,
        }

        # Linhas qualificadas com odd presente e dentro do gate
        sendable = []
        for le in evaluation.lines:
            odd = odds_map.get(le.line)
            if odd is None or odd <= 0:
                continue
            if not (settings.min_odds <= odd <= settings.max_odds):
                logger.info(
                    f"M3 {loser}: {le.line}@{odd:.2f} fora do gate "
                    f"[{settings.min_odds}-{settings.max_odds}]"
                )
                continue
            if await self.alerts.exists_for_line(return_match.id, le.line):
                continue
            sendable.append((le, odd))

        if not sendable:
            return False

        # Placar do G1 na perspectiva do perdedor ("loser-opp")
        if game1_match.player_home == loser:
            g1_score = f"{game1_match.score_home}-{game1_match.score_away}"
        else:
            g1_score = f"{game1_match.score_away}-{game1_match.score_home}"

        # Persistir ANTES de enviar (alerta existe mesmo se Telegram falhar)
        created = []
        for le, odd in sendable:
            alert = await self.alerts.create(
                match_id=return_match.id,
                losing_player=loser,
                opponent_player=winner,
                game1_score=g1_score,
                line=le.line,
                odds=odd,
                rate=le.rate,
                hits=le.hits,
                n_h2h=le.n,
                recent_hits=le.recent_hits,
            )
            created.append((alert, le, odd))

        alert_data = {
            "kickoff_str": "",
            "player_home": return_match.player_home,
            "player_away": return_match.player_away,
            "target_player": loser,
            "game1_score": g1_score,
            "bet365_url": bet365_url,
            "lines": [
                {
                    "line": le.line,
                    "line_label": M3_LINE_LABELS[le.line],
                    "rate": le.rate,
                    "hits": le.hits,
                    "n": le.n,
                    "recent_hits": le.recent_hits,
                    "recent_n": le.recent_n,
                    "odds": odd,
                }
                for _, le, odd in created
            ],
        }
        message_id = await self.notifier.send_alert_v3(alert_data)
        if message_id:
            for alert, _, _ in created:
                await self.alerts.update_telegram_message_id(alert.id, message_id)
        return True
