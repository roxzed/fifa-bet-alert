"""Smoke test do relatorio horario — gera o texto e imprime, sem mandar Telegram."""
import asyncio
import sys

from src.db.database import async_session_factory, init_db
from src.db.repositories import BlockedLineRepository
from src.core.blocked_lines import recompute_all_states, build_hourly_report


async def main() -> None:
    await init_db()
    repo = BlockedLineRepository(async_session_factory)
    await recompute_all_states(repo)
    text = await build_hourly_report(repo)
    out = "/tmp/hourly_report.txt"
    try:
        with open(out, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        pass
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("=" * 60)
    print(text)
    print("=" * 60)
    print(f"\nTamanho: {len(text)} chars (limite Telegram = 4096)")


asyncio.run(main())
