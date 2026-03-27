"""Automatic daily and weekly reports sent to Telegram.

Correções aplicadas (auditoria 2025-03-25):
- BUG 4: Removidas chamadas a métodos inexistentes (get_period_stats_by_dates,
  get_top_player_for_date). Agora usa apenas métodos que existem no AlertRepository.
- ROI calculado a partir de alertas validados individuais via get_all_validated_for_export.
- Relatório semanal usa get_period_stats(days=7) em vez de método inexistente.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from loguru import logger


class Reporter:
    """Generates and sends scheduled performance reports."""

    def __init__(self, alert_repo, player_repo, method_stats_repo, notifier) -> None:
        self.alerts = alert_repo
        self.players = player_repo
        self.method_stats = method_stats_repo
        self.notifier = notifier

    async def send_daily_report(self) -> None:
        """Build and send the daily summary report."""
        today = date.today()
        stats = await self.alerts.get_daily_stats(today)

        total = stats.get("total", 0)
        if total == 0:
            await self.notifier.send_message(
                f"📋 <b>RELATÓRIO DIÁRIO - {today}</b>\n\nSem alertas hoje."
            )
            return

        over25_hits = stats.get("over25_hits", 0)
        over35_hits = stats.get("over35_hits", 0)
        validated = stats.get("validated", 0)

        # Calcular ROI a partir dos alertas validados do dia
        roi = await self._compute_daily_roi(today)

        # Monthly accumulated — usar get_period_stats com dias desde início do mês
        days_in_month = (today - today.replace(day=1)).days + 1
        monthly = await self.alerts.get_period_stats(days=days_in_month)
        monthly_validated = monthly.get("validated", 0)
        monthly_rate = (monthly.get("over25_hits", 0) / monthly_validated
                        if monthly_validated > 0 else 0)

        # Top player do dia — buscar entre alertas validados
        top_player = await self._get_top_player_today(today)

        report_data = {
            "date": str(today),
            "total_alerts": total,
            "over25_hits": over25_hits,
            "over35_hits": over35_hits,
            "roi_flat": roi * 100,
            "best_result": None,
            "worst_result": None,
            "monthly_hit_rate": monthly_rate,
            "top_player": top_player,
        }

        await self.notifier.send_daily_report(report_data)
        logger.info(f"Daily report sent: {over25_hits}/{total} over2.5 hits")

    async def send_weekly_report(self) -> None:
        """Build and send the weekly summary report."""
        today = date.today()
        week_ago = today - timedelta(days=7)

        # Usar get_period_stats com 7 dias (método que existe)
        stats = await self.alerts.get_period_stats(days=7)

        total = stats.get("total", 0)
        validated = stats.get("validated", 0)
        hits = stats.get("over25_hits", 0)
        rate = hits / validated if validated > 0 else 0

        # ROI da semana
        roi = await self._compute_period_roi(days=7)

        # Compare to previous week
        prev_stats = await self.alerts.get_period_stats(days=14)
        # Subtrair stats da semana atual das de 14 dias para obter semana anterior
        prev_total = prev_stats.get("validated", 0) - validated
        prev_hits = prev_stats.get("over25_hits", 0) - hits
        prev_rate = prev_hits / prev_total if prev_total > 0 else 0
        trend = "📈" if rate > prev_rate else "📉"

        roi_str = f"+{roi*100:.1f}%" if roi >= 0 else f"{roi*100:.1f}%"

        text = (
            f"📊 <b>RELATÓRIO SEMANAL</b>\n"
            f"{week_ago} → {today}\n\n"
            f"   • Alertas: {total}\n"
            f"   • Validados: {validated}\n"
            f"   • Over 2.5: {hits}/{validated} ({rate:.1%})\n"
            f"   • ROI flat: {roi_str}\n"
            f"   • Vs semana anterior: {trend} ({prev_rate:.1%})"
        )
        await self.notifier.send_message(text)
        logger.info(f"Weekly report sent: {hits}/{total} over2.5 hits")

    async def _compute_daily_roi(self, target: date) -> float:
        """Compute flat-bet ROI for a specific day using validated alerts."""
        try:
            all_validated = await self.alerts.get_all_validated_for_export()
            start = datetime.combine(target, datetime.min.time())
            end = datetime.combine(target, datetime.max.time())

            day_alerts = [
                a for a in all_validated
                if a.sent_at and start <= a.sent_at <= end
            ]
            return self._compute_roi_from_alerts(day_alerts)
        except Exception as e:
            logger.debug(f"Could not compute daily ROI: {e}")
            return 0.0

    async def _compute_period_roi(self, days: int) -> float:
        """Compute flat-bet ROI for the last N days using validated alerts."""
        try:
            all_validated = await self.alerts.get_all_validated_for_export()
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)

            period_alerts = [
                a for a in all_validated
                if a.sent_at and a.sent_at >= cutoff
            ]
            return self._compute_roi_from_alerts(period_alerts)
        except Exception as e:
            logger.debug(f"Could not compute period ROI: {e}")
            return 0.0

    def _compute_roi_from_alerts(self, alerts: list) -> float:
        """Simple flat bet ROI calculation from alert objects."""
        if not alerts:
            return 0.0

        total_stake = len(alerts)
        profit = 0.0
        for a in alerts:
            # Determinar odds e hit baseado na best_line
            best_line = getattr(a, "best_line", "over25") or "over25"
            if best_line == "over45":
                odds = a.over45_odds
                hit = getattr(a, "over45_hit", False)
            elif best_line == "over35":
                odds = a.over35_odds
                hit = getattr(a, "over35_hit", False)
            else:
                odds = a.over25_odds
                hit = getattr(a, "over25_hit", False)

            if hit and odds:
                profit += (odds - 1.0)
            else:
                profit -= 1.0

        return profit / total_stake if total_stake > 0 else 0.0

    async def _get_top_player_today(self, today: date) -> dict | None:
        """Get the player with best hit rate today (min 2 alerts).

        Usa get_all_validated_for_export e filtra manualmente (BUG 4 fix).
        """
        try:
            all_validated = await self.alerts.get_all_validated_for_export()
            start = datetime.combine(today, datetime.min.time())
            end = datetime.combine(today, datetime.max.time())

            day_alerts = [
                a for a in all_validated
                if a.sent_at and start <= a.sent_at <= end
            ]

            # Agrupar por jogador
            player_stats: dict[str, dict] = {}
            for a in day_alerts:
                name = a.losing_player
                if name not in player_stats:
                    player_stats[name] = {"total": 0, "hits": 0}
                player_stats[name]["total"] += 1
                if a.over25_hit:
                    player_stats[name]["hits"] += 1

            # Filtrar min 2 alertas e ordenar por hit rate
            candidates = [
                {"name": name, **stats}
                for name, stats in player_stats.items()
                if stats["total"] >= 2
            ]
            if not candidates:
                return None

            candidates.sort(key=lambda x: x["hits"] / x["total"], reverse=True)
            return candidates[0]
        except Exception as e:
            logger.debug(f"Could not get top player: {e}")
            return None
