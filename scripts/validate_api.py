"""Validates BetsAPI connection and confirms all needed data is available."""

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

from src.api.betsapi_client import BetsAPIClient
from src.config import settings


async def main() -> None:
    print("=" * 55)
    print("    VALIDAÇÃO API - FIFA Bet Alert")
    print("=" * 55)

    if not settings.betsapi_token:
        print("[ERRO] BETSAPI_TOKEN não configurado no .env")
        return

    async with BetsAPIClient(settings.betsapi_token, settings.betsapi_base_url) as api:

        # 1. Auth test
        print("\n[1/6] Testando autenticação...")
        try:
            leagues = await api.get_leagues(sport_id=1)
            print(f"[OK] Autenticação: token válido. {len(leagues)} ligas encontradas.")
        except Exception as e:
            print(f"[ERRO] Autenticação falhou: {e}")
            return

        # 2. Find Battle 8 min league
        print("\n[2/6] Buscando liga 'Esoccer Battle - 8 mins play'...")
        league = await api.find_league("Battle")
        if not league:
            league = await api.find_league("Esoccer")

        if league:
            print(f"[OK] Liga encontrada: '{league.name}' (ID: {league.id})")
            print(f"     -> Adicione no .env: DEFAULT_LEAGUE_ID={league.id}")
        else:
            print("[!!] Liga não encontrada. Listando ligas eSoccer disponíveis:")
            esoccer = [lg for lg in leagues if "soccer" in lg.name.lower() or "esoccer" in lg.name.lower()]
            for lg in esoccer[:10]:
                print(f"     • {lg.name} (ID: {lg.id})")
            return

        # 3. Recent games (MUST pass day= to avoid fetching all 100k+ events)
        from datetime import date as _date
        today_str = _date.today().strftime("%Y%m%d")
        ended = []
        print("\n[3/6] Buscando jogos recentes (hoje)...")
        try:
            ended = await api.get_ended_events(league.id, day=today_str)
            print(f"[OK] Jogos finalizados hoje: {len(ended)}")
            if ended:
                ev = ended[0]
                print(f"     Exemplo: {ev.home_name} {ev.home_score}-{ev.away_score} {ev.away_name}")
                if ev.home_team or ev.away_team:
                    print(f"     Times: {ev.home_team} vs {ev.away_team}")
                else:
                    print("     [ATENÇÃO] Times não disponíveis neste endpoint")
        except Exception as e:
            print(f"[ERRO] Jogos recentes: {e}")

        # 4. Upcoming games (for pair matching)
        print("\n[4/6] Buscando jogos agendados...")
        try:
            upcoming = await api.get_upcoming_events(league.id)
            print(f"[OK] Jogos agendados: {len(upcoming)}")
            if upcoming:
                ev = upcoming[0]
                print(f"     Próximo: {ev.home_name} vs {ev.away_name} às {ev.scheduled_time}")
        except Exception as e:
            print(f"[ERRO] Jogos agendados: {e}")

        # 5. Odds
        print("\n[5/6] Testando odds de um evento...")
        if ended:
            try:
                event_id = ended[0].id
                odds = await api.get_event_odds(event_id)
                if odds:
                    print(f"[OK] Odds disponíveis: {len(odds)} mercados")
                    markets = [o.market for o in odds]
                    print(f"     Mercados: {', '.join(markets[:5])}")

                    has_over25 = any(
                        "2.5" in out.name or "2.5" in (out.header or "")
                        for o in odds for out in o.outcomes
                    )
                    has_over35 = any(
                        "3.5" in out.name or "3.5" in (out.header or "")
                        for o in odds for out in o.outcomes
                    )
                    print(f"     Over 2.5: {'[OK]' if has_over25 else '[NÃO ENCONTRADO]'}")
                    print(f"     Over 3.5: {'[OK]' if has_over35 else '[NÃO ENCONTRADO]'}")
                else:
                    print("[!!] Nenhuma odd disponível para eventos encerrados")
            except Exception as e:
                print(f"[ERRO] Odds: {e}")

        # 6. Summary
        print("\n" + "=" * 55)
        print("RESULTADO:")
        print("  Sistema viável para uso com BetsAPI.")
        print("  Próximo passo: configurar .env e iniciar coleta histórica")
        print(f"  python scripts/collect_history.py")
        print("=" * 55)


if __name__ == "__main__":
    asyncio.run(main())
