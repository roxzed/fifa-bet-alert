"""Testes das mensagens publicas e envios Telegram do Modelo FREE.

CRITICO: a copy publica do FREE NUNCA pode revelar o metodo (G1/G2, volta,
perdedor, edge, EV). So jogador, linha, horario, odd minima e resultado.
"""

from unittest.mock import AsyncMock, MagicMock

from src.telegram.bot import TelegramNotifier
from src.telegram.messages import format_free_prealert, format_free_result

FORBIDDEN = ["volta", "g1", "g2", "perdedor", "edge", "ev "]


def _notifier():
    n = TelegramNotifier(token="1:x", chat_id="-100vip", free_group_id="-100free")
    n.bot = MagicMock()
    m = MagicMock()
    m.message_id = 77
    n.bot.send_message = AsyncMock(return_value=m)
    n.bot.edit_message_text = AsyncMock()
    return n


def _data():
    return {
        "player": "Sena",
        "line_label": "Over 1.5",
        "kickoff_str": "19:43",
        "actual_goals": 3,
        "entry_odd": 1.75,
    }


def test_prealert_tem_odd_minima_170_e_nao_revela_metodo():
    t = format_free_prealert(_data()).lower()
    assert "1.70" in t
    assert not any(f in t for f in FORBIDDEN)


def test_result_green_mostra_entrada():
    t = format_free_result(_data(), "green")
    assert "GREEN" in t and "1.75" in t


def test_result_red_nao_revela_metodo():
    t = format_free_result(_data(), "red").lower()
    assert not any(f in t for f in FORBIDDEN)


def test_result_void_nao_revela_metodo():
    t = format_free_result(_data(), "void").lower()
    assert not any(f in t for f in FORBIDDEN)


async def test_send_watch_free_vai_pro_free_group():
    n = _notifier()
    mid = await n.send_watch_free(_data())
    assert mid == 77
    assert n.bot.send_message.await_args.kwargs["chat_id"] == "-100free"


async def test_send_watch_free_noop_sem_free_group():
    n = _notifier()
    n._free_group_id = ""
    assert await n.send_watch_free(_data()) is None
    n.bot.send_message.assert_not_awaited()


async def test_edit_free_result_edita_mensagem():
    n = _notifier()
    ok = await n.edit_free_result(77, _data(), "green")
    assert ok is True
    kwargs = n.bot.edit_message_text.await_args.kwargs
    assert kwargs["chat_id"] == "-100free"
    assert kwargs["message_id"] == 77


async def test_edit_free_result_noop_sem_free_group():
    n = _notifier()
    n._free_group_id = ""
    assert await n.edit_free_result(77, _data(), "green") is False
    n.bot.edit_message_text.assert_not_awaited()
