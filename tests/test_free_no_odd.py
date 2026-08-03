"""FREE em modo sem odd: sem VOID, mensagens sem odd, green/red so pelo placar."""

import pytest

from src.core.free_status import decide_status
from src.telegram.messages import format_free_prealert, format_free_result

FORBIDDEN = ["volta", "g1", "g2", "perdedor", "edge", "ev "]


def test_decide_status_sem_odd_nunca_void_green():
    # entry_odd None mas require_odd=False -> green pelo placar (3 > 1.5)
    status, hit = decide_status(None, 3, "over15", 1.70, require_odd=False)
    assert status == "green" and hit is True


def test_decide_status_sem_odd_nunca_void_red():
    status, hit = decide_status(None, 1, "over15", 1.70, require_odd=False)
    assert status == "red" and hit is False


def test_decide_status_com_odd_mantem_void():
    # Comportamento legado intacto (modo com odd).
    status, hit = decide_status(None, 3, "over15", 1.70)
    assert status == "void" and hit is None


def test_prealert_sem_odd_nao_mostra_170(monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "bet365_live_odds_enabled", False)
    d = {"player": "Sena", "line_label": "Over 1.5", "kickoff_str": "19:43"}
    t = format_free_prealert(d).lower()
    assert "1.70" not in t
    assert "odd" not in t
    assert not any(f in t for f in FORBIDDEN)


def test_result_green_sem_odd(monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "bet365_live_odds_enabled", False)
    d = {"player": "Sena", "line_label": "Over 1.5", "actual_goals": 3}
    t = format_free_result(d, "green")
    assert "GREEN" in t
    assert "odd" not in t.lower()
    assert not any(f in t.lower() for f in FORBIDDEN)
