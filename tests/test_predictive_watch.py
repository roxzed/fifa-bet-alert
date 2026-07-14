"""Testes do watch preditivo (fallback quando a volta nao casou via API)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from src.core.odds_monitor import OddsMonitor


def _emit_stub(monitor, metodo, gid=111, sent=True):
    """Mock de _emit_watch_mN que replica o contrato real: marca
    (gid, metodo) em _predictive_sent SOMENTE quando envia de fato (True).
    """
    async def _emit(*args, **kwargs):
        if sent:
            monitor._predictive_sent.add((gid, metodo))
        return sent
    return AsyncMock(side_effect=_emit)


def _monitor():
    m = OddsMonitor(MagicMock(), MagicMock(), MagicMock(),
                    alert_engine_v2=MagicMock(), alert_engine_v3=MagicMock())
    m._predictive_offset_min = 58.0
    m._emit_watch_m1 = _emit_stub(m, "m1")
    m._emit_watch_m2 = _emit_stub(m, "m2")
    m._emit_watch_m3 = _emit_stub(m, "m3")
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


async def test_nao_marca_trava_quando_emit_nao_envia(monkeypatch):
    """Se _emit_watch_m1 retorna False (sem candidato/suprimido), a trava
    (gid, "m1") NAO deve ser marcada — assim o watch real pode tentar depois
    quando a volta casar via API.
    """
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    m = _monitor()
    m._emit_watch_m1 = _emit_stub(m, "m1", sent=False)  # nao enviou nada
    await m._predictive_watch_loop(_g1(), "Sena", "Bosko", 2)
    m._emit_watch_m1.assert_awaited_once()
    assert (111, "m1") not in m._predictive_sent
    # os outros metodos (que enviaram) marcam normalmente
    assert (111, "m2") in m._predictive_sent


def test_schedule_noop_quando_desabilitado(monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "watch_predictive_enabled", False)
    m = _monitor()
    m.schedule_predictive_watch(_g1(), "Sena", "Bosko", 2)
    assert 111 not in m._predictive_tasks
