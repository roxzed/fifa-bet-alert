"""Testes do watch preditivo (fallback quando a volta nao casou via API)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from src.core.odds_monitor import OddsMonitor


def _monitor():
    m = OddsMonitor(MagicMock(), MagicMock(), MagicMock(),
                    alert_engine_v2=MagicMock(), alert_engine_v3=MagicMock())
    m._predictive_offset_min = 58.0
    m._emit_watch_m1 = AsyncMock(return_value=True)
    m._emit_watch_m2 = AsyncMock(return_value=True)
    m._emit_watch_m3 = AsyncMock(return_value=True)
    return m


def _g1(started_min_ago=57):
    g1 = MagicMock()
    g1.id = 111
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    g1.started_at = now - timedelta(minutes=started_min_ago)
    g1.player_home = "Sena"
    g1.player_away = "Bosko"
    g1.team_home = "A"
    g1.team_away = "B"
    return g1


async def test_emite_os_tres_metodos_quando_volta_nao_casou(monkeypatch):
    monkeypatch.setattr("asyncio.sleep", AsyncMock())  # nao dormir
    m = _monitor()
    # volta NAO casou: game1_id nao esta em _task_meta
    await m._predictive_watch_loop(_g1(), "Sena", "Bosko", 2)
    m._emit_watch_m1.assert_awaited_once()
    m._emit_watch_m2.assert_awaited_once()
    m._emit_watch_m3.assert_awaited_once()
    assert (111, "m1") in m._predictive_sent


async def test_aborta_se_volta_ja_casou(monkeypatch):
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    m = _monitor()
    # volta casou: registrar o par por game1_id
    m._task_meta[555] = {"game1_id": 111}
    await m._predictive_watch_loop(_g1(), "Sena", "Bosko", 2)
    m._emit_watch_m1.assert_not_awaited()
    m._emit_watch_m3.assert_not_awaited()


def test_schedule_noop_quando_desabilitado(monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "watch_predictive_enabled", False)
    m = _monitor()
    m.schedule_predictive_watch(_g1(), "Sena", "Bosko", 2)
    assert 111 not in m._predictive_tasks
