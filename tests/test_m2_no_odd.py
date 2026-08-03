"""M2 no modo sem odd: pre-alerta DM persiste AlertV2 sem odd."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from src.core.odds_monitor import OddsMonitor


async def test_emit_watch_m2_persiste_alertv2_sem_odd(monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "bet365_live_odds_enabled", False)

    candidate = {
        "line": "over25", "line_label": "Over 2.5", "target_player": "A",
        "target_odds": 1.80, "camada": "C2",
        "lines": [{"line": "over25", "line_label": "Over 2.5", "target_odds": 1.80,
                   "predicted_tp": 0.7, "qualified": True}],
    }
    stats = MagicMock()
    stats.predict_watch_candidate = AsyncMock(return_value=candidate)

    repo = MagicMock()
    repo.exists_for_match = AsyncMock(return_value=False)
    repo.create = AsyncMock(return_value=MagicMock(id=7))
    repo.update_telegram_message_id = AsyncMock()

    blocked = MagicMock()
    blocked.is_suppressed = AsyncMock(return_value=False)

    engine = MagicMock(stats=stats, alerts=repo, blocked=blocked)
    notifier = MagicMock()
    notifier.send_watch = AsyncMock(return_value=888)

    m = OddsMonitor.__new__(OddsMonitor)
    m.alert_engine_v2 = engine
    m.alert_engine = MagicMock(notifier=notifier)
    m._predictive_sent = set()
    m._WATCH_AUTO_DELETE_SECONDS = 900

    rm = MagicMock(id=8)
    rm.game1_id = 8
    rm.started_at = datetime(2026, 1, 1, 12, 0, 0)
    rm.player_home = "A"
    rm.player_away = "B"
    g1 = MagicMock(id=8, player_home="A", player_away="B", score_home=0, score_away=1)

    await m._emit_watch_m2(rm, g1, "A", "B", 0)

    repo.create.assert_awaited_once()
    kw = repo.create.await_args.kwargs
    assert kw["match_id"] == 8
    assert kw["best_line"] == "over25"
    assert kw["camada"] == "C2"
    assert kw.get("over25_odds") is None
    repo.update_telegram_message_id.assert_awaited_once_with(7, 888)
