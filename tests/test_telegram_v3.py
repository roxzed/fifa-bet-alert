"""Testes dos envios Telegram do Metodo 3."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from src.telegram.bot import TelegramNotifier
from src.telegram.messages import format_alert_v3, format_watch_v3


def _make_notifier(m3_chat_id="6034412176") -> TelegramNotifier:
    notifier = TelegramNotifier(
        token="123:fake-token",
        chat_id="-1003721105906",
        m3_chat_id=m3_chat_id,
    )
    notifier.bot = MagicMock()
    sent_msg = MagicMock()
    sent_msg.message_id = 99
    notifier.bot.send_message = AsyncMock(return_value=sent_msg)
    notifier.bot.edit_message_text = AsyncMock()
    notifier.bot.delete_message = AsyncMock()
    return notifier


def _lines():
    return [
        {"line": "over25", "line_label": "Over 2.5", "rate": 0.70,
         "hits": 14, "n": 20, "recent_hits": 5, "recent_n": 7, "odds": 1.85},
        {"line": "over15", "line_label": "Over 1.5", "rate": 0.85,
         "hits": 17, "n": 20, "recent_hits": 6, "recent_n": 7, "odds": 1.62},
    ]


def _data():
    return {
        "kickoff_str": "19:43",
        "player_home": "Sena",
        "player_away": "Bosko",
        "target_player": "Sena",
        "game1_score": "1-3",
        "bet365_url": "https://www.bet365.bet.br/#/IP/B151/",
        "lines": _lines(),
    }


def test_format_watch_v3_mostra_taxas_de_todas_as_linhas():
    text = format_watch_v3(_data())
    assert "M3" in text
    assert "Over 2.5" in text and "70%" in text and "14/20" in text and "5/7" in text
    assert "Over 1.5" in text
    # Pre-aviso NUNCA mostra odds (mercado fechado), mesmo com odds no payload
    assert "1.85" not in text and "1.62" not in text and "@" not in text


def test_format_alert_v3_inclui_odds():
    text = format_alert_v3(_data())
    assert "1.85" in text and "1.62" in text
    assert "Sena" in text


async def test_send_alert_v3_envia_pro_chat_m3():
    notifier = _make_notifier()
    msg_id = await notifier.send_alert_v3(_data())
    assert msg_id == 99
    assert notifier.bot.send_message.await_args.kwargs["chat_id"] == "6034412176"


async def test_send_alert_v3_noop_sem_chat():
    notifier = _make_notifier(m3_chat_id="")
    assert await notifier.send_alert_v3(_data()) is None
    notifier.bot.send_message.assert_not_awaited()


async def test_send_watch_v3_envia_e_agenda_delete():
    notifier = _make_notifier()
    msg_id = await notifier.send_watch_v3(_data(), auto_delete_seconds=0.01)
    assert msg_id == 99
    assert notifier.bot.send_message.await_args.kwargs["chat_id"] == "6034412176"
    # Aguardar a task de auto-delete agendada rodar e verificar o delete real
    delete_tasks = [
        t for t in asyncio.all_tasks() if t.get_name().startswith("m3_watch_delete_")
    ]
    assert delete_tasks, "task de auto-delete nao foi agendada"
    await asyncio.gather(*delete_tasks)
    notifier.bot.delete_message.assert_awaited_once_with(
        chat_id="6034412176", message_id=99
    )


async def test_edit_alert_v3_result_adiciona_green_red():
    notifier = _make_notifier()
    ok = await notifier.edit_alert_v3_result(
        message_id=99,
        alert_data=_data(),
        results=[
            {"line_label": "Over 2.5", "hit": True, "actual_goals": 3},
            {"line_label": "Over 1.5", "hit": True, "actual_goals": 3},
        ],
    )
    assert ok is True
    text = notifier.bot.edit_message_text.await_args.kwargs["text"]
    assert "GREEN" in text


async def test_send_message_v3_raw_envia_pro_chat_m3():
    notifier = _make_notifier()
    msg_id = await notifier.send_message_v3_raw("relatorio teste")
    assert msg_id == 99
    assert notifier.bot.send_message.await_args.kwargs["chat_id"] == "6034412176"
