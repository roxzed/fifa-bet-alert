"""Entry point: boots all components and runs the system 24/7.

Correções aplicadas (auditoria 2025-03-25):
- BUG 6: Cada componente agora recebe a session_factory (get_session) e cria
  sua própria sessão por ciclo de operação, eliminando race conditions.
- BUG 1: BotCommands agora é instanciado e registrado com polling do Telegram.
- MELHORIA 17: Graceful shutdown com signal handlers para SIGTERM/SIGINT.
- PROBLEMA 8: Commits manuais removidos do main.py; cada componente gerencia
  sua própria sessão via context manager.

Melhorias v2 (2026-03-25):
- MELHORIA 5: HealthMonitor integrado como quarto loop concorrente.
  Verifica saúde do sistema a cada 5 min e envia status a cada 6h.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from datetime import datetime

from loguru import logger

from src.config import settings
from src.utils.logger import setup_logger
from src.utils.scheduler import TaskScheduler


async def main() -> None:
    setup_logger()
    logger.info("=" * 60)
    logger.info("FIFA BET ALERT SYSTEM starting up")
    logger.info(f"Timezone: {settings.timezone}")
    logger.info("=" * 60)

    # --- DB ---
    from src.db.database import init_db, async_session_factory
    await init_db()
    logger.info("Database initialized")

    # --- Repositories (session-per-method: cada chamada cria sessão isolada) ---
    from src.db.repositories import (
        AlertRepository,
        LeagueRepository,
        MatchRepository,
        MethodStatsRepository,
        OddsRepository,
        PlayerRepository,
        TeamStatsRepository,
    )

    # Todas as repos recebem a factory — cada método cria sua própria sessão
    sf = async_session_factory

    # --- Find/create league ---
    league_repo = LeagueRepository(sf)
    league = await league_repo.get_by_name(settings.default_league_name)
    if not league:
        league = await league_repo.create(
            name=settings.default_league_name,
            api_league_id=settings.default_league_id,
        )
        logger.info(f"Liga configurada: {league.name} (ID: {league.api_league_id})")

    league_name = league.name
    league_api_id = league.api_league_id

    # --- API Client ---
    from src.api.betsapi_client import BetsAPIClient
    api = BetsAPIClient(
        token=settings.betsapi_token,
        base_url=settings.betsapi_base_url,
        v2_url=settings.betsapi_v2_url,
    )

    # --- Telegram ---
    from src.telegram.bot import TelegramNotifier
    from src.telegram.commands import BotCommands

    notifier = TelegramNotifier(
        token=settings.telegram_bot_token,
        chat_id=settings.telegram_chat_id,
    )

    # --- Stats Engine ---
    from src.core.stats_engine import StatsEngine
    stats_engine = StatsEngine(
        match_repo=MatchRepository(sf),
        player_repo=PlayerRepository(sf),
        alert_repo=AlertRepository(sf),
        method_stats_repo=MethodStatsRepository(sf),
        team_stats_repo=TeamStatsRepository(sf),
    )

    # --- Core modules ---
    from src.core.alert_engine import AlertEngine
    from src.core.game_watcher import GameWatcher
    from src.core.health_monitor import HealthMonitor
    from src.core.odds_monitor import OddsMonitor
    from src.core.pair_matcher import PairMatcher
    from src.core.reporter import Reporter
    from src.core.validator import Validator

    alert_engine = AlertEngine(stats_engine, AlertRepository(sf), notifier)
    odds_monitor = OddsMonitor(
        api, OddsRepository(sf), alert_engine,
        match_repo=MatchRepository(sf),
        poll_interval=settings.odds_poll_interval_seconds,
    )
    pair_matcher = PairMatcher(
        api, MatchRepository(sf), odds_monitor,
        session_factory=sf,
    )
    game_watcher = GameWatcher(
        api, MatchRepository(sf), PlayerRepository(sf),
        TeamStatsRepository(sf), pair_matcher,
    )

    validator = Validator(
        api, MatchRepository(sf), AlertRepository(sf),
        stats_engine, notifier, session_factory=sf,
    )

    # --- MELHORIA 5: Health Monitor ---
    health_monitor = HealthMonitor(
        stats_engine=stats_engine,
        game_watcher=game_watcher,
        pair_matcher=pair_matcher,
        odds_monitor=odds_monitor,
        notifier=notifier,
        check_interval=300,     # 5 min
        status_interval=21600,  # 6 hours
    )
    game_watcher._health = health_monitor

    # --- Reporter ---
    reporter = Reporter(AlertRepository(sf), PlayerRepository(sf), MethodStatsRepository(sf), notifier)

    # --- BotCommands ---
    bot_commands = BotCommands(
        notifier=notifier,
        stats_engine=stats_engine,  # compartilha o mesmo stats_engine (repos isoladas)
        match_repo=MatchRepository(sf),
        alert_repo=AlertRepository(sf),
        league_repo=LeagueRepository(sf),
        player_repo=PlayerRepository(sf),
    )

    # Registrar handlers e iniciar polling do Telegram
    from telegram.ext import Application
    tg_app = Application.builder().token(settings.telegram_bot_token).build()
    bot_commands.register_handlers(tg_app)

    # --- Announce startup ---
    cold_done = await stats_engine.is_cold_start_complete()
    progress = await stats_engine.get_cold_start_progress()
    if cold_done:
        await notifier.send_system_status({
            "uptime": "iniciando...",
            "games_monitoring": 0,
            "pending_pairs": 0,
            "alerts_today": 0,
            "regime_status": "HEALTHY",
            "cold_start_complete": True,
            "alerts_paused": False,
        })
    else:
        await notifier.send_cold_start_progress({
            "days_collected": progress.days_collected,
            "cold_start_days": progress.cold_start_days,
            "total_games": progress.total_games,
            "total_pairs": progress.total_pairs,
            "unique_players": progress.unique_players,
            "unique_teams": progress.unique_teams,
            "activation_date": progress.activation_date,
        })

    # --- Scheduler ---
    scheduler = TaskScheduler()

    # Retry pending pair matches every 5 min (otimizado: 1 fetch serve todos os pares)
    scheduler.add_interval_task(
        pair_matcher.retry_pending, seconds=300, task_id="retry_pending_pairs"
    )

    # Daily report at 23:55
    scheduler.add_daily_task(
        reporter.send_daily_report, hour=23, minute=55, task_id="daily_report"
    )

    # Weekly report on Sunday at 23:50
    scheduler.add_weekly_task(
        reporter.send_weekly_report,
        day_of_week="sun",
        hour=23,
        minute=50,
        task_id="weekly_report",
    )

    # Cold start progress update every 7 days at 09:00
    if not cold_done:
        scheduler.add_interval_task(
            _send_cold_start_update,
            seconds=7 * 24 * 3600,
            task_id="cold_start_progress",
            args=(stats_engine, notifier),
        )

    # --- Recalibracao automatica ---
    from src.core.recalibration import AutoRecalibrator
    recalibrator = AutoRecalibrator(
        stats_engine=stats_engine,
        alert_repo=AlertRepository(sf),
        match_repo=MatchRepository(sf),
        method_stats_repo=MethodStatsRepository(sf),
        notifier=notifier,
    )
    # Injetar recalibrador no alert_engine para controle de pausa
    alert_engine.set_recalibrator(recalibrator)

    # Recalibracao diaria as 06:00
    scheduler.add_daily_task(
        recalibrator.recalibrate, hour=6, minute=0, task_id="daily_recalibration"
    )
    # Deteccao de regime a cada hora
    scheduler.add_interval_task(
        recalibrator.detect_regime_change, seconds=3600, task_id="regime_detection"
    )

    # --- Backtest semanal agendado (domingo 08:00) ---
    from src.core.scheduled_backtest import ScheduledBacktest
    scheduled_backtest = ScheduledBacktest(
        match_repo=MatchRepository(sf),
        method_stats_repo=MethodStatsRepository(sf),
        notifier=notifier,
    )
    scheduler.add_weekly_task(
        scheduled_backtest.run_and_notify,
        day_of_week="sun",
        hour=8,
        minute=0,
        task_id="weekly_backtest",
    )

    scheduler.start()

    # --- Graceful shutdown (MELHORIA 17) ---
    shutdown_event = asyncio.Event()

    def _signal_handler(sig, frame):
        logger.info(f"Received signal {sig}, initiating graceful shutdown...")
        shutdown_event.set()

    if sys.platform != "win32":
        signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    # --- Run core loops + Telegram polling + Health Monitor ---
    logger.info(f"Starting main loops for league: {league_name}")

    async def _run_telegram_polling():
        """Run Telegram bot polling in background."""
        try:
            await tg_app.initialize()
            await tg_app.start()
            await tg_app.updater.start_polling(drop_pending_updates=True)
            logger.info("Telegram command polling started")
            await shutdown_event.wait()
        except Exception as e:
            logger.error(f"Telegram polling error: {e}")
        finally:
            try:
                await tg_app.updater.stop()
                await tg_app.stop()
                await tg_app.shutdown()
            except Exception:
                pass

    try:
        await asyncio.gather(
            game_watcher.start(
                league_id=league_api_id,
                poll_interval=settings.poll_interval_seconds,
            ),
            validator.start(poll_interval=60),
            _run_telegram_polling(),
            health_monitor.start(),  # MELHORIA 5: quarto loop
        )
    except asyncio.CancelledError:
        logger.info("Tasks cancelled during shutdown")
    finally:
        # Cleanup
        shutdown_event.set()
        health_monitor.stop()
        odds_monitor.stop_all()
        game_watcher.stop()
        validator.stop()
        scheduler.shutdown()
        await api.close()

        # Fechar pool de conexões
        from src.db.database import close_db
        await close_db()

        logger.info("FIFA Bet Alert System stopped")


async def _send_cold_start_update(stats_engine, notifier) -> None:
    """Send weekly cold start progress to Telegram."""
    progress = await stats_engine.get_cold_start_progress()
    await notifier.send_cold_start_progress({
        "days_collected": progress.days_collected,
        "cold_start_days": progress.cold_start_days,
        "total_games": progress.total_games,
        "total_pairs": progress.total_pairs,
        "unique_players": progress.unique_players,
        "unique_teams": progress.unique_teams,
        "activation_date": progress.activation_date,
    })


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Shutdown requested by user")
