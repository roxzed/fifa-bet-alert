"""Testes do builder de return_match sintetico pro watch preditivo."""

from datetime import datetime
from unittest.mock import MagicMock

from src.core.synthetic_match import build_synthetic_return


def _g1():
    g1 = MagicMock()
    g1.id = 999
    g1.player_home = "Sena"
    g1.player_away = "Bosko"
    g1.team_home = "Barcelona"
    g1.team_away = "Real Madrid"
    return g1


def test_inverte_jogadores_e_times_e_seta_horario():
    prev = datetime(2026, 7, 14, 18, 7)
    r = build_synthetic_return(_g1(), prev)
    assert r.id is None
    assert r.player_home == "Bosko" and r.player_away == "Sena"   # invertidos
    assert r.team_home == "Real Madrid" and r.team_away == "Barcelona"
    assert r.started_at == prev
    assert r.is_return_match is True
    assert r.game1_id == 999
    assert r.score_home is None and r.score_away is None
