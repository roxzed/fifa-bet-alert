"""Fix the 2 alerts sent with wrong scores."""

import asyncio
from telegram import Bot
from telegram.constants import ParseMode
from src.config import settings


CORRECTIONS = [
    # Uncle O2.5 — score real 2-2, Uncle (home) fez 2 gols, 2 <= 2.5 = RED
    ("Uncle", "Over 2.5", 1.62, False, "2-2 (Uncle fez 2 gols)", "Correção: placar real 2-2 (não 0-0)"),
    # Wboy O3.5 — score real 2-5, Wboy (home) fez 2 gols, 2 <= 3.5 = RED
    ("Wboy", "Over 3.5", 1.80, False, "2-5 (Wboy fez 2 gols)", "Correção: placar real 2-5 (não 0-0)"),
]


async def main():
    bot = Bot(token=settings.telegram_bot_token)
    chat_id = settings.telegram_chat_id

    for player, line, odds, hit, score_line, note in CORRECTIONS:
        emoji = "\u274c"
        profit = "-1.00u"
        text = (
            f"\u26a0\ufe0f <b>CORREÇÃO DE PLACAR</b>\n\n"
            f"{emoji} <b>RESULTADO RED</b>\n"
            f"\U0001f464 {player}\n"
            f"\U0001f3af {line} @{odds:.2f}\n"
            f"\u26bd {score_line}\n"
            f"\U0001f4b0 P&L: <b>{profit}</b>\n\n"
            f"<i>{note}</i>"
        )
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
            print(f"OK: {player} {line} — {note}")
        except Exception as e:
            print(f"ERRO: {e}")
        await asyncio.sleep(0.5)

    print("\nCorreções enviadas.")


if __name__ == "__main__":
    asyncio.run(main())
