"""Health monitor: observabilidade proativa do sistema.

MELHORIA 5 (2026-03-25):
- Verifica saúde do sistema a cada 5 minutos
- Envia alertas ao admin via Telegram quando detecta:
  * Regime degradado (z-score alto)
  * API sem resposta por mais de 3 ciclos
  * Nenhum jogo processado nas últimas 2 horas (em horário ativo)
  * Taxa de erro do banco acima de 10%
- Envia status periódico a cada 6 horas
- Usa os templates existentes de format_regime_warning e format_system_status
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from loguru import logger

_BRT = ZoneInfo("America/Sao_Paulo")

# Horários ativos do eSoccer (BRT) — fora desses, não alertar sobre inatividade
ACTIVE_HOURS = range(6, 24)  # 06:00 - 23:59 BRT


class HealthMonitor:
    """Monitors system health and sends proactive alerts to admin.

    Tracks:
    - API health (consecutive failures)
    - Game processing activity
    - Database error rate
    - Regime status changes
    - System uptime
    """

    def __init__(
        self,
        stats_engine,
        game_watcher,
        pair_matcher,
        odds_monitor,
        notifier,
        check_interval: int = 300,       # 5 min
        status_interval: int = 21600,     # 6 hours
    ):
        self.stats = stats_engine
        self.watcher = game_watcher
        self.pair_matcher = pair_matcher
        self.odds_monitor = odds_monitor
        self.notifier = notifier
        self.check_interval = check_interval
        self.status_interval = status_interval

        # State tracking
        self._running = False
        self._start_time = time.monotonic()
        self._last_status_sent = 0.0
        self._last_game_processed = time.monotonic()
        self._api_failures: int = 0
        self._db_errors: int = 0
        self._db_operations: int = 0
        self._last_regime: str = "HEALTHY"
        self._alerts_today: int = 0
        self._alerts_today_date: str = ""

    def record_api_success(self) -> None:
        """Record a successful API call (resets failure counter)."""
        self._api_failures = 0

    def record_api_failure(self) -> None:
        """Record a failed API call (NOT DB errors)."""
        self._api_failures += 1

    def record_db_error(self) -> None:
        """Record a DB error (tracked separately from API)."""
        self._db_errors += 1
        self._db_operations += 1

    def record_game_processed(self) -> None:
        """Record that a game was processed."""
        self._last_game_processed = time.monotonic()

    def record_db_operation(self, success: bool) -> None:
        """Record a database operation result."""
        self._db_operations += 1
        if not success:
            self._db_errors += 1

    def record_alert_sent(self) -> None:
        """Record that an alert was sent today."""
        today = datetime.now(_BRT).strftime("%Y-%m-%d")
        if today != self._alerts_today_date:
            self._alerts_today = 0
            self._alerts_today_date = today
        self._alerts_today += 1

    @property
    def uptime_str(self) -> str:
        """Human-readable uptime string."""
        elapsed = time.monotonic() - self._start_time
        hours = int(elapsed // 3600)
        minutes = int((elapsed % 3600) // 60)
        if hours > 24:
            days = hours // 24
            hours = hours % 24
            return f"{days}d {hours}h {minutes}m"
        return f"{hours}h {minutes}m"

    async def start(self) -> None:
        """Start the health monitoring loop."""
        self._running = True
        self._start_time = time.monotonic()
        logger.info(
            f"HealthMonitor started (check every {self.check_interval}s, "
            f"status every {self.status_interval}s)"
        )

        while self._running:
            try:
                await self._check_cycle()
            except Exception as e:
                logger.error(f"HealthMonitor check error: {e}")

            await asyncio.sleep(self.check_interval)

    def stop(self) -> None:
        """Stop the health monitoring loop."""
        self._running = False
        logger.info("HealthMonitor stopped")

    async def _check_cycle(self) -> None:
        """Single health check cycle."""
        now = time.monotonic()
        now_brt = datetime.now(_BRT)

        # 1. Check regime status
        try:
            regime = await self.stats.check_regime()
            regime_status = regime.get("status", "HEALTHY")

            # Alert on regime change (only when it gets worse)
            if regime_status != self._last_regime:
                if regime_status in ("WARNING", "DEGRADED"):
                    from src.telegram.messages import format_regime_warning
                    msg = format_regime_warning(regime)
                    await self.notifier.send_admin_message(msg)
                    logger.warning(f"Regime changed: {self._last_regime} → {regime_status}")
                elif self._last_regime in ("WARNING", "DEGRADED"):
                    # Recovered
                    await self.notifier.send_admin_message(
                        f"🟢 <b>Regime recuperado</b>\n\n"
                        f"Status: {self._last_regime} → HEALTHY\n"
                        f"O sistema voltou ao normal."
                    )
                    logger.info(f"Regime recovered: {self._last_regime} → HEALTHY")
                self._last_regime = regime_status
        except Exception as e:
            logger.warning(f"Regime check failed in health monitor: {e}")

        # 2. Check API health (só alertar a cada 1h, não a cada 5 min)
        if self._api_failures >= 5:
            last_api_alert = getattr(self, '_last_api_alert', 0.0)
            if now - last_api_alert > 3600:  # max 1 alerta por hora
                await self.notifier.send_admin_message(
                    f"🚨 <b>API sem resposta</b>\n\n"
                    f"A BetsAPI falhou {self._api_failures} vezes consecutivas.\n"
                    f"Último sucesso: {self._format_elapsed(now - self._last_game_processed)} atrás\n"
                    f"Verificar status da API e cota de requests."
                )
                logger.error(f"API health alert: {self._api_failures} consecutive failures")
                self._last_api_alert = now
            self._api_failures = 0

        # 3. Check game processing activity (only during active hours)
        if now_brt.hour in ACTIVE_HOURS:
            idle_minutes = (now - self._last_game_processed) / 60
            if idle_minutes > 20:  # 20 min sem processar jogos (jogos a cada ~10 min)
                last_idle_alert = getattr(self, '_last_idle_alert', 0.0)
                if now - last_idle_alert > 3600:  # max 1 alerta/hora
                    await self.notifier.send_admin_message(
                        f"⚠️ <b>Inatividade detectada</b>\n\n"
                        f"Nenhum jogo processado nas últimas {idle_minutes:.0f} minutos.\n"
                        f"Horário: {now_brt.strftime('%H:%M')} BRT\n"
                        f"Verificar se a liga está ativa."
                    )
                    logger.warning(f"Inactivity alert: {idle_minutes:.0f}min without games")
                    self._last_idle_alert = now
                # Reset to avoid repeated alerts
                self._last_game_processed = now

        # 4. Check DB error rate
        if self._db_operations >= 100:
            error_rate = self._db_errors / self._db_operations
            if error_rate > 0.10:  # >10% errors
                await self.notifier.send_admin_message(
                    f"🚨 <b>Taxa de erro do banco alta</b>\n\n"
                    f"Erros: {self._db_errors}/{self._db_operations} ({error_rate:.1%})\n"
                    f"Verificar conexão com o banco de dados."
                )
                logger.error(f"DB error rate alert: {error_rate:.1%}")
            # Reset counters
            self._db_errors = 0
            self._db_operations = 0

        # 5. Periodic status update
        if now - self._last_status_sent >= self.status_interval:
            await self._send_status()
            self._last_status_sent = now

    async def _send_status(self) -> None:
        """Send periodic system status to admin."""
        try:
            from src.telegram.messages import format_system_status

            cold_done = await self.stats.is_cold_start_complete()

            pending_pairs = len(self.pair_matcher._pending) if hasattr(self.pair_matcher, '_pending') else 0
            active_monitors = self.odds_monitor.active_count if hasattr(self.odds_monitor, 'active_count') else 0

            # Pool status
            pool_info = ""
            try:
                from src.db.database import engine
                pool = engine.pool
                pool_info = f"DB pool: {pool.checkedout()}/{pool.size()+pool.overflow()} em uso"
            except Exception as e:
                logger.debug(f"Could not read DB pool status: {e}")

            status_data = {
                "uptime": self.uptime_str,
                "games_monitoring": active_monitors,
                "pending_pairs": pending_pairs,
                "alerts_today": self._alerts_today,
                "regime_status": self._last_regime,
                "cold_start_complete": cold_done,
                "alerts_paused": False,
                "pool_info": pool_info,
            }

            msg = format_system_status(status_data)
            await self.notifier.send_admin_message(msg)
            logger.info(f"Periodic status sent: uptime={self.uptime_str}, regime={self._last_regime}")
        except Exception as e:
            logger.warning(f"Failed to send periodic status: {e}")

    @staticmethod
    def _format_elapsed(seconds: float) -> str:
        """Format elapsed seconds as human-readable string."""
        if seconds < 60:
            return f"{seconds:.0f}s"
        elif seconds < 3600:
            return f"{seconds / 60:.0f}min"
        else:
            return f"{seconds / 3600:.1f}h"
