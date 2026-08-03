"""M2 (ValidatorV2) no modo sem odd: cobre o branch sem-odd de
_send_result_notification — edicao via notifier.edit_stat_result (canal
"admin") em vez do editor legado edit_alert_v2_result.

Gap apontado na review das Tasks 4/5: o branch sem-odd de
ValidatorV2._send_result_notification so tinha sido verificado por leitura.
Este arquivo espelha tests/test_validator_m1_no_odd.py para o M2.
"""

from unittest.mock import AsyncMock, MagicMock

from src.core.validator_v2 import ValidatorV2


def _make_alert(best_line="over25", telegram_message_id=999, suppressed=False):
    a = MagicMock()
    a.id = 1
    a.losing_player = "A"
    a.opponent_player = "B"
    a.camada = "C2"
    a.best_line = best_line
    a.telegram_message_id = telegram_message_id
    a.suppressed = suppressed
    a.over25_odds = 1.80
    a.over35_odds = 2.10
    a.over45_odds = 3.20
    a.over15_odds = 1.30
    a.prob = 0.7
    return a


def _make_return_match():
    rm = MagicMock()
    rm.player_home = "A"
    rm.player_away = "B"
    rm.team_home = "TeamX"
    rm.team_away = "TeamY"
    rm.started_at = None
    return rm


def _make_validator():
    notifier = MagicMock()
    notifier.edit_stat_result = AsyncMock(return_value=True)
    notifier.edit_alert_v2_result = AsyncMock(return_value=True)
    notifier.send_message_v2 = AsyncMock(return_value=True)
    validator = ValidatorV2(
        api_client=MagicMock(),
        match_repo=MagicMock(),
        alert_v2_repo=MagicMock(),
        notifier_v2=notifier,
    )
    return validator, notifier


async def test_sem_odd_green_chama_edit_stat_result_canal_admin(monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "bet365_live_odds_enabled", False)

    alert = _make_alert(best_line="over25")
    return_match = _make_return_match()
    validator, notifier = _make_validator()

    await validator._send_result_notification(
        alert, return_match, True, "3-1 (A fez 3 gols)", "Over 2.5",
        None, 0.0, loser_goals=3,
    )

    notifier.edit_stat_result.assert_awaited_once()
    args = notifier.edit_stat_result.await_args.args
    message_id, channel, data, hit = args
    assert message_id == 999
    assert channel == "admin"
    assert hit is True
    assert data["method_tag"] == "M2"
    assert data["line_label"] == "Over 2.5"
    assert data["target_player"] == "A"
    assert data["actual_goals"] == 3

    notifier.edit_alert_v2_result.assert_not_awaited()


async def test_sem_odd_red_chama_edit_stat_result_hit_false(monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "bet365_live_odds_enabled", False)

    alert = _make_alert(best_line="over25")
    return_match = _make_return_match()
    validator, notifier = _make_validator()

    await validator._send_result_notification(
        alert, return_match, False, "1-1 (A fez 1 gol)", "Over 2.5",
        None, 0.0, loser_goals=1,
    )

    notifier.edit_stat_result.assert_awaited_once()
    args = notifier.edit_stat_result.await_args.args
    message_id, channel, data, hit = args
    assert channel == "admin"
    assert hit is False
    assert data["method_tag"] == "M2"
    assert data["line_label"] == "Over 2.5"
    assert data["actual_goals"] == 1

    notifier.edit_alert_v2_result.assert_not_awaited()


async def test_reversibilidade_flag_true_usa_editor_legado(monkeypatch):
    # Com a flag ligada (modo com odd), o MESMO alerta deve seguir o caminho
    # legado (edit_alert_v2_result) e NAO o editor sem-odd (edit_stat_result).
    from src.config import settings
    monkeypatch.setattr(settings, "bet365_live_odds_enabled", True)

    alert = _make_alert(best_line="over25")
    return_match = _make_return_match()
    validator, notifier = _make_validator()

    await validator._send_result_notification(
        alert, return_match, True, "3-1 (A fez 3 gols)", "Over 2.5",
        1.80, 0.80, loser_goals=3,
    )

    notifier.edit_alert_v2_result.assert_awaited_once()
    kwargs = notifier.edit_alert_v2_result.await_args.kwargs
    assert kwargs["message_id"] == 999
    assert kwargs["alert"] is alert
    assert kwargs["return_match"] is return_match
    assert kwargs["hit"] is True
    assert kwargs["score_line"] == "3-1 (A fez 3 gols)"

    notifier.edit_stat_result.assert_not_awaited()


async def test_suppressed_nao_edita_nada(monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "bet365_live_odds_enabled", False)

    alert = _make_alert(best_line="over25", suppressed=True)
    return_match = _make_return_match()
    validator, notifier = _make_validator()

    await validator._send_result_notification(
        alert, return_match, True, "3-1 (A fez 3 gols)", "Over 2.5",
        None, 0.0, loser_goals=3,
    )

    notifier.edit_stat_result.assert_not_awaited()
    notifier.edit_alert_v2_result.assert_not_awaited()


async def test_sem_telegram_message_id_nao_edita_nada(monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "bet365_live_odds_enabled", False)

    alert = _make_alert(best_line="over25", telegram_message_id=None)
    return_match = _make_return_match()
    validator, notifier = _make_validator()

    await validator._send_result_notification(
        alert, return_match, True, "3-1 (A fez 3 gols)", "Over 2.5",
        None, 0.0, loser_goals=3,
    )

    notifier.edit_stat_result.assert_not_awaited()
    notifier.edit_alert_v2_result.assert_not_awaited()
