"""Testes do StatsEngineV3 — critério de frequência H2H do Método 3."""

from unittest.mock import AsyncMock, MagicMock

from src.core.stats_engine_v3 import (
    EvaluationV3,
    StatsEngineV3,
    evaluate_h2h_lines,
)

KW = dict(min_h2h=10, min_prob=0.60, recent_window=7, recent_min_hits=5)


def test_linha_qualifica_com_taxa_e_recencia():
    # 20 jogos, gols altos: over25 bate em 14/20 (70%) e 5 dos últimos 7
    goals = [3, 4, 1, 3, 5, 3, 2, 3, 4, 3, 1, 3, 4, 2, 3, 5, 1, 3, 4, 2]
    ev = evaluate_h2h_lines(goals, **KW)
    over25 = next(line_eval for line_eval in ev.lines if line_eval.line == "over25")
    assert ev.should_alert is True
    assert over25.hits == 14 and over25.n == 20
    assert over25.recent_hits == 5 and over25.recent_n == 7
    assert over25.qualified is True


def test_taxa_alta_mas_recencia_ruim_reprova():
    # 70% no total, mas só 4 dos últimos 7 batem over25
    goals = [3, 1, 3, 1, 3, 1, 3] + [4] * 13  # recentes primeiro: 4/7
    ev = evaluate_h2h_lines(goals, **KW)
    lines = {line_eval.line for line_eval in ev.lines}
    assert "over25" not in lines


def test_recencia_boa_mas_taxa_baixa_reprova():
    # 7/7 recentes, mas 11/20 = 55% < 60%
    goals = [3] * 7 + [1] * 9 + [3] * 4
    ev = evaluate_h2h_lines(goals, **KW)
    lines = {line_eval.line for line_eval in ev.lines}
    assert "over25" not in lines


def test_amostra_entre_10_e_20_usa_o_que_tem():
    # 12 jogos, over15 bate em 9/12 (75%) e 6/7 recentes
    goals = [2, 2, 0, 2, 2, 2, 2, 0, 2, 2, 0, 2]
    ev = evaluate_h2h_lines(goals, **KW)
    over15 = next(line_eval for line_eval in ev.lines if line_eval.line == "over15")
    assert over15.n == 12 and over15.qualified


def test_amostra_menor_que_10_skip_total():
    ev = evaluate_h2h_lines([5, 5, 5, 5, 5, 5, 5, 5, 5], **KW)
    assert ev.should_alert is False
    assert ev.lines == []
    assert "amostra" in ev.reason.lower()


def test_multiplas_linhas_qualificam_juntas():
    goals = [4] * 20  # over15/25/35 batem 100%
    ev = evaluate_h2h_lines(goals, **KW)
    assert {line_eval.line for line_eval in ev.lines} == {"over15", "over25", "over35"}


async def test_engine_consulta_repo_e_avalia():
    repo = MagicMock()
    repo.get_h2h_player_goals = AsyncMock(return_value=[4] * 20)
    engine = StatsEngineV3(match_repo=repo)

    ev = await engine.evaluate("Sena", "Bosko")

    assert isinstance(ev, EvaluationV3)
    assert ev.should_alert is True
    repo.get_h2h_player_goals.assert_awaited_once_with("Sena", "Bosko", limit=20)
