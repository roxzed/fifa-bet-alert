"""Verify alert results against actual API data."""

import asyncio
from sqlalchemy import select, text
from src.db.database import async_session_factory
from src.db.models import Alert, Match
from src.api.betsapi_client import BetsAPIClient
from src.config import settings


async def main():
    api = BetsAPIClient(token=settings.betsapi_token)
    session = async_session_factory()

    # Buscar todos alertas validados recentemente (ultimos 2 dias)
    stmt = (
        select(Alert, Match)
        .join(Match, Alert.match_id == Match.id)
        .where(Alert.validated_at.is_not(None))
        .order_by(Alert.sent_at.desc())
    )
    result = await session.execute(stmt)
    rows = result.all()

    print(f"{'ID':>4} | {'Jogador':<15} | {'Linha':<8} | {'Placar G2':>10} | {'Gols Loser':>10} | {'Resultado':>9} | {'Home G2':<15} | {'Away G2':<15} | {'Loser=Home?'}")
    print("-" * 130)

    for alert, match in rows:
        # Buscar resultado real da API
        try:
            details = await api.get_event_details(match.api_event_id)
            api_home = details.home_score
            api_away = details.away_score
            api_status = details.status
        except Exception as e:
            api_home = "ERR"
            api_away = "ERR"
            api_status = str(e)[:30]

        # Verificar logica do validator
        loser = alert.losing_player
        loser_is_home = match.player_home == loser

        if loser_is_home:
            loser_goals_calc = api_home if isinstance(api_home, int) else "?"
        else:
            loser_goals_calc = api_away if isinstance(api_away, int) else "?"

        # Resultado registrado
        hit_str = "GREEN" if alert.over25_hit or (alert.actual_goals and alert.actual_goals > 1) else "RED"
        if alert.best_line == "over15":
            hit_str = "GREEN" if alert.actual_goals and alert.actual_goals > 1 else "RED"
        elif alert.best_line == "over25":
            hit_str = "GREEN" if alert.actual_goals and alert.actual_goals > 2 else "RED"
        elif alert.best_line == "over35":
            hit_str = "GREEN" if alert.actual_goals and alert.actual_goals > 3 else "RED"

        db_score = f"{match.score_home}-{match.score_away}" if match.score_home is not None else "N/A"
        api_score = f"{api_home}-{api_away}"

        match_ok = "OK" if db_score == api_score else f"DIFF! DB={db_score}"

        print(
            f"{alert.id:>4} | {loser:<15} | {alert.best_line or 'over25':<8} | "
            f"{api_score:>10} | "
            f"DB={alert.actual_goals} API={loser_goals_calc} | "
            f"{hit_str:>9} | "
            f"{match.player_home or '':<15} | {match.player_away or '':<15} | "
            f"{'SIM' if loser_is_home else 'NAO'} {match_ok}"
        )

        await asyncio.sleep(0.3)  # rate limit API

    await session.close()
    await api.close()


if __name__ == "__main__":
    asyncio.run(main())
