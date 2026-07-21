"""Testes do ValidatorFree — GREEN/RED/VOID + edicao da mensagem."""

from unittest.mock import AsyncMock, MagicMock

from src.core.validator_free import ValidatorFree


def _alert(line="over15", entry_odd=1.75, hit=None):
    a = MagicMock()
    a.id = 1
    a.line = line
    a.entry_odd = entry_odd
    a.losing_player = "Sena"
    a.telegram_message_id = 88
    a.hit = hit
    return a


def _v(alerts):
    mr = MagicMock()
    ar = MagicMock()
    ar.get_all_by_match_id = AsyncMock(return_value=alerts)
    ar.validate = AsyncMock()
    n = MagicMock()
    n.edit_free_result = AsyncMock(return_value=True)
    return ValidatorFree(mr, ar, n), ar, n


def _match(home_goals=3):
    m = MagicMock()
    m.id = 5
    m.player_home = "Sena"
    m.player_away = "Bosko"
    m.score_home = home_goals
    m.score_away = 1
    return m


async def test_green_com_entrada_valida():
    v, ar, n = _v([_alert("over15", 1.75)])  # over1.5, 3 gols
    await v.validate_match(_match(3))
    assert ar.validate.await_args.kwargs["status"] == "green"
    n.edit_free_result.assert_awaited_once()


async def test_void_quando_entry_odd_none():
    v, ar, n = _v([_alert("over15", None)])
    await v.validate_match(_match(3))
    assert ar.validate.await_args.kwargs["status"] == "void"


async def test_red_com_entrada_valida_nao_bateu():
    v, ar, n = _v([_alert("over25", 1.90)])  # over2.5, 2 gols
    await v.validate_match(_match(2))
    assert ar.validate.await_args.kwargs["status"] == "red"


async def test_ignora_ja_validado():
    v, ar, n = _v([_alert("over15", 1.75, hit=True)])
    await v.validate_match(_match(3))
    ar.validate.assert_not_awaited()
