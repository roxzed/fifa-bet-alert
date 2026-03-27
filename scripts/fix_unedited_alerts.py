"""One-off script to send result messages for alerts that weren't updated."""

import asyncio
from telegram import Bot
from telegram.constants import ParseMode
from src.config import settings


# Alertas que não foram editados na msg original do Telegram.
# Formato: (player, line, odds, hit, score_line)
ALERTS_TO_FIX = [
    # 03-26 (ontem)
    ("Uncle", "Over 2.5", 1.62, False, "0-0 (Uncle fez 0 gols)"),
    ("Wboy", "Over 3.5", 1.80, False, "0-0 (Wboy fez 0 gols)"),
    # 03-27 (hoje)
    ("RossFCDK", "Over 1.5", 1.62, False, "3-1 (RossFCDK fez 1 gol)"),
    ("Wboy", "Over 2.5", 1.69, True, "2-3 (Wboy fez 3 gols)"),
    ("RossFCDK", "Over 1.5", 1.65, True, "5-3 (RossFCDK fez 5 gols)"),
    ("dm1trena", "Over 1.5", 1.69, True, "4-2 (dm1trena fez 2 gols)"),
]


async def main():
    bot = Bot(token=settings.telegram_bot_token)
    chat_id = settings.telegram_chat_id

    for player, line, odds, hit, score_line in ALERTS_TO_FIX:
        emoji = "\u2705" if hit else "\u274c"
        label = "GREEN" if hit else "RED"
        profit = f"{(odds - 1.0):+.2f}u" if hit else "-1.00u"

        text = (
            f"{emoji} <b>RESULTADO {label}</b>\n\n"
            f"\U0001f464 {player}\n"
            f"\U0001f3af {line} @{odds:.2f}\n"
            f"\u26bd {score_line}\n"
            f"\U0001f4b0 P&L: <b>{profit}</b>"
        )

        try:
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
            )
            print(f"OK: {player} {line} -> {label}")
        except Exception as e:
            print(f"ERRO: {player} {line}: {e}")

        await asyncio.sleep(0.5)  # rate limit

    print("\nTodos os resultados enviados.")


if __name__ == "__main__":
    asyncio.run(main())
