"""Testes da regra pura de status do Modelo FREE."""

from src.core.free_status import decide_status


def test_void_quando_odd_nunca_atingiu_minimo():
    assert decide_status(None, 3, "over15", 1.70) == ("void", None)


def test_green_com_entrada_valida_e_bateu():
    # over15 = >1.5; 3 gols bate
    assert decide_status(1.75, 3, "over15", 1.70) == ("green", True)


def test_red_com_entrada_valida_e_nao_bateu():
    # over25 = >2.5; 2 gols nao bate
    assert decide_status(1.90, 2, "over25", 1.70) == ("red", False)
