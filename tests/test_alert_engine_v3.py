"""Testes do AlertEngineV3 — gate de odds, dedupe e persistência."""

from unittest.mock import AsyncMock, MagicMock

from src.core.alert_engine_v3 import AlertEngineV3
from src.core.stats_engine_v3 import EvaluationV3, LineEvalV3


def _line(line="over25", threshold=2.5, rate=0.70):
    return LineEvalV3(
        line=line,
        threshold=threshold,
        hits=14,
        n=20,
        rate=rate,
        recent_hits=5,
        recent_n=7,
        qualified=True,
    )


def _engine(evaluation, exists=False):
    stats = MagicMock()
    stats.evaluate = AsyncMock(return_value=evaluation)
    repo = MagicMock()
    repo.exists_for_line = AsyncMock(return_value=exists)
    created = MagicMock()
    created.id = 1
    repo.create = AsyncMock(return_value=created)
    repo.update_telegram_message_id = AsyncMock()
    notifier = MagicMock()
    notifier.send_alert_v3 = AsyncMock(return_value=55)
    return AlertEngineV3(stats, repo, notifier), repo, notifier


def _matches():
    g2 = MagicMock()
    g2.id = 62750
    g2.player_home = "Sena"
    g2.player_away = "Bosko"
    g1 = MagicMock()
    g1.player_home = "Bosko"
    g1.player_away = "Sena"
    g1.score_home = 3
    g1.score_away = 1
    return g2, g1


async def test_alerta_quando_linha_qualifica_e_odd_no_gate():
    ev = EvaluationV3(should_alert=True, lines=[_line()], n_h2h=20)
    engine, repo, notifier = _engine(ev)
    g2, g1 = _matches()

    sent = await engine.evaluate_and_alert(
        g2,
        g1,
        loser="Sena",
        winner="Bosko",
        over25_odds=1.85,
        over35_odds=None,
    )

    assert sent is True
    repo.create.assert_awaited_once()
    assert repo.create.await_args.kwargs["line"] == "over25"
    assert repo.create.await_args.kwargs["odds"] == 1.85
    # Placar do G1 na perspectiva do perdedor: Bosko 3-1 Sena → "1-3"
    assert repo.create.await_args.kwargs["game1_score"] == "1-3"
    notifier.send_alert_v3.assert_awaited_once()


async def test_nao_alerta_odd_abaixo_do_piso():
    ev = EvaluationV3(should_alert=True, lines=[_line()], n_h2h=20)
    engine, repo, notifier = _engine(ev)
    g2, g1 = _matches()

    # odd 1.30 < piso M3 1.58 → linha cai; nenhuma sobra → sem alerta
    sent = await engine.evaluate_and_alert(
        g2,
        g1,
        loser="Sena",
        winner="Bosko",
        over25_odds=1.30,
        over35_odds=None,
    )

    assert sent is False
    notifier.send_alert_v3.assert_not_awaited()


async def test_alerta_odd_no_piso_158_e_sem_teto():
    # M3 aceita odd >= 1.58 e NAO tem teto: odd alta (ex. 6.50) deve passar.
    ev = EvaluationV3(should_alert=True, lines=[_line()], n_h2h=20)
    engine, repo, notifier = _engine(ev)
    g2, g1 = _matches()

    sent = await engine.evaluate_and_alert(
        g2,
        g1,
        loser="Sena",
        winner="Bosko",
        over25_odds=6.50,  # bem acima do antigo teto de 4.00
        over35_odds=None,
    )

    assert sent is True
    assert repo.create.await_args.kwargs["odds"] == 6.50
    notifier.send_alert_v3.assert_awaited_once()


async def test_odd_exatamente_no_piso_158_passa():
    ev = EvaluationV3(should_alert=True, lines=[_line()], n_h2h=20)
    engine, repo, notifier = _engine(ev)
    g2, g1 = _matches()

    sent = await engine.evaluate_and_alert(
        g2,
        g1,
        loser="Sena",
        winner="Bosko",
        over25_odds=1.58,  # piso inclusive
        over35_odds=None,
    )

    assert sent is True
    notifier.send_alert_v3.assert_awaited_once()


async def test_nao_duplica_alerta_para_mesma_linha():
    ev = EvaluationV3(should_alert=True, lines=[_line()], n_h2h=20)
    engine, repo, notifier = _engine(ev, exists=True)
    g2, g1 = _matches()

    sent = await engine.evaluate_and_alert(
        g2,
        g1,
        loser="Sena",
        winner="Bosko",
        over25_odds=1.85,
        over35_odds=None,
    )

    assert sent is False
    repo.create.assert_not_awaited()


async def test_multiplas_linhas_em_uma_mensagem():
    # Duas linhas qualificadas → 2 rows persistidas, mas UMA mensagem só
    ev = EvaluationV3(
        should_alert=True,
        lines=[
            _line(line="over15", threshold=1.5, rate=0.85),
            _line(line="over25", threshold=2.5, rate=0.70),
        ],
        n_h2h=20,
    )
    engine, repo, notifier = _engine(ev)
    g2, g1 = _matches()

    sent = await engine.evaluate_and_alert(
        g2,
        g1,
        loser="Sena",
        winner="Bosko",
        over25_odds=1.85,
        over35_odds=None,
        over15_odds=1.62,
    )

    assert sent is True
    assert repo.create.await_count == 2
    lines_created = [c.kwargs["line"] for c in repo.create.await_args_list]
    assert lines_created == ["over15", "over25"]
    notifier.send_alert_v3.assert_awaited_once()
    alert_data = notifier.send_alert_v3.await_args.args[0]
    assert len(alert_data["lines"]) == 2


async def test_sem_avaliacao_positiva_nao_alerta():
    ev = EvaluationV3(
        should_alert=False, reason="amostra insuficiente: 4 H2H < 10"
    )
    engine, repo, notifier = _engine(ev)
    g2, g1 = _matches()

    sent = await engine.evaluate_and_alert(
        g2,
        g1,
        loser="Sena",
        winner="Bosko",
        over25_odds=1.85,
        over35_odds=None,
    )

    assert sent is False
