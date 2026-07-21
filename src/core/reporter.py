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

from src.core.cancelled_alerts import CANCELLED_ALERT_IDS


class Reporter:
    """Generates and sends scheduled performance reports."""

    def __init__(
        self, alert_repo, player_repo, method_stats_repo, notifier,
        session_factory=None, alert_v2_repo=None, alert_v3_repo=None,
        alert_free_repo=None,
    ) -> None:
        self.alerts = alert_repo
        self.players = player_repo
        self.method_stats = method_stats_repo
        self.notifier = notifier
        self._sf = session_factory
        self.alerts_v2 = alert_v2_repo
        self.alerts_v3 = alert_v3_repo
        self.alert_free_repo = alert_free_repo

    async def send_daily_report(self) -> None:
        """Build and send the daily results in /results format."""
        from src.config import settings

        try:
            from zoneinfo import ZoneInfo
            tz_local = ZoneInfo(settings.timezone)
        except Exception:
            tz_local = timezone(timedelta(hours=-3))

        now_local = datetime.now(tz_local)
        target_date = now_local.replace(hour=0, minute=0, second=0, microsecond=0)

        # Skip pontual: pular envio em datas especificas (pedido do owner).
        # 2026-04-26: nao enviar /results hoje.
        SKIP_DATES = {"2026-04-26"}
        if target_date.strftime("%Y-%m-%d") in SKIP_DATES:
            logger.info(
                f"send_daily_report SKIP: data {target_date.strftime('%Y-%m-%d')} "
                f"esta em SKIP_DATES — nao enviando ao grupo"
            )
            return

        start_utc = target_date.astimezone(timezone.utc).replace(tzinfo=None)
        end_utc = (target_date + timedelta(days=1)).astimezone(timezone.utc).replace(tzinfo=None)

        date_label = target_date.strftime("%d/%m/%Y")
        alerts = await self.alerts.get_results_by_date(start_utc, end_utc)

        # Filtrar suprimidos (auto-block) — eles NUNCA foram pra grupo nenhum.
        alerts = [a for a in alerts if not getattr(a, "suppressed", False)]

        if not alerts:
            await self.notifier.send_message(
                f"📋 <b>RESULTADOS {date_label}</b>\n\nSem alertas hoje."
            )
            return

        # ----- Construir mensagem do VIP (universo completo, marcando FREE) -----
        vip_msg = self._build_results_msg(
            alerts, date_label, tz_local, scope="vip"
        )

        await self.notifier.send_message(vip_msg["text"])
        logger.info(
            f"Daily results VIP sent: {vip_msg['greens']}G {vip_msg['losses']}R, "
            f"P/L {vip_msg['profit']:+.2f}u"
        )

        # ----- Imagem de resultado + menções enviadas ao grupo FREE -----
        if settings.telegram_free_group_id:
            free_group_id = (
                getattr(self.notifier, "_free_group_id", None)
                or settings.telegram_free_group_id
            )
            try:
                from src.core.daily_image import generate_daily_image
                import io as _io
                from telegram.constants import ParseMode

                img_bytes = await generate_daily_image(
                    date_label=date_label,
                    pl=vip_msg["profit"],
                    alertas=vip_msg["total"],
                    greens=vip_msg["greens"],
                    reds=vip_msg["losses"],
                    roi=vip_msg["roi"],
                )

                # Busca membros e separa em caption + overflow
                caption, overflow_batches = await self._build_mentions_split()

                photo_msg = await self.notifier.bot.send_photo(
                    chat_id=free_group_id,
                    photo=_io.BytesIO(img_bytes),
                    caption=caption or None,
                    parse_mode=ParseMode.HTML if caption else None,
                )
                logger.info(f"Daily image sent to FREE group (caption={len(caption)} chars, overflow={len(overflow_batches)} batches)")

                # Envia overflow como reply da foto para manter tudo agrupado
                for batch_text in overflow_batches:
                    try:
                        await self.notifier.bot.send_message(
                            chat_id=free_group_id,
                            text=batch_text,
                            parse_mode=ParseMode.HTML,
                            reply_to_message_id=photo_msg.message_id,
                            disable_web_page_preview=True,
                        )
                    except Exception as e:
                        logger.warning(f"Daily image overflow mention error: {e}")
            except Exception as e:
                logger.warning(f"Failed to send daily image to FREE group: {e}")

        # ----- Mandar tambem versao FREE (texto) no grupo FREE (subset) -----
        free_alerts = [a for a in alerts if getattr(a, "free_message_id", None)]
        if free_alerts and settings.telegram_free_group_id:
            free_msg = self._build_results_msg(
                free_alerts, date_label, tz_local, scope="free"
            )
            free_group_id = getattr(self.notifier, "_free_group_id", None) or settings.telegram_free_group_id
            try:
                await self.notifier.bot.send_message(
                    chat_id=free_group_id,
                    text=free_msg["text"],
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                )
                logger.info(
                    f"Daily results FREE sent: {free_msg['greens']}G {free_msg['losses']}R, "
                    f"P/L {free_msg['profit']:+.2f}u"
                )
            except Exception as e:
                logger.warning(f"Failed to send FREE daily report: {e}")

    async def _build_mentions_split(self) -> tuple[str, list[str]]:
        """Retorna (caption, overflow_batches) com todas as menções do grupo free.

        caption: primeira fatia que cabe em 1024 chars (limite Telegram caption).
        overflow_batches: lista de strings de menção (cada uma <= 4096 chars)
                          para enviar como reply da foto.
        """
        if not self._sf:
            return "", []
        from sqlalchemy import select
        from src.db.models import FreeGroupMember

        try:
            async with self._sf() as session:
                result = await session.execute(select(FreeGroupMember))
                members = result.scalars().all()
        except Exception as e:
            logger.warning(f"_build_mentions_split DB error: {e}")
            return "", []

        if not members:
            return "", []

        tags = [
            f'<a href="tg://user?id={m.user_id}">{m.first_name or "user"}</a>'
            for m in members
        ]

        CAPTION_LIMIT = 1024
        MSG_LIMIT = 4096

        # Preenche caption com o máximo que cabe
        caption_parts: list[str] = []
        used = 0
        remaining_tags = list(tags)
        for tag in tags:
            needed = len(tag) + (1 if caption_parts else 0)
            if used + needed > CAPTION_LIMIT:
                break
            caption_parts.append(tag)
            used += needed
            remaining_tags = remaining_tags[1:]

        caption = " ".join(caption_parts)

        # Agrupa o restante em batches de até 4096 chars
        overflow_batches: list[str] = []
        current_parts: list[str] = []
        current_len = 0
        for tag in remaining_tags:
            needed = len(tag) + (1 if current_parts else 0)
            if current_len + needed > MSG_LIMIT:
                overflow_batches.append(" ".join(current_parts))
                current_parts = [tag]
                current_len = len(tag)
            else:
                current_parts.append(tag)
                current_len += needed
        if current_parts:
            overflow_batches.append(" ".join(current_parts))

        return caption, overflow_batches

    async def send_daily_report_v2(self) -> None:
        """Envia /results do M2 no grupo M2 — formato texto, sem imagem."""
        from src.config import settings

        if self.alerts_v2 is None:
            logger.warning("send_daily_report_v2 SKIP: alert_v2_repo nao configurado")
            return

        v2_group_id = (
            getattr(self.notifier, "_v2_group_id", None)
            or getattr(settings, "telegram_group_v2_id", "")
        )
        if not v2_group_id:
            logger.info("send_daily_report_v2 SKIP: TELEGRAM_GROUP_V2_ID vazio")
            return

        try:
            from zoneinfo import ZoneInfo
            tz_local = ZoneInfo(settings.timezone)
        except Exception:
            tz_local = timezone(timedelta(hours=-3))

        now_local = datetime.now(tz_local)
        target_date = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        start_utc = target_date.astimezone(timezone.utc).replace(tzinfo=None)
        end_utc = (target_date + timedelta(days=1)).astimezone(timezone.utc).replace(tzinfo=None)
        date_label = target_date.strftime("%d/%m/%Y")

        alerts = await self.alerts_v2.get_results_by_date(start_utc, end_utc)
        # Suprimidos (auto-block) nao foram enviados — fora do report
        alerts = [a for a in alerts if not getattr(a, "suppressed", False)]

        if not alerts:
            try:
                await self.notifier.bot.send_message(
                    chat_id=v2_group_id,
                    text=f"📋 <b>M2 RESULTADOS {date_label}</b>\n\nSem alertas hoje.",
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.warning(f"Failed to send empty M2 report: {e}")
            return

        msg = self._build_results_msg_v2(alerts, date_label, tz_local)
        try:
            await self.notifier.bot.send_message(
                chat_id=v2_group_id,
                text=msg["text"],
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            logger.info(
                f"Daily M2 results sent: {msg['greens']}G {msg['losses']}R, "
                f"PL {msg['profit']:+.2f}u"
            )
        except Exception as e:
            logger.warning(f"Failed to send M2 daily report: {e}")

    async def send_daily_report_v3(self) -> None:
        """Resumo diario do M3 (greens/reds/profit) no privado do owner.

        NO-OP se alert_v3_repo nao configurado (M3 desligado).
        """
        if self.alerts_v3 is None:
            logger.info("send_daily_report_v3 SKIP: alert_v3_repo nao configurado")
            return

        since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=24)
        alerts = await self.alerts_v3.get_validated_since(since)
        if not alerts:
            logger.info("send_daily_report_v3 SKIP: sem alertas validados nas ultimas 24h")
            return

        greens = [a for a in alerts if a.hit]
        reds = [a for a in alerts if a.hit is False]
        profit = sum(a.profit_flat or 0.0 for a in alerts)
        emoji = "🟢" if profit >= 0 else "🔴"
        text = (
            f"📊 <b>[M3] Diário</b>\n"
            f"✅ {len(greens)} GREEN | ❌ {len(reds)} RED\n"
            f"{emoji} P/L flat: {profit:+.2f}u ({len(alerts)} apostas)"
        )
        await self.notifier.send_message_v3_raw(text)
        logger.info(
            f"M3 daily report sent: {len(greens)}G {len(reds)}R, P/L {profit:+.2f}u"
        )

    async def send_daily_report_free(self) -> None:
        """Resumo diario do Modelo FREE no grupo gratis (tips + saldo total).

        NO-OP se alert_free_repo nao configurado ou sem alertas
        greens/reds validados nas ultimas 24h (void nao conta pro saldo).
        Copy publica: nunca revela o metodo interno.
        """
        if not self.alert_free_repo:
            logger.info("send_daily_report_free SKIP: alert_free_repo nao configurado")
            return

        from src.core.free_status import LINE_LABELS

        since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=24)
        alerts = await self.alert_free_repo.get_validated_since(since)

        counted = [a for a in alerts if a.status in ("green", "red")]
        if not counted:
            logger.info("send_daily_report_free SKIP: sem tips validadas nas ultimas 24h")
            return

        greens = [a for a in counted if a.status == "green"]
        reds = [a for a in counted if a.status == "red"]
        voids = [a for a in alerts if a.status == "void"]
        pnl = sum((a.entry_odd - 1.0) for a in greens) - len(reds)
        emoji = "🟢" if pnl >= 0 else "🔴"

        linhas = "\n".join(
            f"{'✅' if a.status == 'green' else '❌'} {a.losing_player} "
            f"{LINE_LABELS[a.line]} @ {a.entry_odd:.2f}"
            for a in counted
        )

        text = (
            f"📊 <b>RESULTADO DO DIA — FIFA eSports</b>\n\n"
            f"{linhas}\n\n"
            f"✅ {len(greens)} GREEN | ❌ {len(reds)} RED"
            + (f" | ⚪ {len(voids)} anuladas" if voids else "")
            + f"\n{emoji} <b>Saldo: {pnl:+.2f}u</b>  ({len(counted)} entradas)"
        )
        await self.notifier.send_free_raw(text)
        logger.info(
            f"FREE daily report sent: {len(greens)}G {len(reds)}R, saldo {pnl:+.2f}u"
        )

    def _build_results_msg_v2(self, alerts, date_label, tz_local) -> dict:
        """Monta mensagem /results pro M2 — formato igual ao M1, com camada (C1b/C2)."""
        from datetime import timezone as _tz

        lines = []
        total_profit = 0.0
        greens = 0
        total_validated = 0

        for a in alerts:
            sent_utc = a.sent_at
            if sent_utc:
                sent_local = sent_utc.replace(tzinfo=_tz.utc).astimezone(tz_local)
                hora = sent_local.strftime("%H:%M")
            else:
                hora = "??:??"

            player = a.losing_player or "?"
            bl = a.best_line or "over25"
            mercado_map = {"over15": "O1.5", "over25": "O2.5", "over35": "O3.5", "over45": "O4.5"}
            mercado = mercado_map.get(bl, bl)

            odds = getattr(a, f"{bl}_odds", None) or a.over25_odds
            odds_str = f"@{odds:.2f}" if odds else "—"

            camada = a.camada or "—"

            gols = a.actual_goals
            if gols is not None and a.hit is not None:
                total_validated += 1
                hit = bool(a.hit)
                resultado = "🟢" if hit else "🔴"
                if a.profit_flat is not None:
                    total_profit += float(a.profit_flat)
                elif odds:
                    p = (odds - 1.0) if hit else -1.0
                    total_profit += p
                if hit:
                    greens += 1
            else:
                gols = "—"
                resultado = "⏳"

            lines.append(
                f"{hora} | {player:<12} | {mercado} | {odds_str:<6} | {camada:<3} | {gols} | {resultado}"
            )

        header = "Hora  | Jogador      | Linha | Odds   | Cam | G | R"
        sep = "—" * 52

        total = len(alerts)
        losses = total_validated - greens
        roi = (total_profit / total_validated * 100) if total_validated > 0 else 0
        roi_emoji = "📈" if total_profit >= 0 else "📉"

        summary = (
            f"\n{sep}\n"
            f"<b>M2:</b> ✅ {greens}  ❌ {losses}  |  "
            f"Net: <b>{total_profit:+.2f}u</b>  |  "
            f"ROI: <b>{roi:+.1f}%</b> {roi_emoji}"
        )
        if total > total_validated:
            summary += f"\n⏳ {total - total_validated} aguardando resultado"

        text = (
            f"📋 <b>M2 RESULTADOS {date_label}</b>\n\n"
            f"<pre>{header}\n{sep}\n"
            + "\n".join(lines)
            + f"</pre>{summary}"
        )
        return {
            "text": text,
            "greens": greens,
            "losses": losses,
            "profit": total_profit,
        }

    def _build_results_msg(self, alerts, date_label, tz_local, scope: str) -> dict:
        """Monta mensagem /results.

        scope='vip': universo total, marca alertas FREE com 🆓, sumario VIP+FREE.
        scope='free': universo ja pre-filtrado, sumario unico FREE.
        """
        from datetime import timezone as _tz

        lines = []
        total_profit = 0.0
        greens = 0
        total_validated = 0
        free_profit = 0.0
        free_greens = 0
        free_validated = 0
        free_total = 0
        cancelled_count_local = 0

        for a in alerts:
            sent_utc = a.sent_at
            if sent_utc:
                sent_local = sent_utc.replace(tzinfo=_tz.utc).astimezone(tz_local)
                hora = sent_local.strftime("%H:%M")
            else:
                hora = "??:??"

            player = a.losing_player or "?"
            bl = a.best_line or "over25"

            mercado_map = {"over15": "O1.5", "over25": "O2.5", "over35": "O3.5", "over45": "O4.5"}
            mercado = mercado_map.get(bl, bl)

            odds = getattr(a, f"{bl}_odds", None) or a.over25_odds
            odds_str = f"@{odds:.2f}" if odds else "—"

            is_free_alert = bool(getattr(a, "free_message_id", None))
            if is_free_alert and scope == "vip":
                free_total += 1

            # Cancelado pelo owner: nao conta em PL/greens/validated
            is_cancelled = a.id in CANCELLED_ALERT_IDS
            if is_cancelled:
                cancelled_count_local += 1

            gols = a.actual_goals
            if is_cancelled:
                gols = a.actual_goals if a.actual_goals is not None else "—"
                resultado = "⚪"
            elif gols is not None:
                total_validated += 1
                thresholds = {"over15": 1, "over25": 2, "over35": 3, "over45": 4}
                hit = gols > thresholds.get(bl, 2)

                resultado = "🟢" if hit else "🔴"
                if odds:
                    p = (odds - 1.0) if hit else -1.0
                    total_profit += p
                    if is_free_alert and scope == "vip":
                        free_profit += p
                if hit:
                    greens += 1
                    if is_free_alert and scope == "vip":
                        free_greens += 1
                if is_free_alert and scope == "vip":
                    free_validated += 1
            else:
                gols = "—"
                resultado = "⏳"

            free_tag = " 🆓" if (is_free_alert and scope == "vip") else ""
            lines.append(
                f"{hora} | {player:<12} | {mercado} | {odds_str:<6} | {gols} | {resultado}{free_tag}"
            )

        header = "Hora  | Jogador      | Linha | Odds   | G | R"
        sep = "—" * 48

        total = len(alerts) - cancelled_count_local
        losses = total_validated - greens
        roi = (total_profit / total_validated * 100) if total_validated > 0 else 0
        roi_emoji = "📈" if total_profit >= 0 else "📉"

        if scope == "free":
            summary = (
                f"\n{sep}\n"
                f"<b>FREE:</b> ✅ {greens}  ❌ {losses}  |  "
                f"Net: <b>{total_profit:+.2f}u</b>  |  "
                f"ROI: <b>{roi:+.1f}%</b> {roi_emoji}"
            )
            if total > total_validated:
                summary += f"\n⏳ {total - total_validated} aguardando resultado"
            prefix = "FREE "
        else:
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
            prefix = ""

        text = (
            f"📋 <b>{prefix}RESULTADOS {date_label}</b>\n\n"
            f"<pre>{header}\n{sep}\n"
            + "\n".join(lines)
            + f"</pre>{summary}"
        )
        return {
            "text": text,
            "greens": greens,
            "losses": losses,
            "profit": total_profit,
            "total": total,
            "roi": roi,
        }

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
        await self.notifier.send_admin_message(text)
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
