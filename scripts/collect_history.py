"""Coleta dados históricos da liga Esoccer Battle 8 min.

Estratégia:
- Liga: 22614 (Esoccer Battle - 8 mins play 2026, v2 API)
- Período: configurável via START_DATE / END_DATE
- Foco: histórico de gols do perdedor no jogo seguinte

Estatísticas capturadas por jogador:
- Quantas vezes perdeu
- Quantos gols fez no jogo imediato após a derrota
- Taxa de over 2.5 / over 3.5 pós-derrota
- Breakdown por tipo de derrota (apertada / blowout)
- Breakdown por horário do dia

Correções aplicadas (auditoria 2025-03-25):
- PROBLEMA 14: Docstrings e logs atualizados para refletir liga 22614 / temporada 2026
- Janela de pareamento alinhada com PairMatcher de produção (30-80 min após ended_at)
- Adicionado fallback para started_at quando ended_at não disponível (45-80 min)

Usage:
    python scripts/collect_history.py
    DAYS_TO_COLLECT=60 python scripts/collect_history.py
"""

import asyncio
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv
load_dotenv()

from loguru import logger
from src.api.betsapi_client import BetsAPIClient
from src.config import settings
from src.db.database import init_db, get_session
from src.db.repositories import (
    MatchRepository, PlayerRepository, TeamStatsRepository,
)
from src.utils.logger import setup_logger

# Liga Esoccer Battle - 8 mins play (v2 API, 2026)
LEAGUE_ID = "22614"
LEAGUE_ID_2026 = "22614"
USE_V2 = True

# Temporada 2026 (01/01/2026 até hoje)
START_DATE = date(2026, 1, 1)
END_DATE   = date.today()


async def collect_day(api: BetsAPIClient, day: date,
                      match_repo: MatchRepository,
                      player_repo: PlayerRepository,
                      team_repo: TeamStatsRepository) -> int:
    """Coleta todos os jogos de um dia. Retorna quantidade salva."""
    day_str = day.strftime("%Y%m%d")
    league = LEAGUE_ID_2026 if USE_V2 else LEAGUE_ID
    try:
        events = await api.get_ended_events(league, day=day_str, use_v2=USE_V2)
    except Exception as e:
        logger.warning(f"Falha ao buscar {day_str}: {e}")
        return 0

    saved = 0
    for event in events:
        # Pular se já existe no banco
        existing = await match_repo.get_by_api_event_id(event.id)
        if existing:
            continue

        # Converter hora para fuso local
        from zoneinfo import ZoneInfo
        brt = ZoneInfo("America/Sao_Paulo")
        started_brt = event.scheduled_time.astimezone(brt) if event.scheduled_time else None

        match = await match_repo.create(
            api_event_id=event.id,
            league=event.league_name,
            player_home=event.home_name,
            player_away=event.away_name,
            team_home=event.home_team,
            team_away=event.away_team,
            score_home=event.home_score,
            score_away=event.away_score,
            status="ended",
            started_at=event.scheduled_time,
            ended_at=event.scheduled_time + timedelta(minutes=8) if event.scheduled_time else None,
            day_of_week=started_brt.weekday() if started_brt else None,
            hour_of_day=started_brt.hour if started_brt else None,
        )

        await player_repo.get_or_create(event.home_name)
        await player_repo.get_or_create(event.away_name)

        if event.home_team:
            await team_repo.save_match_team(match.id, event.home_name, event.home_team, "home")
        if event.away_team:
            await team_repo.save_match_team(match.id, event.away_name, event.away_team, "away")

        saved += 1

    return saved


async def find_and_link_pairs(match_repo: MatchRepository, total_days: int) -> int:
    """Liga pares ida/volta: mesmo par de jogadores, janela alinhada com produção.

    Lógica alinhada com PairMatcher de produção (auditoria 2025-03-25):
    - Se ended_at disponível: busca G2 entre ended_at + 30 min e ended_at + 80 min
    - Se apenas started_at: busca G2 entre started_at + 45 min e started_at + 80 min
    - Isso garante que os dados históricos reflitam a mesma lógica do bot em tempo real
    """
    logger.info("Vinculando pares ida/volta...")
    unlinked = await match_repo.get_unlinked_matches(days_back=total_days)
    linked = 0

    for match in unlinked:
        if match.is_return_match or match.pair_match_id:
            continue

        # Determinar janela temporal alinhada com PairMatcher de produção
        if match.ended_at:
            # Preferir ended_at como âncora (mesmo que PairMatcher em produção)
            window_anchor = match.ended_at
            window_min_offset = 30
            window_max_offset = 80
        elif match.started_at:
            # Fallback: started_at + janela mais ampla
            window_anchor = match.started_at
            window_min_offset = 45
            window_max_offset = 80
        else:
            # Sem timestamps — pular
            continue

        return_match = await match_repo.find_return_match(
            player1=match.player_home,
            player2=match.player_away,
            after_time=window_anchor,
            window_min=window_min_offset,
            window_max=window_max_offset,
        )

        if return_match:
            time_between = None
            if match.started_at and return_match.started_at:
                time_between = int(
                    (return_match.started_at - match.started_at).total_seconds() / 60
                )
            await match_repo.link_pair(match.id, return_match.id, time_between)
            linked += 1

    return linked


async def main() -> None:
    setup_logger()
    logger.info("=" * 55)
    logger.info("COLETA HISTÓRICA - FIFA Bet Alert")
    logger.info("=" * 55)

    if not settings.betsapi_token:
        logger.error("BETSAPI_TOKEN não configurado no .env")
        return

    start_date = START_DATE
    end_date   = END_DATE

    total_days = (end_date - start_date).days + 1
    logger.info(f"Período: {start_date} até {end_date} ({total_days} dias)")
    active_league = LEAGUE_ID_2026 if USE_V2 else LEAGUE_ID
    logger.info(f"Liga: {active_league} ({'v2 - atual 2026' if USE_V2 else 'v1 - histórico 2025'})")

    await init_db()

    async with get_session() as session:
        match_repo = MatchRepository(session)
        player_repo = PlayerRepository(session)
        team_repo = TeamStatsRepository(session)

        async with BetsAPIClient(
            settings.betsapi_token,
            settings.betsapi_base_url,
            v2_url=settings.betsapi_v2_url,
        ) as api:
            total_saved = 0

            current = start_date
            day_count = 0

            while current <= end_date:
                saved = await collect_day(api, current, match_repo, player_repo, team_repo)
                total_saved += saved
                day_count += 1

                if day_count % 10 == 0 or day_count >= total_days - 2:
                    pct = day_count / total_days * 100
                    logger.info(
                        f"Progresso: {day_count}/{total_days} dias ({pct:.0f}%) | "
                        f"{total_saved:,} jogos coletados | "
                        f"Último: {current} ({saved} jogos)"
                    )

                current += timedelta(days=1)

            logger.info(f"\nColeta concluída: {total_saved:,} jogos salvos")

            # Vincular pares ida/volta
            logger.info("Vinculando pares ida/volta (pode demorar alguns minutos)...")
            linked = await find_and_link_pairs(match_repo, total_days)
            logger.info(f"Pares vinculados: {linked:,}")

            # Resumo final
            total_games = await match_repo.count_total()
            total_pairs = await match_repo.count_pairs()
            unique_players = await player_repo.count_unique()

            print("\n" + "=" * 55)
            print("COLETA CONCLUÍDA:")
            print(f"  Período: {start_date} a {end_date}")
            print(f"  Total de jogos: {total_games:,}")
            print(f"  Pares ida/volta: {total_pairs:,}")
            print(f"  Jogadores únicos: {unique_players}")
            print(f"\nPróximo passo: python scripts/backtest.py")
            print("=" * 55)


if __name__ == "__main__":
    asyncio.run(main())
