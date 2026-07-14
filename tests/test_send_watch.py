"""Testes do send_watch — regressao do AttributeError _chat_id (2026-07-13).

Bug: send_watch referenciava self._chat_id, mas o atributo definido no
__init__ eh self.chat_id. Todo watch morria com AttributeError antes do envio.
"""

from unittest.mock import AsyncMock, MagicMock

from src.telegram.bot import TelegramNotifier


def _make_notifier() -> TelegramNotifier:
    notifier = TelegramNotifier(
        token="123:fake-token",
        chat_id="-1003721105906",
        admin_chat_id="6034412176",
    )
    # Mock do Bot pra nao tocar na API real
    notifier.bot = MagicMock()
    sent_msg = MagicMock()
    sent_msg.message_id = 42
    notifier.bot.send_message = AsyncMock(return_value=sent_msg)
    notifier.bot.delete_message = AsyncMock()
    return notifier


def _watch_data() -> dict:
    return {
        "kickoff_str": "19:43",
        "player_home": "Sena",
        "player_away": "Bosko",
        "line_label": "over 2.5",
        "target_player": "Sena",
        "target_odds": 1.80,
        "lines": [
            {
                "line": "over25",
                "line_label": "over 2.5",
                "target_odds": 1.80,
                "predicted_tp": 0.72,
                "h2h_tier": "A",
            }
        ],
    }


async def test_send_watch_envia_pro_grupo_vip():
    """send_watch deve enviar pro chat_id (grupo VIP) sem AttributeError."""
    notifier = _make_notifier()

    msg_id = await notifier.send_watch(_watch_data(), auto_delete_seconds=300)

    assert msg_id == 42
    notifier.bot.send_message.assert_awaited_once()
    kwargs = notifier.bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == "-1003721105906"


async def test_send_watch_noop_sem_chat_id():
    """Sem chat_id configurado, send_watch deve ser NO-OP (None), sem crash."""
    notifier = _make_notifier()
    notifier.chat_id = ""

    msg_id = await notifier.send_watch(_watch_data(), auto_delete_seconds=300)

    assert msg_id is None
    notifier.bot.send_message.assert_not_awaited()


async def test_send_watch_to_admin_vai_pro_dm():
    """to_admin=True (watch M2) deve enviar pro DM admin, nao pro grupo VIP."""
    notifier = _make_notifier()

    msg_id = await notifier.send_watch(
        _watch_data(), auto_delete_seconds=300, to_admin=True
    )

    assert msg_id == 42
    kwargs = notifier.bot.send_message.await_args.kwargs
    assert kwargs["chat_id"] == "6034412176"  # DM do owner, nao o VIP


async def test_send_watch_to_admin_noop_sem_admin_chat():
    """to_admin=True sem admin_chat_id configurado deve ser NO-OP."""
    notifier = _make_notifier()
    notifier._admin_chat_id = ""

    msg_id = await notifier.send_watch(
        _watch_data(), auto_delete_seconds=300, to_admin=True
    )

    assert msg_id is None
    notifier.bot.send_message.assert_not_awaited()
