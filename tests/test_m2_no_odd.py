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
    # sem odd: nao pode auto-deletar antes do ValidatorV2 editar com resultado
    notifier.send_watch.assert_awaited_once()
    assert notifier.send_watch.await_args.kwargs["auto_delete_seconds"] == 0
    # sem odd: o watch M2 E o sinal -> vai pro grupo do Method 2 (fifafriends),
    # nao pro DM do owner.
    assert notifier.send_watch.await_args.kwargs.get("to_v2_group") is True
    assert notifier.send_watch.await_args.kwargs.get("to_admin") is not True


async def test_emit_watch_m2_auto_delete_900_com_odd(monkeypatch):
    """Modo legado (flag True): reversibilidade — auto-delete continua 900s."""
    from src.config import settings
    monkeypatch.setattr(settings, "bet365_live_odds_enabled", True)

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

    notifier.send_watch.assert_awaited_once()
    assert notifier.send_watch.await_args.kwargs["auto_delete_seconds"] == 900
    # legado (modo com odd): watch M2 continua indo pro DM do owner (pre-aviso)
    assert notifier.send_watch.await_args.kwargs.get("to_admin") is True
    assert notifier.send_watch.await_args.kwargs.get("to_v2_group") is not True
    repo.create.assert_not_awaited()  # legado: nao persiste sem odd


async def test_send_watch_to_v2_group_vai_pro_grupo_do_method2():
    """send_watch(to_v2_group=True) envia pro TELEGRAM_GROUP_V2_ID, nao pro DM."""
    from src.telegram.bot import TelegramNotifier

    n = TelegramNotifier(
        token="1:x", chat_id="-100vip", admin_chat_id="6034412176",
        v2_group_id="-1003740732998",
    )
    n.bot = MagicMock()
    m = MagicMock()
    m.message_id = 555
    n.bot.send_message = AsyncMock(return_value=m)

    watch_data = {
        "target_player": "A", "player_home": "A", "player_away": "B",
        "kickoff_str": "12:00",
        "lines": [{"line": "over25", "line_label": "Over 2.5", "target_odds": 1.80,
                   "predicted_tp": 0.7, "qualified": True}],
    }
    mid = await n.send_watch(watch_data, auto_delete_seconds=0, to_v2_group=True)

    assert mid == 555
    assert n.bot.send_message.await_args.kwargs["chat_id"] == "-1003740732998"
