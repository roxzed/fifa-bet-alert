"""Scrape bet365 live eSoccer odds for player-specific goals markets.

Usage:
    python scripts/scrape_bet365.py <event_url>
    python scripts/scrape_bet365.py  # uses default eSoccer live page

Requires: pip install playwright && python -m playwright install chromium
"""

import asyncio
import re
import sys

from playwright.async_api import async_playwright


async def scrape_event(url: str | None = None) -> dict:
    """Scrape a bet365 live event page for player goals odds.

    Returns dict with:
        - players: list of player/team info
        - markets: list of {player, team, line, over_odds, under_odds}
    """
    if url is None:
        # Default: eSoccer Battle live page
        url = "https://www.bet365.bet.br/#/IP/B151/"

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="pt-BR",
        )
        page = await context.new_page()

        # Bypass webdriver detection
        await page.add_init_script(
            'Object.defineProperty(navigator, "webdriver", {get: () => undefined});'
        )

        print(f"Acessando: {url}")
        await page.goto(url, timeout=30000)
        await asyncio.sleep(8)

        # Accept cookies if present
        try:
            accept_btn = page.locator("text=Aceitar todos")
            if await accept_btn.is_visible(timeout=3000):
                await accept_btn.click()
                await asyncio.sleep(2)
        except Exception:
            pass

        # Get full page text
        text = await page.inner_text("body")
        print(f"Page text: {len(text)} chars")

        # Try to find player goals markets
        # Pattern: "Team (Player) - Gols" followed by lines and odds
        markets = []

        # Get all visible text elements
        elements = await page.query_selector_all("[class*='participant'], [class*='odds'], [class*='market'], [class*='gl-Market'], [class*='event']")
        print(f"Found {len(elements)} potential elements")

        for el in elements[:50]:
            try:
                txt = await el.inner_text()
                if txt and ("Gols" in txt or "gols" in txt or "Over" in txt or "Mais de" in txt):
                    print(f"  MATCH: {txt[:200]}")
            except Exception:
                pass

        # Also dump raw text for analysis
        if "Gols" in text or "gols" in text:
            # Find the relevant section
            lines = text.split("\n")
            for i, line in enumerate(lines):
                if "Gols" in line or "gols" in line or "Mais de" in line or "Menos de" in line:
                    context_lines = lines[max(0, i - 2):i + 5]
                    print(f"  CONTEXT: {' | '.join(l.strip() for l in context_lines if l.strip())}")
        else:
            print("No 'Gols' market found in page text")
            # Print first 2000 chars for debugging
            print("--- PAGE TEXT (first 2000) ---")
            print(text[:2000])

        await browser.close()
        return {"text": text, "markets": markets}


async def main():
    url = sys.argv[1] if len(sys.argv) > 1 else None
    result = await scrape_event(url)
    print(f"\nDone. Text length: {len(result['text'])}")


if __name__ == "__main__":
    asyncio.run(main())
