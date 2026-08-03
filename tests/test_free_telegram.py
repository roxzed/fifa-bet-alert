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


def test_prealert_tem_odd_minima_170_e_nao_revela_metodo(monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "bet365_live_odds_enabled", True)
    t = format_free_prealert(_data()).lower()
    assert "1.70" in t
    assert not any(f in t for f in FORBIDDEN)


async def test_edit_free_result_void_agenda_auto_delete(monkeypatch):
    """Tip ANULADA (void) deve agendar a delecao da mensagem apos 5 min."""
    from src.config import settings
    monkeypatch.setattr(settings, "bet365_live_odds_enabled", True)
    n = _notifier()
    n.bot.delete_message = AsyncMock()
    sleeps = []

    async def _fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr("asyncio.sleep", _fake_sleep)
    ok = await n.edit_free_result(77, _data(), "void")
    assert ok is True
    # deixa a task de delecao rodar
    import asyncio as _a
    await _a.gather(*[t for t in _a.all_tasks() if t.get_name() == "free_void_delete_77"])
    assert 300 in sleeps
    n.bot.delete_message.assert_awaited_once_with(chat_id="-100free", message_id=77)


async def test_edit_free_result_green_nao_deleta(monkeypatch):
    """GREEN/RED NAO devem agendar delecao (ficam no grupo)."""
    n = _notifier()
    n.bot.delete_message = AsyncMock()
    await n.edit_free_result(77, _data(), "green")
    import asyncio as _a
    assert not any(t.get_name() == "free_void_delete_77" for t in _a.all_tasks())
    n.bot.delete_message.assert_not_awaited()


def test_result_green_mostra_entrada(monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "bet365_live_odds_enabled", True)
    t = format_free_result(_data(), "green")
    assert "GREEN" in t and "1.75" in t


def test_result_red_nao_revela_metodo(monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "bet365_live_odds_enabled", True)
    t = format_free_result(_data(), "red").lower()
    assert not any(f in t for f in FORBIDDEN)


def test_result_void_nao_revela_metodo(monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "bet365_live_odds_enabled", True)
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
