"""M1 no modo sem odd: pre-alerta VIP sem odd + persiste Alert sem odd."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.telegram.messages import format_watch_message


def test_watch_message_vip_sem_odd(monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "bet365_live_odds_enabled", False)
    d = {
        "vip_clean_mode": True,
        "method": "M1",
        "player_home": "A", "player_away": "B",
        "target_player": "A",
        "kickoff_str": "19:43",
        "lines_eligible": [{"line_label": "Over 2.5", "target_odds": 1.80}],
    }
    t = format_watch_message(d)
    assert "Over 2.5" in t
    assert "1.80" not in t   # odd-alvo escondida
    assert "@" not in t


@pytest.mark.asyncio
async def test_emit_watch_m1_persiste_alert_sem_odd(monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "bet365_live_odds_enabled", False)

    candidate = {
        "line": "over25", "line_label": "Over 2.5", "target_player": "A",
        "target_odds": 1.80,
        "lines": [{"line": "over25", "line_label": "Over 2.5", "target_odds": 1.80,
                   "predicted_tp": 0.7, "qualified": True}],
    }
    stats = MagicMock()
    stats.predict_watch_candidate = AsyncMock(return_value=candidate)

    repo = MagicMock()
    repo.exists_for_match = AsyncMock(return_value=False)
    repo.create = AsyncMock(return_value=MagicMock(id=42))
    repo.update_telegram_message_id = AsyncMock()

    notifier = MagicMock()
    notifier.send_watch = AsyncMock(return_value=777)

    # _emit_watch_m1 le o notifier via self.alert_engine.notifier (nao
    # self.notifier) — o engine precisa expor o mesmo objeto mockado.
    engine = MagicMock(stats=stats, alerts=repo, notifier=notifier)

    m = _make_odds_monitor_stub(engine, notifier)

    rm = MagicMock(id=5)
    rm.game1_id = 5
    g1 = MagicMock(id=5, player_home="A", player_away="B", score_home=0, score_away=1)

    await m._emit_watch_m1(rm, g1, "A", "B", 0)

    repo.create.assert_awaited_once()
    kw = repo.create.await_args.kwargs
    assert kw["match_id"] == 5
    assert kw["best_line"] == "over25"
    assert kw.get("over25_odds") is None
    repo.update_telegram_message_id.assert_awaited_once_with(42, 777)


@pytest.mark.asyncio
async def test_emit_watch_m1_nao_persiste_quando_id_none(monkeypatch):
    """Match sintetico (return_match.id None) no modo sem odd: nao ha linha
    real em `matches` pra persistir contra (FK invalida), entao o guard
    `if return_match.id is not None` deve pular a persistencia sem crashar —
    o watch (mensagem Telegram) ainda e enviado normalmente."""
    from src.config import settings
    monkeypatch.setattr(settings, "bet365_live_odds_enabled", False)

    candidate = {
        "line": "over25", "line_label": "Over 2.5", "target_player": "A",
        "target_odds": 1.80,
        "lines": [{"line": "over25", "line_label": "Over 2.5", "target_odds": 1.80,
                   "predicted_tp": 0.7, "qualified": True}],
    }
    stats = MagicMock()
    stats.predict_watch_candidate = AsyncMock(return_value=candidate)

    repo = MagicMock()
    repo.exists_for_match = AsyncMock(return_value=False)
    repo.create = AsyncMock(return_value=MagicMock(id=42))
    repo.update_telegram_message_id = AsyncMock()

    notifier = MagicMock()
    notifier.send_watch = AsyncMock(return_value=777)

    engine = MagicMock(stats=stats, alerts=repo, notifier=notifier)

    m = _make_odds_monitor_stub(engine, notifier)

    rm = MagicMock(id=None)  # match sintetico (SyntheticReturnMatch)
    rm.game1_id = 5
    g1 = MagicMock(id=5, player_home="A", player_away="B", score_home=0, score_away=1)

    result = await m._emit_watch_m1(rm, g1, "A", "B", 0)

    assert result is True  # watch (mensagem) ainda foi enviado
    repo.create.assert_not_awaited()
    repo.update_telegram_message_id.assert_not_awaited()


def _make_odds_monitor_stub(engine, notifier):
    from src.core.odds_monitor import OddsMonitor
    m = OddsMonitor.__new__(OddsMonitor)
    m.alert_engine = engine
    m.notifier = notifier
    m._predictive_sent = set()
    m._WATCH_AUTO_DELETE_SECONDS = 900
    m.blocked_repo = None
    return m
