"""Quick check of recent games from Battle 8 min league."""
import asyncio
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv
load_dotenv()

from loguru import logger
logger.disable("src")

from src.api.betsapi_client import BetsAPIClient
from src.config import settings
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo


async def main():
    BZ = ZoneInfo("America/Sao_Paulo")
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=60)

    today_str = now.strftime("%Y%m%d")
    async with BetsAPIClient(settings.betsapi_token, settings.betsapi_base_url) as api:
        events = await api.get_ended_events("22614", day=today_str)
        print(f"Total jogos hoje (liga 22614): {len(events)}")

        recent = sorted(
            [e for e in events if e.scheduled_time and e.scheduled_time >= cutoff],
            key=lambda x: x.scheduled_time
        )
        print(f"Ultimos 60 min: {len(recent)} jogos\n")

        for e in recent:
            t = e.scheduled_time.astimezone(BZ).strftime("%H:%M")
            print(f"  {t}  {e.home_name} ({e.home_team})  {e.home_score}-{e.away_score}  {e.away_name} ({e.away_team})")


if __name__ == "__main__":
    asyncio.run(main())
