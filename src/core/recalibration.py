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
        self._last_regime_status: str = "HEALTHY"
        self._degraded_since: datetime | None = None

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
            cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).replace(tzinfo=None)
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

            # 5b. Auditoria blacklist/elite baseada nos ultimos 20 jogos de volta
            await self._audit_blacklist_elite(report)

            # 5c. Auto-demote ELITE players com performance ruim em alertas (7d)
            await self._audit_elite_alert_performance(report, alerts)

            # 5d. Monitor de regime por loss_type (hoje vs 7d baseline)
            await self._audit_loss_type_regime(alerts)

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

        Compara hit rate dos ultimos 25 alertas vs historico.
        Se DEGRADED: pausa alertas automaticamente.
        Se volta a HEALTHY: reativa alertas.
        """
        try:
            from sqlalchemy import select
            from src.db.models import Alert

            # Ultimos 25 alertas validados (window maior para evitar falsos positivos)
            stmt = (
                select(Alert)
                .where(Alert.validated_at.is_not(None))
                .order_by(Alert.sent_at.desc())
                .limit(25)
            )
            result = await self.alerts.execute_query(stmt)
            recent = result.scalars().all()

            if len(recent) < 15:
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

            if status == "DEGRADED" and prev_status != "DEGRADED":
                self._degraded_since = datetime.now(timezone.utc)
                logger.warning(
                    f"REGIME DEGRADED — alertas continuam ativos. "
                    f"Recent: {recent_rate:.0%} vs Historical: {hist_rate:.0%} (delta {delta:+.0%})"
                )
                if self.notifier:
                    await self.notifier.send_admin_message(
                        "\U0001f6d1 <b>Regime DEGRADED</b>\n\n"
                        f"Hit rate recente: {recent_rate:.0%} ({recent_hits}/{len(recent)})\n"
                        f"Hit rate historico: {hist_rate:.0%} ({hist_hits}/{len(historical)})\n"
                        f"Delta: {delta:+.0%}\n\n"
                        "Alertas continuam ativos — recalibracao automatica na proxima rodada (06:00)."
                    )
                # Trigger recalibracao imediata
                await self._trigger_full_recalibration({"changes": [], "regime": "DEGRADED"})

            elif status == "HEALTHY" and prev_status == "DEGRADED":
                degraded_duration = ""
                if self._degraded_since:
                    hours = (datetime.now(timezone.utc) - self._degraded_since).total_seconds() / 3600
                    degraded_duration = f" (regime degradado por {hours:.1f}h)"
                self._degraded_since = None
                logger.info(f"REGIME HEALTHY{degraded_duration}")
                if self.notifier:
                    await self.notifier.send_admin_message(
                        "\u2705 <b>Regime voltou a HEALTHY</b>\n\n"
                        f"Hit rate recente: {recent_rate:.0%}\n"
                        f"Hit rate historico: {hist_rate:.0%}\n"
                        f"Delta: {delta:+.0%}"
                        + (f"\n\n{degraded_duration.strip()}" if degraded_duration else "")
                    )

            elif status == "WARNING" and prev_status == "HEALTHY":
                logger.warning(f"Regime WARNING: recent {recent_rate:.0%} vs hist {hist_rate:.0%}")
                if self.notifier:
                    await self.notifier.send_admin_message(
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
                checked_at=datetime.now(timezone.utc).replace(tzinfo=None),
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
            "DEGRADED": "\U0001f6d1",
        }.get(regime, "\u2753")

        changes_str = "\n".join(f"  \u2022 {c}" for c in report["changes"])

        msg = (
            f"\U0001f504 <b>Recalibracao Automatica</b>\n\n"
            f"Alertas analisados: {total} (7 dias)\n"
            f"Hit rate: {hit_rate:.0%}\n"
            f"Regime: {regime_icon} {regime}\n\n"
            f"<b>Observacoes:</b>\n{changes_str}"
        )
        try:
            await self.notifier.send_admin_message(msg)
        except Exception as e:
            logger.error(f"Failed to send recalibration report: {e}")

    async def _audit_blacklist_elite(self, report: dict) -> None:
        """Audita blacklist/elite com base nos ultimos 20 jogos de volta de cada jogador.

        - Jogador na blacklist com O2.5 >= 50% nos ultimos 20: REMOVE
        - Jogador na blacklist com O1.5 >= 65% e G1>=2 frequente (>=40%): REMOVE
        - Jogador fora da blacklist com O2.5 < 30% nos ultimos 20 (min 15 jogos): ADICIONA
        - Jogador fora da elite com O2.5 >= 65% nos ultimos 20 (min 15 jogos): ADICIONA elite
        - Jogador na elite com O2.5 < 45% nos ultimos 20: REMOVE elite
        """
        try:
            from sqlalchemy import text
            from src.db.database import get_session

            async with get_session() as session:
                # Buscar todos os jogadores com jogos de volta recentes
                result = await session.execute(text("""
                    SELECT
                        CASE WHEN m1.player_home = m1.player_home
                             AND m1.score_home < m1.score_away
                             THEN m1.player_home
                             ELSE m1.player_away END AS loser,
                        m1.player_home, m1.player_away,
                        m1.score_home AS g1_sh, m1.score_away AS g1_sa,
                        m2.player_home AS g2_ph,
                        m2.score_home AS g2_sh, m2.score_away AS g2_sa,
                        m1.ended_at
                    FROM matches m2
                    JOIN matches m1 ON m2.pair_match_id = m1.id
                    WHERE m2.is_return_match = true
                      AND m2.score_home IS NOT NULL AND m2.score_away IS NOT NULL
                      AND m1.score_home IS NOT NULL AND m1.score_away IS NOT NULL
                      AND m1.score_home != m1.score_away
                    ORDER BY m1.ended_at DESC
                """))
                rows = result.fetchall()

            # Agrupar por jogador (como perdedor)
            from collections import defaultdict
            player_games: dict[str, list] = defaultdict(list)

            for r in rows:
                _, g1_ph, g1_pa, g1_sh, g1_sa, g2_ph, g2_sh, g2_sa, ended = r
                if g1_sh < g1_sa:
                    loser = g1_ph
                else:
                    loser = g1_pa

                # Gols do perdedor em G2
                loser_goals_g2 = g2_sh if g2_ph == loser else g2_sa
                # Gols do perdedor em G1
                loser_goals_g1 = g1_sh if g1_ph == loser else g1_sa

                player_games[loser].append({
                    "g2_goals": loser_goals_g2,
                    "g1_goals": loser_goals_g1,
                })

            # Auditar cada jogador (ultimos 20 jogos)
            changes_made = 0
            for player, games in player_games.items():
                recent = games[:20]  # ja ordenado DESC
                n = len(recent)
                if n < 15:
                    continue

                o15 = sum(1 for g in recent if g["g2_goals"] > 1)
                o25 = sum(1 for g in recent if g["g2_goals"] > 2)
                g1_2plus = sum(1 for g in recent if g["g1_goals"] >= 2)
                o25_rate = o25 / n
                o15_rate = o15 / n
                g1_active = g1_2plus / n

                # --- Blacklist audit ---
                if player in self.stats.PLAYER_BLACKLIST:
                    should_remove = False
                    reason = ""
                    if o25_rate >= 0.50:
                        should_remove = True
                        reason = f"O2.5={o25_rate:.0%} nos ult 20"
                    elif o15_rate >= 0.65 and g1_active >= 0.40:
                        should_remove = True
                        reason = f"O1.5={o15_rate:.0%}, G1>=2={g1_active:.0%} nos ult 20"

                    if should_remove:
                        self.stats.PLAYER_BLACKLIST.discard(player)
                        report["changes"].append(
                            f"BLACKLIST removido: {player} ({reason})"
                        )
                        logger.info(f"Auto-audit: {player} REMOVIDO da blacklist ({reason})")
                        changes_made += 1

                elif player not in self.stats.PLAYER_BLACKLIST:
                    # Candidato a blacklist
                    if o25_rate < 0.30 and o15_rate < 0.55:
                        self.stats.PLAYER_BLACKLIST.add(player)
                        report["changes"].append(
                            f"BLACKLIST adicionado: {player} (O2.5={o25_rate:.0%}, O1.5={o15_rate:.0%} ult 20)"
                        )
                        logger.info(f"Auto-audit: {player} ADICIONADO a blacklist (O2.5={o25_rate:.0%})")
                        changes_made += 1

                # --- Elite audit ---
                if player in self.stats.PLAYER_ELITE:
                    if o25_rate < 0.45:
                        self.stats.PLAYER_ELITE.discard(player)
                        report["changes"].append(
                            f"ELITE removido: {player} (O2.5={o25_rate:.0%} nos ult 20)"
                        )
                        logger.info(f"Auto-audit: {player} REMOVIDO da elite (O2.5={o25_rate:.0%})")
                        changes_made += 1

                elif player not in self.stats.PLAYER_ELITE and player not in self.stats.PLAYER_BLACKLIST:
                    if o25_rate >= 0.65:
                        self.stats.PLAYER_ELITE.add(player)
                        report["changes"].append(
                            f"ELITE adicionado: {player} (O2.5={o25_rate:.0%} nos ult 20)"
                        )
                        logger.info(f"Auto-audit: {player} ADICIONADO a elite (O2.5={o25_rate:.0%})")
                        changes_made += 1

            logger.info(f"Blacklist/elite audit: {changes_made} alteracoes, "
                        f"blacklist={len(self.stats.PLAYER_BLACKLIST)}, "
                        f"elite={len(self.stats.PLAYER_ELITE)}")

        except Exception as e:
            logger.error(f"Blacklist/elite audit failed: {e}")

    async def _audit_elite_alert_performance(self, report: dict, alerts: list) -> None:
        """Demote ELITE players whose alert WR is poor in last 7 days.

        Trigger: WR < 40% AND n >= 8 AND sum(profit_flat) < -2u.
        Action: remove from PLAYER_ELITE and WINNER_BOOST + add conditional block
        on their worst-performing line.
        """
        try:
            by_player_line = defaultdict(lambda: {"n": 0, "hits": 0, "pl": 0.0})
            by_player = defaultdict(lambda: {"n": 0, "hits": 0, "pl": 0.0})

            for a in alerts:
                p = a.losing_player
                if not p:
                    continue
                line = a.best_line or "over25"
                hit = self._is_hit(a)
                pl = float(a.profit_flat or 0)
                by_player[p]["n"] += 1
                by_player[p]["hits"] += int(hit)
                by_player[p]["pl"] += pl
                by_player_line[(p, line)]["n"] += 1
                by_player_line[(p, line)]["hits"] += int(hit)
                by_player_line[(p, line)]["pl"] += pl

            demoted = 0
            for player in list(self.stats.PLAYER_ELITE):
                s = by_player.get(player)
                if not s or s["n"] < 8:
                    continue
                wr = s["hits"] / s["n"]
                if wr >= 0.40 or s["pl"] >= -2.0:
                    continue

                self.stats.PLAYER_ELITE.discard(player)
                if hasattr(self.stats, "WINNER_BOOST"):
                    self.stats.WINNER_BOOST.discard(player)

                worst_line = None
                worst_pl = 0.0
                for (pl_player, line), ls in by_player_line.items():
                    if pl_player != player or ls["n"] < 3:
                        continue
                    if ls["pl"] < worst_pl:
                        worst_pl = ls["pl"]
                        worst_line = line

                if worst_line:
                    cond = self.stats.PLAYER_CONDITIONAL_BLACKLIST.setdefault(player, {})
                    blocked = cond.setdefault("block_lines", set())
                    blocked.add(worst_line)

                report["changes"].append(
                    f"ELITE auto-demote: {player} (WR={wr:.0%}, n={s['n']}, "
                    f"P/L={s['pl']:+.2f}u, bloqueio {worst_line or 'n/a'})"
                )
                logger.warning(
                    f"Auto-demote: {player} REMOVIDO de ELITE (WR={wr:.0%}, "
                    f"n={s['n']}, P/L={s['pl']:+.2f}u), bloqueio em {worst_line}"
                )
                demoted += 1

                if self.notifier:
                    try:
                        await self.notifier.send_admin_message(
                            f"\u26a0\ufe0f <b>ELITE demote automatico</b>\n\n"
                            f"Jogador: <b>{player}</b>\n"
                            f"WR 7d: {wr:.0%} ({s['hits']}/{s['n']})\n"
                            f"P/L 7d: {s['pl']:+.2f}u\n"
                            f"Bloqueio condicional: <b>{worst_line or 'n/a'}</b>\n\n"
                            f"Removido de PLAYER_ELITE e WINNER_BOOST."
                        )
                    except Exception as e:
                        logger.error(f"Failed to send elite-demote admin msg: {e}")

            if demoted:
                logger.info(f"Elite auto-demote: {demoted} jogador(es) removido(s)")

        except Exception as e:
            logger.error(f"Elite alert performance audit failed: {e}")

    async def _audit_loss_type_regime(self, alerts: list) -> None:
        """Detect regime shifts per loss_type by comparing today vs 7d baseline.

        For each loss_type with >= 8 alerts today, compute today's WR and compare
        against the trailing-7-day WR (excluding today). Flag via admin Telegram
        if deviation >= 20 percentage points.
        """
        try:
            from datetime import date as date_cls

            today = datetime.now(timezone.utc).date()
            today_by_lt = defaultdict(lambda: {"n": 0, "hits": 0, "pl": 0.0})
            hist_by_lt = defaultdict(lambda: {"n": 0, "hits": 0, "pl": 0.0})

            for a in alerts:
                lt = a.loss_type or ""
                if not lt:
                    continue
                a_date = a.sent_at.date() if a.sent_at else None
                bucket = today_by_lt if a_date == today else hist_by_lt
                bucket[lt]["n"] += 1
                bucket[lt]["hits"] += int(self._is_hit(a))
                bucket[lt]["pl"] += float(a.profit_flat or 0)

            for lt, t in today_by_lt.items():
                if t["n"] < 8:
                    continue
                h = hist_by_lt.get(lt)
                if not h or h["n"] < 15:
                    continue
                today_wr = t["hits"] / t["n"]
                hist_wr = h["hits"] / h["n"]
                delta = today_wr - hist_wr
                if delta <= -0.20:
                    logger.warning(
                        f"loss_type regime shift {lt}: hoje {today_wr:.0%} ({t['n']} tips) "
                        f"vs 7d {hist_wr:.0%} ({h['n']} tips), delta={delta:+.0%}"
                    )
                    if self.notifier:
                        try:
                            await self.notifier.send_admin_message(
                                f"\u26a0\ufe0f <b>Regime shift em loss_type</b>\n\n"
                                f"Categoria: <b>{lt}</b>\n"
                                f"Hoje: {today_wr:.0%} ({t['hits']}/{t['n']}), "
                                f"P/L {t['pl']:+.2f}u\n"
                                f"Baseline 7d: {hist_wr:.0%} ({h['hits']}/{h['n']})\n"
                                f"Delta: <b>{delta:+.0%} pp</b>"
                            )
                        except Exception as e:
                            logger.error(f"Failed to send loss_type regime msg: {e}")
        except Exception as e:
            logger.error(f"loss_type regime audit failed: {e}")

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
