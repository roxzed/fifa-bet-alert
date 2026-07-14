"""Testes do ValidatorV3 — GREEN/RED por linha e edição da mensagem."""

from unittest.mock import AsyncMock, MagicMock

from src.core.validator_v3 import ValidatorV3


def _alert(line="over25", odds=1.85, msg_id=55):
    a = MagicMock()
    a.id = 1
    a.line = line
    a.odds = odds
    a.losing_player = "Sena"
    a.opponent_player = "Bosko"
    a.telegram_message_id = msg_id
    a.hit = None
    a.game1_score = "1-3"
    a.rate = 0.70
    a.hits = 14
    a.n_h2h = 20
    a.recent_hits = 5
    return a


def _validator(alerts):
    match_repo = MagicMock()
    alert_repo = MagicMock()
    alert_repo.get_all_by_match_id = AsyncMock(return_value=alerts)
    alert_repo.validate = AsyncMock()
    notifier = MagicMock()
    notifier.edit_alert_v3_result = AsyncMock(return_value=True)
    return ValidatorV3(match_repo, alert_repo, notifier), alert_repo, notifier


def _ended_match(loser_home_goals=3, opp_goals=1):
    m = MagicMock()
    m.id = 62750
    m.player_home = "Sena"
    m.player_away = "Bosko"
    m.score_home = loser_home_goals
    m.score_away = opp_goals
    return m


async def test_valida_hit_e_edita_mensagem():
    alert = _alert()  # over25, Sena
    validator, repo, notifier = _validator([alert])

    await validator.validate_match(_ended_match(loser_home_goals=3))

    repo.validate.assert_awaited_once()
    kwargs = repo.validate.await_args.kwargs
    assert kwargs["actual_goals"] == 3
    assert kwargs["hit"] is True
    assert abs(kwargs["profit_flat"] - 0.85) < 1e-9
    notifier.edit_alert_v3_result.assert_awaited_once()


async def test_valida_red_com_profit_negativo():
    alert = _alert(line="over35", odds=2.10)
    validator, repo, notifier = _validator([alert])

    await validator.validate_match(_ended_match(loser_home_goals=2))

    kwargs = repo.validate.await_args.kwargs
    assert kwargs["hit"] is False
    assert kwargs["profit_flat"] == -1.0


async def test_ignora_alertas_ja_validados():
    alert = _alert()
    alert.hit = True  # ja validado
    validator, repo, notifier = _validator([alert])

    await validator.validate_match(_ended_match())

    repo.validate.assert_not_awaited()
    notifier.edit_alert_v3_result.assert_not_awaited()
