"""M3 no modo sem odd: o pre-alerta (watch) persiste AlertV3 sem odd."""

from unittest.mock import AsyncMock, MagicMock

from src.core.odds_monitor import OddsMonitor


async def test_emit_watch_m3_persiste_alertv3_sem_odd(monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "bet365_live_odds_enabled", False)

    # evaluation com 1 linha qualificada
    line = MagicMock(line="over25", line_label="Over 2.5", rate=0.7, hits=14, n=20,
                     recent_hits=5, recent_n=7, qualified=True)
    evaluation = MagicMock(should_alert=True, lines=[line])

    stats = MagicMock()
    stats.evaluate = AsyncMock(return_value=evaluation)

    repo = MagicMock()
    repo.exists_for_match = AsyncMock(return_value=False)
    created = MagicMock(id=99)
    repo.create = AsyncMock(return_value=created)
    repo.update_telegram_message_id = AsyncMock()

    engine_v3 = MagicMock(stats=stats, alerts=repo)

    notifier = MagicMock()
    notifier.send_watch_v3 = AsyncMock(return_value=555)

    m = OddsMonitor.__new__(OddsMonitor)
    m.alert_engine_v3 = engine_v3
    m.alert_engine = MagicMock(notifier=notifier)
    m.notifier = notifier
    m._predictive_sent = set()
    m._WATCH_AUTO_DELETE_SECONDS = 900

    rm = MagicMock(id=1)
    rm.game1_id = 1
    g1 = MagicMock(id=1, player_home="A", player_away="B", score_home=0, score_away=2)

    await m._emit_watch_m3(rm, g1, "A", "B")

    repo.create.assert_awaited_once()
    kwargs = repo.create.await_args.kwargs
    assert kwargs["match_id"] == 1
    assert kwargs["line"] == "over25"
    assert kwargs.get("odds") is None
    repo.update_telegram_message_id.assert_awaited_once_with(99, 555)


def _stub_watch_m3():
    """Monta os stubs de _emit_watch_m3 (evaluation com 1 linha qualificada)."""
    line = MagicMock(line="over25", line_label="Over 2.5", rate=0.7, hits=14, n=20,
                     recent_hits=5, recent_n=7, qualified=True)
    evaluation = MagicMock(should_alert=True, lines=[line])

    stats = MagicMock()
    stats.evaluate = AsyncMock(return_value=evaluation)

    repo = MagicMock()
    repo.exists_for_match = AsyncMock(return_value=False)
    created = MagicMock(id=99)
    repo.create = AsyncMock(return_value=created)
    repo.update_telegram_message_id = AsyncMock()

    engine_v3 = MagicMock(stats=stats, alerts=repo)

    notifier = MagicMock()
    notifier.send_watch_v3 = AsyncMock(return_value=555)

    m = OddsMonitor.__new__(OddsMonitor)
    m.alert_engine_v3 = engine_v3
    m.alert_engine = MagicMock(notifier=notifier)
    m.notifier = notifier
    m._predictive_sent = set()
    m._WATCH_AUTO_DELETE_SECONDS = 900

    rm = MagicMock(id=1)
    rm.game1_id = 1
    g1 = MagicMock(id=1, player_home="A", player_away="B", score_home=0, score_away=2)
    return m, repo, rm, g1


async def test_emit_watch_m3_com_flag_true_nao_persiste_alertv3(monkeypatch):
    # Modo legado (com odd): flag True -> comportamento antigo, sem persistir
    # AlertV3 sem odd. repo.create NAO deve ser chamado.
    from src.config import settings
    monkeypatch.setattr(settings, "bet365_live_odds_enabled", True)

    m, repo, rm, g1 = _stub_watch_m3()

    await m._emit_watch_m3(rm, g1, "A", "B")

    repo.create.assert_not_awaited()


async def test_emit_watch_m3_pula_criacao_se_ja_existe(monkeypatch):
    # Guard de duplicata: se ja existe AlertV3 pra esse match_id, nao recria
    # (evita duplicar linhas em retries/reprocessamento).
    from src.config import settings
    monkeypatch.setattr(settings, "bet365_live_odds_enabled", False)

    m, repo, rm, g1 = _stub_watch_m3()
    repo.exists_for_match = AsyncMock(return_value=True)

    await m._emit_watch_m3(rm, g1, "A", "B")

    repo.exists_for_match.assert_awaited_once_with(1)
    repo.create.assert_not_awaited()
