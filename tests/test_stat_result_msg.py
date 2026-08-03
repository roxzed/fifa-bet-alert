"""Mensagem de resultado sem odd (compartilhada M1/M2/M3)."""

from unittest.mock import AsyncMock, MagicMock

from src.telegram.bot import TelegramNotifier
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


def _m3_notifier():
    n = TelegramNotifier(token="1:x", chat_id="-100vip", m3_chat_id="-100m3")
    n.bot = MagicMock()
    n.bot.edit_message_text = AsyncMock()
    return n


async def test_edit_stat_result_edita_mensagem_m3():
    n = _m3_notifier()
    d = {"target_player": "Wboy", "line_label": "Over 2.5", "actual_goals": 3}
    ok = await n.edit_stat_result(55, "m3", d, True)
    assert ok is True
    kwargs = n.bot.edit_message_text.await_args.kwargs
    assert kwargs["chat_id"] == "-100m3"
    assert kwargs["message_id"] == 55
    assert "GREEN" in kwargs["text"]


async def test_edit_stat_result_sanitiza_surrogate_solto():
    # Regressao: producao ja quebrou (UnicodeEncodeError) quando o texto tinha
    # surrogates soltos. edit_stat_result precisa passar por _sanitize_text
    # igual edit_free_result/edit_alert_v3_result, senao o encode('utf-8')
    # do proprio Telegram client levanta excecao antes de mandar a mensagem.
    n = _m3_notifier()
    d = {
        "target_player": "Wboy\ud83d",  # surrogate solto simulando payload corrompido
        "line_label": "Over 2.5",
        "actual_goals": 3,
    }
    ok = await n.edit_stat_result(55, "m3", d, True)
    assert ok is True
    text = n.bot.edit_message_text.await_args.kwargs["text"]
    text.encode("utf-8")  # nao deve levantar UnicodeEncodeError
