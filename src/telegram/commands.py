"""Interactive Telegram bot commands.

Correções aplicadas (auditoria 2025-03-25):
- BUG 1: BotCommands agora recebe player_repo como parâmetro (instância com sessão)
- BUG 2: cmd_player usa self.players (instância) em vez de PlayerRepository (classe)
- BUG 3: cmd_status converte ColdStartProgress dataclass para dict corretamente
- BUG 5: cmd_stats calcula ROI a partir dos dados disponíveis (sem campo roi_flat)
- cmd_progress converte dataclass para dict antes de passar ao template
"""

from __future__ import annotations

from loguru import logger
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from src.core.cancelled_alerts import CANCELLED_ALERT_IDS


class BotCommands:
    """Handles /commands sent to the bot in Telegram."""

    def __init__(self, notifier, stats_engine, match_repo, alert_repo, league_repo,
                 player_repo=None, odds_monitor=None, alert_v2_repo=None) -> None:
        self.notifier = notifier
        self.stats_engine = stats_engine
        self.matches = match_repo
        self.alerts = alert_repo
        self.leagues = league_repo
        self.players = player_repo  # BUG 2 fix: recebe instância com sessão
        self.odds_monitor = odds_monitor
        self.alerts_v2 = alert_v2_repo  # Method 2 repo (opcional)

    def _is_v2_group(self, update: Update) -> bool:
        """Retorna True se o comando foi enviado no grupo M2."""
        from src.config import settings
        v2_id = settings.telegram_group_v2_id
        if not v2_id:
            return False
        chat_id = str(update.effective_chat.id) if update.effective_chat else ""
        return chat_id == str(v2_id)

    def _is_free_group(self, update: Update) -> bool:
        """Retorna True se o comando foi enviado no grupo FREE."""
        from src.config import settings
        free_id = settings.telegram_free_group_id
        if not free_id:
            return False
        chat_id = str(update.effective_chat.id) if update.effective_chat else ""
        return chat_id == str(free_id)

    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/status - System uptime, monitoring info, regime status."""
        try:
            regime = await self.stats_engine.check_regime()
            cold_done = await self.stats_engine.is_cold_start_complete()
            progress = await self.stats_engine.get_cold_start_progress()

            # BUG 3 fix: ColdStartProgress é um dataclass, não dict.
            # Acessar atributos diretamente em vez de .get()
            from src.telegram.messages import format_system_status
            text = format_system_status({
                "uptime": "N/A",
                "games_monitoring": getattr(progress, "total_pairs", 0),
                "pending_pairs": 0,
                "alerts_today": 0,
                "regime_status": regime.get("status", "HEALTHY") if isinstance(regime, dict) else getattr(regime, "status", "HEALTHY"),
                "cold_start_complete": cold_done,
                "alerts_paused": self.notifier._paused,
            })
            await update.message.reply_html(text)
        except Exception as e:
            logger.error(f"cmd_status error: {e}")
            await update.message.reply_text("Erro ao buscar status.")

    async def cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/stats - Overall statistics."""
        try:
            if self._is_v2_group(update) and self.alerts_v2:
                days = int(context.args[0]) if context.args else 30
                pnl = await self.alerts_v2.get_pnl_summary(days)
                total = pnl.get("total", 0)
                wins = pnl.get("wins", 0)
                losses = pnl.get("losses", 0)
                rate = pnl.get("hit_rate", 0)
                profit = pnl.get("profit", 0.0)
                roi = pnl.get("roi", 0.0)
                by_camada = pnl.get("by_camada", {})
                camada_str = "\n".join(
                    f"  {c}: {v['wins']}/{v['total']} = {v['wins']/v['total']:.0%} ({v['profit']:+.1f}u)"
                    for c, v in sorted(by_camada.items()) if v["total"] > 0
                )
                text = (
                    f"\U0001f4ca <b>M2 STATS (ultimos {days} dias)</b>\n\n"
                    f"Alertas: {total} ({wins}W / {losses}L)\n"
                    f"Hit rate: {rate:.0%}\n"
                    f"Profit: <b>{profit:+.1f}u</b>  ROI: <b>{roi:.1%}</b>\n"
                )
                if camada_str:
                    text += f"\n<b>Por camada:</b>\n{camada_str}"
                await update.message.reply_html(text)
                return

            stats = await self.alerts.get_period_stats(days=30)
            total = stats.get("total", 0)
            validated = stats.get("validated", 0)
            hits = stats.get("over25_hits", 0)
            rate = (hits / validated * 100) if validated > 0 else 0
            hit_rate_25 = stats.get("hit_rate_25", 0)

            text = (
                f"📊 <b>STATS GERAIS (últimos 30 dias)</b>\n\n"
                f"   • Total alertas: {total}\n"
                f"   • Validados: {validated}\n"
                f"   • Over 2.5 bateu: {hits}/{validated} ({rate:.1f}%)\n"
                f"   • Hit rate O2.5: {hit_rate_25:.1%}\n"
                f"   • Hit rate O3.5: {stats.get('hit_rate_35', 0):.1%}"
            )
            await update.message.reply_html(text)
        except Exception as e:
            logger.error(f"cmd_stats error: {e}")
            await update.message.reply_text("Erro ao buscar stats.")

    async def cmd_today(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/today - Today's summary so far."""
        try:
            from datetime import date
            stats = await self.alerts.get_daily_stats(date.today())
            total = stats.get("total", 0)
            validated = stats.get("validated", 0)
            hits = stats.get("over25_hits", 0)
            rate = (hits / validated * 100) if validated > 0 else 0

            text = (
                f"📅 <b>HOJE</b>\n\n"
                f"   • Alertas enviados: {total}\n"
                f"   • Validados: {validated}\n"
                f"   • Bateram (over 2.5): {hits}/{validated} ({rate:.1f}%)"
            )
            await update.message.reply_html(text)
        except Exception as e:
            logger.error(f"cmd_today error: {e}")
            await update.message.reply_text("Erro ao buscar dados de hoje.")

    async def cmd_player(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/player <name> - Player profile."""
        if not context.args:
            await update.message.reply_text("Uso: /player <nome do jogador>")
            return

        name = " ".join(context.args)
        try:
            # BUG 2 fix: usar self.players (instância com sessão) em vez de classe
            if self.players is None:
                await update.message.reply_text("Player repository não configurado.")
                return

            profile = await self.players.get_profile_by_name(name)
            if not profile:
                await update.message.reply_text(f"Jogador '{name}' não encontrado.")
                return

            total_return = getattr(profile, "total_return_matches", 0) or 0
            over25_after = getattr(profile, "over25_after_loss", 0) or 0
            avg_goals = getattr(profile, "avg_goals_after_loss", 0.0) or 0.0
            is_reliable = getattr(profile, "is_reliable", False)

            hit_rate = (over25_after / total_return * 100) if total_return > 0 else 0
            text = (
                f"👤 <b>{profile.name}</b>\n\n"
                f"   • Jogos após derrota: {total_return}\n"
                f"   • Over 2.5 bateu: {over25_after}/{total_return} "
                f"({hit_rate:.1f}%)\n"
                f"   • Média de gols: {avg_goals:.2f}\n"
                f"   • Confiável: {'✅' if is_reliable else '❌'}"
            )
            await update.message.reply_html(text)
        except Exception as e:
            logger.error(f"cmd_player error: {e}")
            await update.message.reply_text("Erro ao buscar jogador.")

    async def cmd_leagues(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/leagues - Active leagues being monitored."""
        try:
            leagues = await self.leagues.get_active_leagues()
            if not leagues:
                await update.message.reply_text("Nenhuma liga ativa.")
                return

            lines = "\n".join(f"   • {lg.name} (ID: {lg.api_league_id})" for lg in leagues)
            await update.message.reply_html(f"⚽ <b>LIGAS ATIVAS</b>\n\n{lines}")
        except Exception as e:
            logger.error(f"cmd_leagues error: {e}")
            await update.message.reply_text("Erro ao buscar ligas.")

    async def cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/pause - Temporarily pause alerts."""
        self.notifier.pause()
        await update.message.reply_text("⏸️ Alertas pausados. Use /resume para retomar.")

    async def cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/resume - Resume alerts."""
        self.notifier.resume()
        await update.message.reply_text("▶️ Alertas retomados.")

    async def cmd_progress(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/progress - Cold start collection progress."""
        try:
            progress = await self.stats_engine.get_cold_start_progress()
            from src.telegram.messages import format_cold_start_progress

            # BUG 3 fix: converter dataclass para dict antes de passar ao template
            progress_dict = {
                "days_collected": getattr(progress, "days_collected", 0),
                "cold_start_days": getattr(progress, "cold_start_days", 90),
                "total_games": getattr(progress, "total_games", 0),
                "total_pairs": getattr(progress, "total_pairs", 0),
                "unique_players": getattr(progress, "unique_players", 0),
                "unique_teams": getattr(progress, "unique_teams", 0),
                "activation_date": getattr(progress, "activation_date", "N/A"),
            }
            await update.message.reply_html(format_cold_start_progress(progress_dict))
        except Exception as e:
            logger.error(f"cmd_progress error: {e}")
            await update.message.reply_text("Erro ao buscar progresso.")

    async def cmd_pnl(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Mostra P&L (lucro/prejuizo) dos ultimos N dias. Uso: /pnl [dias]"""
        try:
            days = int(context.args[0]) if context.args else 30

            if self._is_v2_group(update) and self.alerts_v2:
                pnl = await self.alerts_v2.get_pnl_summary(days)
                if pnl["total"] == 0:
                    await update.message.reply_text(f"Sem alertas M2 validados nos ultimos {days} dias.")
                    return
                top_players = sorted(pnl["by_player"].items(), key=lambda x: x[1]["profit"], reverse=True)[:5]
                worst_players = sorted(pnl["by_player"].items(), key=lambda x: x[1]["profit"])[:3]
                line_str = "\n".join(
                    f"  {l}: {s['wins']}/{s['total']} = {s['wins']/s['total']:.0%} ({s['profit']:+.1f}u)"
                    for l, s in sorted(pnl["by_line"].items()) if s["total"] > 0
                )
                camada_str = "\n".join(
                    f"  {c}: {s['wins']}/{s['total']} = {s['wins']/s['total']:.0%} ({s['profit']:+.1f}u)"
                    for c, s in sorted(pnl["by_camada"].items()) if s["total"] > 0
                )
                top_str = "\n".join(
                    f"  {p}: {s['wins']}/{s['total']} ({s['profit']:+.1f}u)"
                    for p, s in top_players
                )
                weekly = await self.alerts_v2.get_weekly_breakdown(weeks=4)
                weekly_str = ""
                if weekly:
                    weekly_str = "\n\n<b>Por semana:</b>\n"
                    cumulative = 0.0
                    for w in sorted(weekly, key=lambda x: x["week"]):
                        cumulative += w["profit"]
                        emoji = "\u2705" if w["profit"] >= 0 else "\u274c"
                        weekly_str += (
                            f"  {emoji} {w['week']}: {w['wins']}/{w['total']} "
                            f"({w['profit']:+.1f}u) acum: {cumulative:+.1f}u\n"
                        )
                streak_data = await self.alerts_v2.get_recent_streak(20)
                streak = streak_data["streak"]
                streak_str = f"\U0001f525 Streak: {streak}W" if streak > 0 else (
                    f"\u26a0\ufe0f Streak: {abs(streak)}L" if streak < 0 else ""
                )
                roi_emoji = "\U0001f4c8" if pnl["roi"] > 0 else "\U0001f4c9"
                msg = (
                    f"{roi_emoji} <b>M2 P&L - Ultimos {days} dias</b>\n\n"
                    f"Alertas: {pnl['total']} ({pnl['wins']}W / {pnl['losses']}L)\n"
                    f"Hit rate: {pnl['hit_rate']:.0%}\n"
                    f"Profit: <b>{pnl['profit']:+.1f} unidades</b>\n"
                    f"ROI: <b>{pnl['roi']:.1%}</b>\n"
                )
                if streak_str:
                    msg += f"{streak_str}\n"
                if camada_str:
                    msg += f"\n<b>Por camada:</b>\n{camada_str}\n"
                if line_str:
                    msg += f"\n<b>Por linha:</b>\n{line_str}\n"
                if top_str:
                    msg += f"\n<b>Top jogadores:</b>\n{top_str}"
                msg += weekly_str
                await update.message.reply_text(msg, parse_mode="HTML")
                return

            pnl = await self.alerts.get_pnl_summary(days)

            if pnl["total"] == 0:
                await update.message.reply_text(f"Sem alertas validados nos ultimos {days} dias.")
                return

            # Top jogadores por profit
            top_players = sorted(pnl["by_player"].items(), key=lambda x: x[1]["profit"], reverse=True)[:5]
            worst_players = sorted(pnl["by_player"].items(), key=lambda x: x[1]["profit"])[:3]

            top_str = "\n".join(
                f"  {p}: {s['wins']}/{s['total']} ({s['profit']:+.1f}u)"
                for p, s in top_players
            )
            worst_str = "\n".join(
                f"  {p}: {s['wins']}/{s['total']} ({s['profit']:+.1f}u)"
                for p, s in worst_players
            )

            # Por linha
            line_str = "\n".join(
                f"  {l}: {s['wins']}/{s['total']} = {s['wins']/s['total']:.0%} ({s['profit']:+.1f}u)"
                for l, s in sorted(pnl["by_line"].items())
            )

            # Breakdown semanal
            weekly = await self.alerts.get_weekly_breakdown(weeks=4)
            weekly_str = ""
            if weekly:
                weekly_str = "\n\n<b>Por semana:</b>\n"
                cumulative = 0.0
                for w in weekly:
                    cumulative += w["profit"]
                    emoji = "\u2705" if w["profit"] >= 0 else "\u274c"
                    weekly_str += (
                        f"  {emoji} {w['week']}: {w['wins']}/{w['total']} "
                        f"({w['profit']:+.1f}u) acum: {cumulative:+.1f}u\n"
                    )

            # Streak atual
            streak_data = await self.alerts.get_recent_streak(20)
            streak = streak_data["streak"]
            if streak > 0:
                streak_str = f"\U0001f525 Streak: {streak}W seguidas"
            elif streak < 0:
                streak_str = f"\u26a0\ufe0f Streak: {abs(streak)}L seguidas"
            else:
                streak_str = ""

            roi_emoji = "\U0001f4c8" if pnl["roi"] > 0 else "\U0001f4c9"
            msg = (
                f"{roi_emoji} <b>P&L - Ultimos {days} dias</b>\n\n"
                f"Alertas: {pnl['total']} ({pnl['wins']}W / {pnl['losses']}L)\n"
                f"Hit rate: {pnl['hit_rate']:.0%}\n"
                f"Profit: <b>{pnl['profit']:+.1f} unidades</b>\n"
                f"ROI: <b>{pnl['roi']:.1%}</b>\n"
            )
            if streak_str:
                msg += f"{streak_str}\n"
            msg += (
                f"\n<b>Por linha:</b>\n{line_str}\n\n"
                f"<b>Top jogadores:</b>\n{top_str}\n\n"
                f"<b>Piores:</b>\n{worst_str}"
                f"{weekly_str}"
            )
            await update.message.reply_text(msg, parse_mode="HTML")
        except Exception as e:
            logger.error(f"cmd_pnl error: {e}")
            await update.message.reply_text("Erro ao calcular P&L.")

    async def cmd_players(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/players - Dashboard de performance por jogador."""
        try:
            days = int(context.args[0]) if context.args else 30
            if self._is_v2_group(update) and self.alerts_v2:
                players = await self.alerts_v2.get_player_performance(days=days, min_alerts=2)
            else:
                players = await self.alerts.get_player_performance(days=days, min_alerts=2)

            if not players:
                await update.message.reply_text(f"Sem dados de jogadores nos ultimos {days} dias.")
                return

            # Separar lucrativos e deficitarios
            profitable = [p for p in players if p["profit"] > 0]
            losing = [p for p in players if p["profit"] <= 0]

            prefix = "M2 " if self._is_v2_group(update) and self.alerts_v2 else ""
            lines = []
            lines.append(f"\U0001f4ca <b>{prefix}PERFORMANCE POR JOGADOR ({days}d)</b>\n")

            if profitable:
                lines.append("<b>\u2705 Lucrativos:</b>")
                for p in profitable[:8]:
                    bar = "\u2588" * min(int(p["profit"]), 10)
                    lines.append(
                        f"  <b>{p['player']}</b>: {p['wins']}/{p['total']} "
                        f"({p['hit_rate']:.0%}) <b>{p['profit']:+.1f}u</b> "
                        f"ROI {p['roi']:.0%} {bar}"
                    )

            if losing:
                lines.append("\n<b>\u274c Deficitarios:</b>")
                for p in losing[:5]:
                    lines.append(
                        f"  <b>{p['player']}</b>: {p['wins']}/{p['total']} "
                        f"({p['hit_rate']:.0%}) <b>{p['profit']:+.1f}u</b> "
                        f"ROI {p['roi']:.0%}"
                    )
                    if p["worst_streak"] >= 3:
                        lines[-1] += f" (pior: {p['worst_streak']}L)"

            # Summary
            total_profit = sum(p["profit"] for p in players)
            total_alerts = sum(p["total"] for p in players)
            lines.append(
                f"\n<b>Resumo:</b> {len(profitable)} lucrativos, "
                f"{len(losing)} deficitarios\n"
                f"Net: <b>{total_profit:+.1f}u</b> em {total_alerts} alertas"
            )

            await update.message.reply_text("\n".join(lines), parse_mode="HTML")
        except Exception as e:
            logger.error(f"cmd_players error: {e}")
            await update.message.reply_text("Erro ao buscar performance.")

    async def cmd_results(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/results [dd/mm] - Tabela de resultados do dia (GREEN/RED) com ROI.

        Sem argumento: resultados de hoje.
        Com data: resultados do dia especificado (ex: /results 27/03).
        """
        try:
            from datetime import datetime, timedelta, timezone
            from src.config import settings

            try:
                from zoneinfo import ZoneInfo
                tz_local = ZoneInfo(settings.timezone)
            except Exception:
                tz_local = timezone(timedelta(hours=-3))

            # Determinar a data solicitada
            now_local = datetime.now(tz_local)
            if context.args:
                date_str = context.args[0]
                try:
                    # Aceitar dd/mm ou dd/mm/yyyy
                    parts = date_str.split("/")
                    day = int(parts[0])
                    month = int(parts[1])
                    year = int(parts[2]) if len(parts) > 2 else now_local.year
                    target_date = now_local.replace(year=year, month=month, day=day,
                                                     hour=0, minute=0, second=0, microsecond=0)
                except (ValueError, IndexError):
                    await update.message.reply_text("Formato invalido. Use: /results dd/mm ou /results dd/mm/yyyy")
                    return
            else:
                target_date = now_local.replace(hour=0, minute=0, second=0, microsecond=0)

            # Converter meia-noite local → UTC naive
            start_utc = target_date.astimezone(timezone.utc).replace(tzinfo=None)
            end_utc = (target_date + timedelta(days=1)).astimezone(timezone.utc).replace(tzinfo=None)

            date_label = target_date.strftime("%d/%m/%Y")
            is_v2 = self._is_v2_group(update) and self.alerts_v2
            is_free = self._is_free_group(update)

            if is_v2:
                alerts = await self.alerts_v2.get_results_by_date(start_utc, end_utc)
            else:
                alerts = await self.alerts.get_results_by_date(start_utc, end_utc)

            if not alerts:
                await update.message.reply_text(f"Nenhum alerta em {date_label}.")
                return

            # Filtrar suprimidos (auto-block) — eles NUNCA foram pra grupo nenhum
            # e nao devem aparecer no /results pra nao confundir.
            if not is_v2:
                alerts = [a for a in alerts if not getattr(a, "suppressed", False)]
                if not alerts:
                    await update.message.reply_text(f"Nenhum alerta em {date_label}.")
                    return

            # Se /results foi chamado no grupo FREE, mostra so os que foram pro FREE.
            if is_free:
                alerts = [a for a in alerts if getattr(a, "free_message_id", None)]
                if not alerts:
                    await update.message.reply_text(
                        f"Nenhum alerta FREE em {date_label}."
                    )
                    return

            lines = []
            # Stats VIP (todos os alertas que foram pro grupo)
            total_profit = 0.0
            greens = 0
            total_validated = 0
            # Stats FREE (subset com free_message_id) — calculado em paralelo
            # so quando NAO estamos no grupo FREE (no FREE eh redundante).
            free_profit = 0.0
            free_greens = 0
            free_validated = 0
            free_total = 0

            for a in alerts:
                # Hora local
                sent_utc = a.sent_at
                if sent_utc:
                    sent_local = sent_utc.replace(tzinfo=timezone.utc).astimezone(tz_local)
                    hora = sent_local.strftime("%H:%M")
                else:
                    hora = "??:??"

                player = a.losing_player or "?"
                bl = a.best_line or "over25"

                # Label do mercado
                if bl == "over15":
                    mercado = "O1.5"
                elif bl == "over25":
                    mercado = "O2.5"
                elif bl == "over35":
                    mercado = "O3.5"
                elif bl == "over45":
                    mercado = "O4.5"
                elif bl == "ml":
                    mercado = "ML"
                else:
                    mercado = bl

                # Odds usadas
                if bl == "over15":
                    odds = a.over15_odds
                elif bl == "over25":
                    odds = a.over25_odds
                elif bl == "over35":
                    odds = a.over35_odds
                elif bl == "over45":
                    odds = a.over45_odds
                elif bl == "ml":
                    odds = a.ml_odds
                else:
                    odds = a.over25_odds
                odds_str = f"@{odds:.2f}" if odds else "—"

                # Marca FREE
                is_free_alert = bool(getattr(a, "free_message_id", None))
                if is_free_alert and not is_free:
                    free_total += 1

                # Cancelado pelo owner: nao conta em PL/greens/validated
                is_cancelled = a.id in CANCELLED_ALERT_IDS

                # Resultado
                gols = a.actual_goals
                if is_cancelled:
                    gols = a.actual_goals if a.actual_goals is not None else "—"
                    resultado = "⚪"
                elif gols is not None:
                    total_validated += 1
                    # Determinar hit
                    if bl == "over15":
                        hit = gols > 1
                    elif bl == "over25":
                        hit = gols > 2
                    elif bl == "over35":
                        hit = gols > 3
                    elif bl == "over45":
                        hit = gols > 4
                    elif bl == "ml":
                        hit = getattr(a, "ml_hit", False)
                    else:
                        hit = gols > 2

                    resultado = "🟢" if hit else "🔴"
                    if odds:
                        p = (odds - 1.0) if hit else -1.0
                        total_profit += p
                        if is_free_alert and not is_free:
                            free_profit += p
                    if hit:
                        greens += 1
                        if is_free_alert and not is_free:
                            free_greens += 1
                    if is_free_alert and not is_free:
                        free_validated += 1
                else:
                    gols = "—"
                    resultado = "⏳"

                # Tag FREE so quando estamos no VIP/admin (no FREE seria redundante)
                free_tag = " 🆓" if (is_free_alert and not is_free) else ""
                lines.append(
                    f"{hora} | {player:<12} | {mercado} | {odds_str:<6} | {gols} | {resultado}{free_tag}"
                )

            # Header
            header = "Hora  | Jogador      | Linha | Odds   | G | R"
            sep = "—" * 48

            # ROI VIP (=universo selecionado). Cancelados nao contam pra
            # "aguardando resultado" nem pro total apostavel.
            cancelled_count = sum(1 for a in alerts if a.id in CANCELLED_ALERT_IDS)
            total = len(alerts) - cancelled_count
            losses = total_validated - greens
            roi = (total_profit / total_validated * 100) if total_validated > 0 else 0
            roi_emoji = "📈" if total_profit >= 0 else "📉"

            if is_free:
                # No grupo FREE, soh um sumario (proprio universo)
                label_pl = "FREE"
                summary = (
                    f"\n{sep}\n"
                    f"<b>{label_pl}:</b> ✅ {greens}  ❌ {losses}  |  "
                    f"Net: <b>{total_profit:+.2f}u</b>  |  "
                    f"ROI: <b>{roi:+.1f}%</b> {roi_emoji}"
                )
                if total > total_validated:
                    summary += f"\n⏳ {total - total_validated} aguardando resultado"
            elif is_v2:
                summary = (
                    f"\n{sep}\n"
                    f"✅ {greens}  ❌ {losses}  |  "
                    f"Net: <b>{total_profit:+.2f}u</b>  |  "
                    f"ROI: <b>{roi:+.1f}%</b> {roi_emoji}"
                )
                if total > total_validated:
                    summary += f"\n⏳ {total - total_validated} aguardando resultado"
            else:
                # No VIP/admin DM, mostra VIP + FREE separados
                free_losses = free_validated - free_greens
                free_roi = (free_profit / free_validated * 100) if free_validated > 0 else 0
                free_emoji = "📈" if free_profit >= 0 else "📉"
                summary = (
                    f"\n{sep}\n"
                    f"<b>VIP:</b>  ✅ {greens}  ❌ {losses}  |  "
                    f"Net: <b>{total_profit:+.2f}u</b>  |  "
                    f"ROI: <b>{roi:+.1f}%</b> {roi_emoji}\n"
                    f"<b>FREE:</b> ✅ {free_greens}  ❌ {free_losses}  |  "
                    f"Net: <b>{free_profit:+.2f}u</b>  |  "
                    f"ROI: <b>{free_roi:+.1f}%</b> {free_emoji}  ({free_total}/{total} alertas)"
                )
                if total > total_validated:
                    summary += f"\n⏳ {total - total_validated} aguardando resultado"

            if is_free:
                prefix = "FREE "
            elif is_v2:
                prefix = "M2 "
            else:
                prefix = ""
            msg = (
                f"📋 <b>{prefix}RESULTADOS {date_label}</b>\n\n"
                f"<pre>{header}\n{sep}\n"
                + "\n".join(lines)
                + f"</pre>{summary}"
            )

            await update.message.reply_text(msg, parse_mode="HTML")
        except Exception as e:
            logger.error(f"cmd_results error: {e}")
            await update.message.reply_text(f"Erro ao buscar resultados: {e}")

    async def cmd_monitor(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/monitor - Jogos de volta sendo monitorados agora."""
        try:
            if not self.odds_monitor:
                await update.message.reply_text("OddsMonitor nao disponivel.")
                return

            from datetime import datetime, timedelta, timezone
            try:
                from zoneinfo import ZoneInfo
                from src.config import settings
                tz_local = ZoneInfo(settings.timezone)
            except Exception:
                tz_local = timezone(timedelta(hours=-3))

            tasks = self.odds_monitor._tasks
            meta = self.odds_monitor._task_meta

            if not tasks:
                await update.message.reply_text("Nenhum jogo sendo monitorado agora.")
                return

            lines = []
            for match_id, task in sorted(tasks.items()):
                if task.done():
                    continue
                info = meta.get(match_id, {})
                loser = info.get("loser", "?")
                g1 = info.get("game1_match")

                # Buscar dados do return match
                try:
                    rm = await self.matches.get_by_id(match_id)
                except Exception:
                    rm = None

                if rm:
                    ph = rm.player_home or "?"
                    pa = rm.player_away or "?"
                    th = rm.team_home or ""
                    ta = rm.team_away or ""
                    kickoff = rm.started_at
                    if kickoff:
                        kick_local = kickoff.replace(tzinfo=timezone.utc).astimezone(tz_local)
                        kick_str = kick_local.strftime("%H:%M")
                        now = datetime.now(tz_local)
                        diff = int((kick_local - now).total_seconds() / 60)
                        if diff > 0:
                            time_str = f"em {diff}min"
                        elif diff > -8:
                            time_str = "AO VIVO"
                        else:
                            time_str = "encerr."
                    else:
                        kick_str = "?"
                        time_str = "?"
                else:
                    ph = pa = th = ta = "?"
                    kick_str = time_str = "?"

                # G1 score
                if g1:
                    g1_score = f"{g1.score_home or '?'}-{g1.score_away or '?'}"
                    # Gols do loser em G1
                    if g1.player_home == loser:
                        loser_g1 = g1.score_home or 0
                    else:
                        loser_g1 = g1.score_away or 0
                else:
                    g1_score = "?"
                    loser_g1 = "?"

                lines.append(
                    f"{kick_str} {time_str:>7s} | {loser:<12s} | G1: {g1_score} ({loser_g1}g)"
                )

            if not lines:
                await update.message.reply_text("Nenhum jogo ativo no momento.")
                return

            header = "Hora  Status  | Perdedor     | G1"
            sep = "-" * 44
            count = len(lines)

            msg = (
                f"<b>MONITORANDO ({count} jogos)</b>\n\n"
                f"<pre>{header}\n{sep}\n"
                + "\n".join(lines)
                + "</pre>"
            )

            await update.message.reply_text(msg, parse_mode="HTML")
        except Exception as e:
            logger.error(f"cmd_monitor error: {e}")
            await update.message.reply_text(f"Erro: {e}")

    async def cmd_blocked(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/blocked - Lista matchups (jogador, linha, oponente) bloqueados.

        v3 H2H granular (2026-04-29): cada (player, line, opponent) tem state
        machine independente. Read-only.
        """
        try:
            from src.db.database import async_session_factory
            from src.db.repositories import BlockedLineRepository
            from src.core.blocked_lines import get_status

            repo = BlockedLineRepository(async_session_factory)
            statuses = await get_status(repo)

            if not statuses:
                await update.message.reply_text(
                    "🟢 Nenhum matchup bloqueado no momento."
                )
                return

            line_label = {"over15": "O1.5", "over25": "O2.5",
                          "over35": "O3.5", "over45": "O4.5"}

            lines = ["🔒 <b>MATCHUPS BLOQUEADOS</b>", ""]
            for s in statuses:
                emoji = "⛔" if s["state"] == "PERMANENT" else "🔇"
                player = s["player"]
                line = line_label.get(s["line"], s["line"])
                opp = s["opponent"] or "?"
                pl = s["pl_total"]
                n = s["n_total"]
                strikes = s["block_count"]

                if s["state"] == "PERMANENT":
                    lines.append(
                        f"{emoji} <b>{player}</b> {line} vs {opp}  "
                        f"<i>(strike {strikes} — PERMANENTE)</i>"
                    )
                    lines.append(f"   PL = {pl:+.2f}u em {n} alertas")
                else:  # SHADOW
                    shadow_pl = s["shadow_pl"]
                    shadow_n = s["shadow_n"]
                    needed_pl = s["needed_pl"]
                    pl_ok = "✓" if shadow_pl >= needed_pl else "✗"
                    lines.append(
                        f"{emoji} <b>{player}</b> {line} vs {opp}  "
                        f"<i>(strike {strikes})</i>"
                    )
                    lines.append(f"   PL total = {pl:+.2f}u em {n} alertas")
                    lines.append(
                        f"   shadow = {shadow_pl:+.2f}u {pl_ok} / "
                        f"{shadow_n} alerts  "
                        f"(precisa ≥ +{needed_pl:.0f}u)"
                    )
                lines.append("")

            await update.message.reply_html("\n".join(lines))
        except Exception as e:
            logger.error(f"cmd_blocked error: {e}")
            await update.message.reply_text(f"Erro: {e}")

    async def cmd_relatorio(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/relatorio - Envia o relatorio de acompanhamento atualizado pro admin DM.

        Mesmo conteudo da automacao das 23:55 BRT, sob demanda. Apenas o admin
        pode invocar — outros usuarios recebem aviso silencioso.
        """
        from src.config import settings
        from src.db.database import async_session_factory
        from src.db.repositories import BlockedLineRepository
        from src.core.blocked_lines import recompute_all_states, build_hourly_report

        admin_id = settings.telegram_admin_chat_id
        invoker_chat = str(update.effective_chat.id) if update.effective_chat else ""

        if not admin_id or invoker_chat != str(admin_id):
            # Sem auth: ignora silenciosamente (nao expoe dados privados a outros)
            logger.info(
                f"/relatorio invocado por chat {invoker_chat} (nao admin) — ignorando"
            )
            return

        try:
            await update.message.reply_text("Gerando relatorio...")
            repo = BlockedLineRepository(async_session_factory)
            await recompute_all_states(repo)
            text = await build_hourly_report(repo)
            msg_id = await self.notifier.send_admin_message(text)
            if msg_id:
                logger.info(f"/relatorio enviado ao admin (msg_id={msg_id})")
            else:
                await update.message.reply_text(
                    "Falha ao enviar (TELEGRAM_ADMIN_CHAT_ID nao configurado?)"
                )
        except Exception as e:
            logger.error(f"cmd_relatorio error: {e}")
            await update.message.reply_text(f"Erro: {e}")

    def register_handlers(self, application: Application) -> None:
        """Register all command handlers with the bot application."""
        application.add_handler(CommandHandler("status", self.cmd_status))
        application.add_handler(CommandHandler("stats", self.cmd_stats))
        application.add_handler(CommandHandler("today", self.cmd_today))
        application.add_handler(CommandHandler("player", self.cmd_player))
        application.add_handler(CommandHandler("leagues", self.cmd_leagues))
        application.add_handler(CommandHandler("pause", self.cmd_pause))
        application.add_handler(CommandHandler("resume", self.cmd_resume))
        application.add_handler(CommandHandler("progress", self.cmd_progress))
        application.add_handler(CommandHandler("pnl", self.cmd_pnl))
        application.add_handler(CommandHandler("players", self.cmd_players))
        application.add_handler(CommandHandler("results", self.cmd_results))
        application.add_handler(CommandHandler("monitor", self.cmd_monitor))
        application.add_handler(CommandHandler("blocked", self.cmd_blocked))
        application.add_handler(CommandHandler("relatorio", self.cmd_relatorio))
        logger.info("Telegram command handlers registered")
