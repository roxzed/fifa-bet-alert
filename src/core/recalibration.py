"""Recalibracao automatica de fatores baseada em performance dos alertas.

Fluxo de auto-recalibracao:
1. detect_regime_change() roda a cada hora
2. Se DEGRADED: pausa alertas, dispara recalibracao completa, notifica
3. recalibrate() roda diariamente (06:00) — analise de 7 dias
4. Se regime volta a HEALTHY: reativa alertas automaticamente

Persiste estado de regime no DB (regime_checks) para historico.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from loguru import logger

from src.core.scheduled_backtest import ScheduledBacktest


class AutoRecalibrator:
    """Recalibra fatores do modelo baseado em performance real."""

    def __init__(self, stats_engine, alert_repo, match_repo, method_stats_repo=None, notifier=None):
        self.stats = stats_engine
        self.alerts = alert_repo
        self.matches = match_repo
        self.method_stats = method_stats_repo
        self.notifier = notifier
        # Estado em memoria — alertas pausados por regime degradado
        self._alerts_paused: bool = False
        self._last_regime_status: str = "HEALTHY"
        self._degraded_since: datetime | None = None

    @property
    def alerts_paused(self) -> bool:
        return self._alerts_paused

    async def recalibrate(self) -> dict:
        """Recalibracao principal. Chamada diariamente pelo scheduler.

        1. Analisa alertas validados dos ultimos 7 dias
        2. Calcula hit rate real vs esperada por layer
        3. Atualiza listas de elite/blacklist baseado em performance
        4. Se regime DEGRADED, roda backtest completo para recalibrar method_stats
        5. Notifica via Telegram
        """
        report = {"changes": [], "regime": self._last_regime_status, "recalibrated": False}

        try:
            from sqlalchemy import select
            from src.db.models import Alert

            # 1. Buscar alertas validados recentes
            cutoff = datetime.now(timezone.utc) - timedelta(days=7)
            stmt = (
                select(Alert)
                .where(Alert.validated_at.is_not(None), Alert.sent_at >= cutoff)
            )
            result = await self.alerts.execute_query(stmt)
            alerts = result.scalars().all()

            if len(alerts) < 10:
                logger.info(f"Recalibration: apenas {len(alerts)} alertas validados (min 10)")
                return report

            # 2. Hit rate geral
            total = len(alerts)
            hits = sum(1 for a in alerts if self._is_hit(a))
            hit_rate = hits / total
            expected_rate = 0.55
            logger.info(
                f"Recalibration: {total} alertas, {hits} hits ({hit_rate:.1%}), "
                f"esperado ~{expected_rate:.0%}"
            )

            # 3. Regime check
            if hit_rate < 0.35 and total >= 15:
                report["regime"] = "DEGRADED"
                report["changes"].append(
                    f"REGIME DEGRADED: hit rate {hit_rate:.0%} ({hits}/{total}) nos ultimos 7 dias"
                )
                # Trigger recalibracao completa via backtest
                await self._trigger_full_recalibration(report)
            elif hit_rate < 0.45 and total >= 15:
                report["regime"] = "WARNING"
                report["changes"].append(
                    f"REGIME WARNING: hit rate {hit_rate:.0%} ({hits}/{total})"
                )

            # 4. Performance por jogador — detectar novos elite/blacklist
            by_player = defaultdict(lambda: {"total": 0, "hits": 0})
            for a in alerts:
                p = a.losing_player
                by_player[p]["total"] += 1
                if self._is_hit(a):
                    by_player[p]["hits"] += 1

            elite_candidates = []
            blacklist_candidates = []

            for player, stats in by_player.items():
                if stats["total"] < 5:
                    continue
                rate = stats["hits"] / stats["total"]
                if rate >= 0.70:
                    elite_candidates.append((player, rate, stats["total"]))
                    if player not in self.stats.PLAYER_ELITE:
                        report["changes"].append(
                            f"ELITE candidato: {player} {stats['hits']}/{stats['total']} ({rate:.0%})"
                        )
                elif rate <= 0.20:
                    blacklist_candidates.append((player, rate, stats["total"]))
                    if player not in self.stats.PLAYER_BLACKLIST:
                        report["changes"].append(
                            f"BLACKLIST candidato: {player} {stats['hits']}/{stats['total']} ({rate:.0%})"
                        )

            # 5. Auto-update elite/blacklist sets no StatsEngine (em memoria)
            for player, rate, n in elite_candidates:
                if n >= 8 and player not in self.stats.PLAYER_ELITE:
                    self.stats.PLAYER_ELITE.add(player)
                    report["changes"].append(f"ELITE adicionado: {player} ({rate:.0%}, n={n})")
                    logger.info(f"Auto-recal: {player} adicionado a ELITE ({rate:.0%})")

            for player, rate, n in blacklist_candidates:
                if n >= 8 and player not in self.stats.PLAYER_BLACKLIST:
                    self.stats.PLAYER_BLACKLIST.add(player)
                    report["changes"].append(f"BLACKLIST adicionado: {player} ({rate:.0%}, n={n})")
                    logger.info(f"Auto-recal: {player} adicionado a BLACKLIST ({rate:.0%})")

            # 6. Performance por hora
            by_hour = defaultdict(lambda: {"total": 0, "hits": 0})
            for a in alerts:
                h = a.sent_at.hour if a.sent_at else 12
                by_hour[h]["total"] += 1
                if self._is_hit(a):
                    by_hour[h]["hits"] += 1

            for hour, stats in sorted(by_hour.items()):
                if stats["total"] < 3:
                    continue
                rate = stats["hits"] / stats["total"]
                if rate < 0.25:
                    report["changes"].append(
                        f"Hora {hour:02d}h: hit rate baixo {stats['hits']}/{stats['total']} ({rate:.0%})"
                    )

            # 7. Persistir regime check no DB
            await self._persist_regime_check(report, hit_rate, total)

            # 8. Notificar
            if report["changes"] and self.notifier:
                await self._send_recalibration_report(report, hit_rate, total)

            report["recalibrated"] = True
            logger.info(f"Recalibration complete: {len(report['changes'])} observacoes")

        except Exception as e:
            logger.error(f"Recalibration failed: {e}")

        return report

    async def detect_regime_change(self) -> dict:
        """Deteccao rapida de regime change (roda a cada hora).

        Compara hit rate dos ultimos 10 alertas vs historico.
        Se DEGRADED: pausa alertas automaticamente.
        Se volta a HEALTHY: reativa alertas.
        """
        try:
            from sqlalchemy import select
            from src.db.models import Alert

            # Ultimos 10 alertas validados
            stmt = (
                select(Alert)
                .where(Alert.validated_at.is_not(None))
                .order_by(Alert.sent_at.desc())
                .limit(10)
            )
            result = await self.alerts.execute_query(stmt)
            recent = result.scalars().all()

            if len(recent) < 8:
                return {"status": "INSUFFICIENT_DATA"}

            recent_hits = sum(1 for a in recent if self._is_hit(a))
            recent_rate = recent_hits / len(recent)

            # Historico (ultimos 100)
            stmt2 = (
                select(Alert)
                .where(Alert.validated_at.is_not(None))
                .order_by(Alert.sent_at.desc())
                .limit(100)
            )
            result2 = await self.alerts.execute_query(stmt2)
            historical = result2.scalars().all()

            if len(historical) < 20:
                return {"status": "INSUFFICIENT_DATA"}

            hist_hits = sum(1 for a in historical if self._is_hit(a))
            hist_rate = hist_hits / len(historical)

            delta = recent_rate - hist_rate
            status = "HEALTHY"

            if delta <= -0.20:
                status = "DEGRADED"
            elif delta <= -0.10:
                status = "WARNING"

            # Auto-acao baseada em transicao de regime
            prev_status = self._last_regime_status
            self._last_regime_status = status

            if status == "DEGRADED" and not self._alerts_paused:
                # Pausar alertas
                self._alerts_paused = True
                self._degraded_since = datetime.now(timezone.utc)
                logger.warning(
                    f"REGIME DEGRADED — Alertas PAUSADOS automaticamente. "
                    f"Recent: {recent_rate:.0%} vs Historical: {hist_rate:.0%} (delta {delta:+.0%})"
                )
                if self.notifier:
                    await self.notifier.send_message(
                        "\u26a0\ufe0f <b>Regime DEGRADED — Alertas PAUSADOS</b>\n\n"
                        f"Hit rate recente: {recent_rate:.0%} ({recent_hits}/{len(recent)})\n"
                        f"Hit rate historico: {hist_rate:.0%} ({hist_hits}/{len(historical)})\n"
                        f"Delta: {delta:+.0%}\n\n"
                        "Alertas pausados automaticamente.\n"
                        "Recalibracao automatica sera executada na proxima rodada (06:00).\n"
                        "Alertas serao reativados quando regime voltar a HEALTHY."
                    )
                # Trigger recalibracao imediata
                await self._trigger_full_recalibration({"changes": [], "regime": "DEGRADED"})

            elif status == "HEALTHY" and self._alerts_paused:
                # Reativar alertas
                self._alerts_paused = False
                paused_duration = ""
                if self._degraded_since:
                    hours = (datetime.now(timezone.utc) - self._degraded_since).total_seconds() / 3600
                    paused_duration = f" (pausados por {hours:.1f}h)"
                self._degraded_since = None
                logger.info(f"REGIME HEALTHY — Alertas REATIVADOS{paused_duration}")
                if self.notifier:
                    await self.notifier.send_message(
                        "\u2705 <b>Regime HEALTHY — Alertas REATIVADOS</b>\n\n"
                        f"Hit rate recente: {recent_rate:.0%}\n"
                        f"Hit rate historico: {hist_rate:.0%}\n"
                        f"Delta: {delta:+.0%}\n\n"
                        f"Alertas reativados automaticamente{paused_duration}."
                    )

            elif status == "WARNING" and prev_status == "HEALTHY":
                logger.warning(f"Regime WARNING: recent {recent_rate:.0%} vs hist {hist_rate:.0%}")
                if self.notifier:
                    await self.notifier.send_message(
                        "\u26a0\ufe0f <b>Regime WARNING</b>\n\n"
                        f"Hit rate recente: {recent_rate:.0%} ({recent_hits}/{len(recent)})\n"
                        f"Hit rate historico: {hist_rate:.0%}\n"
                        f"Delta: {delta:+.0%}\n\n"
                        "Alertas continuam ativos, mas monitorando de perto."
                    )

            # Persistir check
            await self._persist_regime_check(
                {"regime": status, "changes": []},
                recent_rate,
                len(recent),
            )

            return {
                "status": status,
                "recent_rate": recent_rate,
                "historical_rate": hist_rate,
                "delta": delta,
                "alerts_paused": self._alerts_paused,
            }

        except Exception as e:
            logger.error(f"Regime detection failed: {e}")
            return {"status": "ERROR"}

    async def _trigger_full_recalibration(self, report: dict) -> None:
        """Dispara backtest completo para recalibrar method_stats."""
        if not self.method_stats:
            logger.warning("Recalibration: method_stats_repo nao disponivel, skip backtest")
            return

        logger.info("Regime DEGRADED: disparando backtest completo para recalibracao...")

        try:
            backtest = ScheduledBacktest(self.matches, self.method_stats, self.notifier)
            bt_report = await backtest.run_and_notify()

            if bt_report.get("status") == "ok":
                report["changes"].append(
                    f"Backtest recalibracao: {bt_report['total_pairs']} pares, "
                    f"O2.5={bt_report['global_rate_25']:.1%}, "
                    f"ROI@1.80={bt_report['roi_25']:+.1f}%"
                )
                logger.info(
                    f"Recalibracao completa: {bt_report['total_pairs']} pares reprocessados, "
                    f"method_stats atualizadas"
                )
            else:
                report["changes"].append(
                    f"Backtest falhou: {bt_report.get('message', 'unknown')}"
                )
        except Exception as e:
            logger.error(f"Full recalibration failed: {e}")
            report["changes"].append(f"Recalibracao falhou: {e}")

    async def _persist_regime_check(self, report: dict, hit_rate: float, total: int) -> None:
        """Salva regime check no banco para historico."""
        try:
            from src.db.models import RegimeCheck
            regime_check = RegimeCheck(
                checked_at=datetime.now(timezone.utc),
                window_size=total,
                recent_rate=hit_rate,
                historical_rate=0.55,
                z_score=0.0,
                status=report["regime"],
                action_taken="; ".join(report["changes"][:3]) if report["changes"] else None,
            )
            await self.alerts.save_model(regime_check)
        except Exception as e:
            logger.debug(f"Could not persist regime check: {e}")

    async def _send_recalibration_report(
        self, report: dict, hit_rate: float, total: int
    ) -> None:
        """Envia relatorio de recalibracao via Telegram."""
        regime = report["regime"]
        regime_icon = {
            "HEALTHY": "\u2705",
            "WARNING": "\u26a0\ufe0f",
            "DEGRADED": "\ud83d\uded1",
        }.get(regime, "\u2753")

        changes_str = "\n".join(f"  \u2022 {c}" for c in report["changes"])
        paused_str = "\n\n<b>Alertas PAUSADOS</b>" if self._alerts_paused else ""

        msg = (
            f"\ud83d\udd04 <b>Recalibracao Automatica</b>\n\n"
            f"Alertas analisados: {total} (7 dias)\n"
            f"Hit rate: {hit_rate:.0%}\n"
            f"Regime: {regime_icon} {regime}\n\n"
            f"<b>Observacoes:</b>\n{changes_str}"
            f"{paused_str}"
        )
        try:
            await self.notifier.send_message(msg)
        except Exception as e:
            logger.error(f"Failed to send recalibration report: {e}")

    @staticmethod
    def _is_hit(alert) -> bool:
        """Verifica se o alerta acertou baseado na best_line."""
        bl = alert.best_line or "over25"
        if bl == "over45":
            return bool(alert.over45_hit)
        elif bl == "over35":
            return bool(alert.over35_hit)
        elif bl == "over15":
            return bool(getattr(alert, "over15_hit", alert.over25_hit))
        return bool(alert.over25_hit)
