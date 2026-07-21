"""Testes unitarios para o rastreamento de odd FREE (_track_free_odd)."""

from unittest.mock import MagicMock

from src.core.odds_monitor import OddsMonitor


def _mon():
    return OddsMonitor(MagicMock(), MagicMock(), MagicMock())


def test_grava_entry_odd_na_primeira_vez_acima_de_170():
    m = _mon()
    m._free_tracking[9] = {"line": "over15", "entry_odd": None, "max_odd": 0.0}
    ret1 = m._track_free_odd(9, over15=1.55, over25=None, over35=None, over45=None)
    assert ret1 is False                                  # 1.55 < 1.70, nao gravou
    assert m._free_tracking[9]["entry_odd"] is None       # 1.55 < 1.70
    assert m._free_tracking[9]["max_odd"] == 1.55
    ret2 = m._track_free_odd(9, over15=1.80, over25=None, over35=None, over45=None)
    assert ret2 is True                                   # 1a vez >= 1.70, ACABOU de gravar
    assert m._free_tracking[9]["entry_odd"] == 1.80       # 1a vez >= 1.70
    assert m._free_tracking[9]["max_odd"] == 1.80
    ret3 = m._track_free_odd(9, over15=1.72, over25=None, over35=None, over45=None)
    assert ret3 is False                                  # ja tinha entry_odd, nao regrava
    assert m._free_tracking[9]["entry_odd"] == 1.80       # nao sobrescreve
    assert m._free_tracking[9]["max_odd"] == 1.80         # max mantem o maior


def test_ignora_match_sem_tracking():
    m = _mon()
    ret = m._track_free_odd(1, over15=2.0, over25=None, over35=None, over45=None)  # nao lanca
    assert ret is False
    assert 1 not in m._free_tracking
