"""Mensagem de resultado sem odd (compartilhada M1/M2/M3)."""

from src.telegram.messages import format_stat_result


def test_stat_result_green_sem_odd():
    d = {"target_player": "Wboy", "line_label": "Over 2.5", "actual_goals": 3}
    t = format_stat_result(d, True)
    assert "GREEN" in t and "Wboy" in t and "3" in t
    assert "odd" not in t.lower()
    assert "@" not in t  # nenhuma odd


def test_stat_result_red_sem_odd():
    d = {"target_player": "OG", "line_label": "Over 3.5", "actual_goals": 2}
    t = format_stat_result(d, False)
    assert "RED" in t and "OG" in t
    assert "odd" not in t.lower()


def test_stat_result_com_method_tag():
    d = {"target_player": "X", "line_label": "Over 1.5", "actual_goals": 2, "method_tag": "M3"}
    t = format_stat_result(d, True)
    assert "M3" in t
