"""Auditoria semanal dos filtros manuais (blacklist condicional, tight AWAY, horários ruins).

Roda todo domingo às 10:00. Puxa alertas validados dos últimos 7 dias,
analisa a performance de cada filtro e jogador condicional, e envia um
relatório completo via Telegram com insights e recomendações.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone

from loguru import logger


class WeeklyFilterAudit:
    """Audita filtros manuais e envia relatório semanal com insights."""

    def __init__(self, alert_repo, match_repo, stats_engine, notifier) -> None:
        self.alerts = alert_repo
        self.matches = match_repo
        self.stats = stats_engine
        self.notifier = notifier

    async def run_and_notify(self) -> dict:
        """Executa auditoria completa e envia via Telegram."""
        try:
            report = await self._build_report()
            await self._send_telegram(report)
            logger.info("Weekly filter audit sent successfully")
            return report
        except Exception as e:
            logger.error(f"Weekly filter audit failed: {e}")
            return {"error": str(e)}

    async def _build_report(self) -> dict:
        """Constrói relatório completo analisando alertas dos últimos 7 dias."""
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)

        # Buscar alertas da semana com match data via JOIN
        try:
            from sqlalchemy import select
            from sqlalchemy.orm import selectinload
            from src.db.database import get_session
            from src.db.models import Alert

            async with get_session() as session:
                stmt = (
                    select(Alert)
                    .options(selectinload(Alert.match))
                    .where(Alert.validated_at.is_not(None))
                    .where(Alert.sent_at >= cutoff)
                    .order_by(Alert.sent_at.asc())
                )
                result = await session.execute(stmt)
                week_alerts = list(result.scalars().all())
        except Exception as e:
            logger.error(f"Could not fetch alerts for audit: {e}")
            return {"error": str(e)}

        # Build match cache from loaded relationships
        match_cache = {}
        for a in week_alerts:
            m = a.match
            if m and m.id not in match_cache:
                match_cache[m.id] = {
                    "player_home": m.player_home, "player_away": m.player_away,
                    "team_home": m.team_home, "team_away": m.team_away,
                    "score_home": m.score_home, "score_away": m.score_away,
                }

        report = {
            "period_start": cutoff,
            "period_end": datetime.now(timezone.utc),
            "total_alerts": len(week_alerts),
            "overall": self._calc_stats(week_alerts),
            "conditional_bl": {},
            "tight_away": {"passed": [], "blocked": []},
            "bad_hours": {"passed": [], "blocked": []},
            "filter_impact": {},
            "top_players": [],
            "worst_players": [],
            "insights": [],
        }

        # Classificar cada alerta
        for a in week_alerts:
            m = match_cache.get(a.match_id, {})
            is_home = a.losing_player == m.get("player_home")
            line = getattr(a, "best_line", "over25") or "over25"
            edge = getattr(a, "edge", 0) or 0
            loss_type = getattr(a, "loss_type", "") or ""
            hour = a.sent_at.hour if a.sent_at else 0

            # Tag como seria filtrado
            a._audit_home = is_home
            a._audit_match = m
            a._audit_line = line
            a._audit_edge = edge
            a._audit_loss_type = loss_type
            a._audit_hour = hour

        # === 1. Blacklist condicional — cada jogador ===
        cond_bl = self.stats.PLAYER_CONDITIONAL_BLACKLIST
        for player, rules in cond_bl.items():
            player_alerts = [a for a in week_alerts if a.losing_player == player]
            if not player_alerts:
                report["conditional_bl"][player] = {
                    "rules": rules, "total": 0,
                    "passed": self._calc_stats([]),
                    "blocked": self._calc_stats([]),
                    "all": self._calc_stats([]),
                }
                continue

            passed = []
            blocked = []
            for a in player_alerts:
                would_block = False
                if rules.get("block_home_g2") and a._audit_home:
                    would_block = True
                if rules.get("block_away_g2") and not a._audit_home:
                    would_block = True
                if rules.get("block_lines") and a._audit_line in rules["block_lines"]:
                    would_block = True
                if would_block:
                    blocked.append(a)
                else:
                    passed.append(a)

            report["conditional_bl"][player] = {
                "rules": rules,
                "total": len(player_alerts),
                "passed": self._calc_stats(passed),
                "blocked": self._calc_stats(blocked),
                "all": self._calc_stats(player_alerts),
            }

        # === 2. Tight AWAY ===
        for a in week_alerts:
            if not a._audit_home and a._audit_loss_type == "tight":
                report["tight_away"]["blocked"].append(a)
            elif not a._audit_home and a._audit_loss_type == "tight":
                pass  # ja adicionado
            # Alertas tight AWAY que teriam passado sem o filtro
        # Tambem coletar tight HOME para comparacao
        tight_home = [a for a in week_alerts if a._audit_home and a._audit_loss_type == "tight"]
        tight_away_all = [a for a in week_alerts
                          if not a._audit_home and a._audit_loss_type == "tight"]
        report["tight_away"] = {
            "blocked": self._calc_stats(tight_away_all),
            "tight_home_comparison": self._calc_stats(tight_home),
            "blocked_alerts": tight_away_all,
        }

        # === 3. Horários ruins ===
        bad_hours = self.stats.BAD_HOURS
        min_edge = self.stats.BAD_HOUR_MIN_EDGE
        bh_blocked = [a for a in week_alerts
                      if a._audit_hour in bad_hours and a._audit_edge < min_edge]
        bh_passed = [a for a in week_alerts
                     if a._audit_hour in bad_hours and a._audit_edge >= min_edge]
        bh_good_hours = [a for a in week_alerts if a._audit_hour not in bad_hours]
        report["bad_hours"] = {
            "blocked": self._calc_stats(bh_blocked),
            "passed_high_edge": self._calc_stats(bh_passed),
            "good_hours": self._calc_stats(bh_good_hours),
            "by_hour": {},
        }
        # Detalhamento por hora
        hour_groups = defaultdict(list)
        for a in week_alerts:
            hour_groups[a._audit_hour].append(a)
        for h in sorted(hour_groups.keys()):
            report["bad_hours"]["by_hour"][h] = self._calc_stats(hour_groups[h])

        # === 4. Top/worst jogadores da semana (todos, não só condicionais) ===
        player_groups = defaultdict(list)
        for a in week_alerts:
            player_groups[a.losing_player].append(a)

        player_stats = []
        for name, alerts in player_groups.items():
            s = self._calc_stats(alerts)
            s["name"] = name
            player_stats.append(s)

        player_stats.sort(key=lambda x: x["pl"], reverse=True)
        report["top_players"] = player_stats[:10]
        report["worst_players"] = player_stats[-5:]

        # === 5. Performance por time do perdedor ===
        team_groups = defaultdict(list)
        for a in week_alerts:
            m = a._audit_match
            team = m.get("team_home") if a._audit_home else m.get("team_away")
            if team:
                team_groups[team].append(a)
        team_stats = []
        for team, alerts in team_groups.items():
            if len(alerts) >= 3:
                s = self._calc_stats(alerts)
                s["name"] = team
                team_stats.append(s)
        team_stats.sort(key=lambda x: x["pl"], reverse=True)
        report["top_teams"] = team_stats[:5]
        report["worst_teams"] = team_stats[-5:] if len(team_stats) >= 5 else []

        # === 6. HOME vs AWAY geral ===
        home_alerts = [a for a in week_alerts if a._audit_home]
        away_alerts = [a for a in week_alerts if not a._audit_home]
        report["home_vs_away"] = {
            "home": self._calc_stats(home_alerts),
            "away": self._calc_stats(away_alerts),
        }

        # === 7. Por linha ===
        line_groups = defaultdict(list)
        for a in week_alerts:
            line_groups[a._audit_line].append(a)
        report["by_line"] = {}
        for line in ["over15", "over25", "over35", "over45"]:
            if line in line_groups:
                report["by_line"][line] = self._calc_stats(line_groups[line])

        # === 8. Gerar insights ===
        report["insights"] = self._generate_insights(report)

        return report

    def _calc_stats(self, alerts: list) -> dict:
        """Calcula stats básicas de uma lista de alertas."""
        n = len(alerts)
        if n == 0:
            return {"n": 0, "hits": 0, "wr": 0, "pl": 0, "roi": 0}

        hits = 0
        pl = 0.0
        for a in alerts:
            line = getattr(a, "best_line", "over25") or "over25"
            if line == "over15":
                odds = getattr(a, "over15_odds", 0) or 0
                hit = getattr(a, "over15_hit", False)
            elif line == "over35":
                odds = getattr(a, "over35_odds", 0) or 0
                hit = getattr(a, "over35_hit", False)
            elif line == "over45":
                odds = getattr(a, "over45_odds", 0) or 0
                hit = getattr(a, "over45_hit", False)
            else:
                odds = getattr(a, "over25_odds", 0) or 0
                hit = getattr(a, "over25_hit", False)

            if hit:
                hits += 1
                pl += (odds - 1.0)
            else:
                pl -= 1.0

        return {
            "n": n,
            "hits": hits,
            "wr": hits / n * 100,
            "pl": round(pl, 2),
            "roi": round(pl / n * 100, 1),
        }

    def _generate_insights(self, report: dict) -> list[str]:
        """Gera insights automáticos baseados nos dados."""
        insights = []

        # 1. Blacklist condicional — está funcionando?
        for player, data in report.get("conditional_bl", {}).items():
            if data["total"] == 0:
                insights.append(f"📭 {player}: nenhum alerta na semana (inativo)")
                continue

            blocked = data["blocked"]
            passed = data["passed"]

            if blocked["n"] > 0 and blocked["pl"] > 0:
                insights.append(
                    f"⚠️ {player}: bloqueio CUSTOU {blocked['pl']:+.2f}u esta semana "
                    f"({blocked['n']} tips bloqueados teriam dado green, "
                    f"WR={blocked['wr']:.0f}%). Considerar remover regra."
                )
            elif blocked["n"] > 0 and blocked["pl"] < 0:
                insights.append(
                    f"✅ {player}: bloqueio SALVOU {-blocked['pl']:+.2f}u "
                    f"({blocked['n']} tips ruins com WR={blocked['wr']:.0f}% evitados)"
                )
            if passed["n"] > 0:
                if passed["roi"] > 20:
                    insights.append(
                        f"🟢 {player} (permitido): ROI={passed['roi']:+.1f}%, "
                        f"P/L={passed['pl']:+.2f}u — jogador rendendo bem no contexto liberado"
                    )
                elif passed["roi"] < -20 and passed["n"] >= 3:
                    insights.append(
                        f"🔴 {player} (permitido): ROI={passed['roi']:+.1f}%, "
                        f"P/L={passed['pl']:+.2f}u — considerar expandir bloqueio"
                    )

        # 2. Tight AWAY
        ta = report.get("tight_away", {})
        ta_blocked = ta.get("blocked", {})
        ta_home = ta.get("tight_home_comparison", {})
        if ta_blocked.get("n", 0) > 0:
            if ta_blocked["pl"] < 0:
                insights.append(
                    f"✅ Filtro tight AWAY salvou {-ta_blocked['pl']:+.2f}u "
                    f"({ta_blocked['n']} tips, WR={ta_blocked['wr']:.0f}%)"
                )
            else:
                insights.append(
                    f"⚠️ Filtro tight AWAY custou {ta_blocked['pl']:+.2f}u esta semana "
                    f"({ta_blocked['n']} tips bloqueados teriam lucrado)"
                )
        if ta_home.get("n", 0) > 0:
            insights.append(
                f"ℹ️ Tight HOME (permitido): {ta_home['n']} tips, "
                f"WR={ta_home['wr']:.0f}%, P/L={ta_home['pl']:+.2f}u"
            )

        # 3. Horários ruins
        bh = report.get("bad_hours", {})
        bh_blocked = bh.get("blocked", {})
        bh_passed = bh.get("passed_high_edge", {})
        if bh_blocked.get("n", 0) > 0:
            if bh_blocked["pl"] < 0:
                insights.append(
                    f"✅ Filtro horário ruim salvou {-bh_blocked['pl']:+.2f}u "
                    f"({bh_blocked['n']} tips com edge baixo)"
                )
            else:
                insights.append(
                    f"⚠️ Filtro horário ruim custou {bh_blocked['pl']:+.2f}u "
                    f"({bh_blocked['n']} tips bloqueados teriam lucrado)"
                )
        if bh_passed.get("n", 0) > 0:
            insights.append(
                f"ℹ️ Horário ruim + edge alto (permitido): {bh_passed['n']} tips, "
                f"WR={bh_passed['wr']:.0f}%, ROI={bh_passed['roi']:+.1f}%"
            )

        # 4. HOME vs AWAY
        hva = report.get("home_vs_away", {})
        home = hva.get("home", {})
        away = hva.get("away", {})
        if home.get("n", 0) > 0 and away.get("n", 0) > 0:
            if home["roi"] > away["roi"] + 15:
                insights.append(
                    f"📊 HOME ({home['roi']:+.1f}%) muito melhor que "
                    f"AWAY ({away['roi']:+.1f}%) esta semana"
                )
            elif away["roi"] > home["roi"] + 15:
                insights.append(
                    f"📊 AWAY ({away['roi']:+.1f}%) superou "
                    f"HOME ({home['roi']:+.1f}%) esta semana"
                )

        # 5. Melhores/piores jogadores
        top = report.get("top_players", [])
        if top and top[0].get("pl", 0) > 3:
            insights.append(
                f"🏆 Melhor jogador: {top[0]['name']} "
                f"({top[0]['n']} tips, P/L={top[0]['pl']:+.2f}u, ROI={top[0]['roi']:+.1f}%)"
            )
        worst = report.get("worst_players", [])
        if worst and worst[-1].get("pl", 0) < -3:
            w = worst[-1]
            in_bl = w["name"] in self.stats.PLAYER_BLACKLIST
            in_cond = w["name"] in self.stats.PLAYER_CONDITIONAL_BLACKLIST
            tag = ""
            if not in_bl and not in_cond:
                tag = " ← CONSIDERAR BLACKLIST"
            insights.append(
                f"💀 Pior jogador: {w['name']} "
                f"({w['n']} tips, P/L={w['pl']:+.2f}u, ROI={w['roi']:+.1f}%){tag}"
            )

        # 6. Linhas
        lines = report.get("by_line", {})
        best_line = max(lines.items(), key=lambda x: x[1].get("roi", 0), default=None)
        worst_line = min(lines.items(), key=lambda x: x[1].get("roi", 0), default=None)
        if best_line and worst_line and best_line[0] != worst_line[0]:
            bl, bs = best_line
            wl, ws = worst_line
            insights.append(
                f"📈 Melhor linha: {bl} (ROI={bs['roi']:+.1f}%) | "
                f"Pior: {wl} (ROI={ws['roi']:+.1f}%)"
            )

        # 7. Times destaque
        top_teams = report.get("top_teams", [])
        if top_teams and top_teams[0].get("pl", 0) > 2:
            t = top_teams[0]
            insights.append(
                f"⚽ Melhor time (perdedor): {t['name']} "
                f"({t['n']} tips, P/L={t['pl']:+.2f}u)"
            )
        worst_teams = report.get("worst_teams", [])
        if worst_teams and worst_teams[-1].get("pl", 0) < -2:
            t = worst_teams[-1]
            insights.append(
                f"🚫 Pior time (perdedor): {t['name']} "
                f"({t['n']} tips, P/L={t['pl']:+.2f}u)"
            )

        return insights

    async def _send_telegram(self, report: dict) -> None:
        """Envia relatório formatado via Telegram."""
        overall = report["overall"]
        period_start = report["period_start"].strftime("%d/%m")
        period_end = report["period_end"].strftime("%d/%m")

        lines = [
            f"🔍 <b>AUDITORIA SEMANAL DE FILTROS</b>",
            f"📅 {period_start} → {period_end} | {overall['n']} alertas validados",
            f"📊 WR={overall['wr']:.1f}% | P/L={overall['pl']:+.2f}u | ROI={overall['roi']:+.1f}%",
            "",
        ]

        # === Blacklist condicional ===
        lines.append("━━━ <b>BLACKLIST CONDICIONAL</b> ━━━")
        cond_bl = report.get("conditional_bl", {})
        for player, data in cond_bl.items():
            if data["total"] == 0:
                lines.append(f"  {player}: sem alertas (inativo)")
                continue

            blocked = data["blocked"]
            passed = data["passed"]
            rules = data["rules"]

            # Descrever regra
            rule_parts = []
            if rules.get("block_home_g2"):
                rule_parts.append("HOME")
            if rules.get("block_away_g2"):
                rule_parts.append("AWAY")
            if rules.get("block_lines"):
                rule_parts.append("/".join(sorted(rules["block_lines"])))
            rule_str = " + ".join(rule_parts)

            line = f"  <b>{player}</b> [bloqueia: {rule_str}]"
            if blocked["n"] > 0:
                icon = "✅" if blocked["pl"] < 0 else "⚠️"
                line += f"\n    {icon} Bloqueado: {blocked['n']}x WR={blocked['wr']:.0f}% P/L={blocked['pl']:+.2f}u"
            if passed["n"] > 0:
                icon = "🟢" if passed["pl"] > 0 else "🔴"
                line += f"\n    {icon} Permitido: {passed['n']}x WR={passed['wr']:.0f}% P/L={passed['pl']:+.2f}u"
            lines.append(line)
        lines.append("")

        # === Tight AWAY ===
        lines.append("━━━ <b>TIGHT AWAY</b> ━━━")
        ta = report.get("tight_away", {})
        ta_blocked = ta.get("blocked", {})
        ta_home = ta.get("tight_home_comparison", {})
        if ta_blocked.get("n", 0) > 0:
            icon = "✅" if ta_blocked["pl"] < 0 else "⚠️"
            lines.append(
                f"  {icon} Bloqueado: {ta_blocked['n']}x WR={ta_blocked['wr']:.0f}% "
                f"P/L={ta_blocked['pl']:+.2f}u"
            )
        else:
            lines.append("  Nenhum tight AWAY na semana")
        if ta_home.get("n", 0) > 0:
            lines.append(
                f"  ℹ️ Tight HOME: {ta_home['n']}x WR={ta_home['wr']:.0f}% "
                f"P/L={ta_home['pl']:+.2f}u"
            )
        lines.append("")

        # === Horários ruins ===
        lines.append("━━━ <b>HORÁRIOS RUINS</b> ━━━")
        bh = report.get("bad_hours", {})
        bad_h_str = ", ".join(f"{h:02d}h" for h in sorted(self.stats.BAD_HOURS))
        lines.append(f"  Horários: {bad_h_str} (edge < {self.stats.BAD_HOUR_MIN_EDGE:.0%})")
        bh_blocked = bh.get("blocked", {})
        bh_passed = bh.get("passed_high_edge", {})
        if bh_blocked.get("n", 0) > 0:
            icon = "✅" if bh_blocked["pl"] < 0 else "⚠️"
            lines.append(
                f"  {icon} Bloqueado: {bh_blocked['n']}x WR={bh_blocked['wr']:.0f}% "
                f"P/L={bh_blocked['pl']:+.2f}u"
            )
        if bh_passed.get("n", 0) > 0:
            lines.append(
                f"  ℹ️ Passou (edge alto): {bh_passed['n']}x WR={bh_passed['wr']:.0f}% "
                f"P/L={bh_passed['pl']:+.2f}u"
            )
        # Detalhe por hora
        by_hour = bh.get("by_hour", {})
        if by_hour:
            hour_lines = []
            for h in sorted(by_hour.keys()):
                s = by_hour[h]
                if s["n"] > 0:
                    hour_lines.append(f"{h:02d}h:{s['n']}({s['pl']:+.1f}u)")
            if hour_lines:
                lines.append(f"  📋 {' | '.join(hour_lines)}")
        lines.append("")

        # === HOME vs AWAY ===
        lines.append("━━━ <b>HOME vs AWAY</b> ━━━")
        hva = report.get("home_vs_away", {})
        for side in ["home", "away"]:
            s = hva.get(side, {})
            if s.get("n", 0) > 0:
                lines.append(
                    f"  {side.upper()}: {s['n']}x WR={s['wr']:.0f}% "
                    f"P/L={s['pl']:+.2f}u ROI={s['roi']:+.1f}%"
                )
        lines.append("")

        # === Por linha ===
        lines.append("━━━ <b>POR LINHA</b> ━━━")
        by_line = report.get("by_line", {})
        for bl in ["over15", "over25", "over35", "over45"]:
            s = by_line.get(bl, {})
            if s.get("n", 0) > 0:
                label = bl.replace("over", "O")
                lines.append(
                    f"  {label}: {s['n']}x WR={s['wr']:.0f}% "
                    f"P/L={s['pl']:+.2f}u ROI={s['roi']:+.1f}%"
                )
        lines.append("")

        # === Top jogadores ===
        lines.append("━━━ <b>TOP 5 JOGADORES</b> ━━━")
        for p in report.get("top_players", [])[:5]:
            lines.append(
                f"  🟢 {p['name']}: {p['n']}x WR={p['wr']:.0f}% P/L={p['pl']:+.2f}u"
            )
        lines.append("")
        lines.append("━━━ <b>PIORES JOGADORES</b> ━━━")
        for p in report.get("worst_players", [])[-3:]:
            in_bl = p["name"] in self.stats.PLAYER_BLACKLIST
            in_cond = p["name"] in self.stats.PLAYER_CONDITIONAL_BLACKLIST
            tag = ""
            if not in_bl and not in_cond and p["pl"] < -2:
                tag = " ⚠️"
            lines.append(
                f"  🔴 {p['name']}: {p['n']}x WR={p['wr']:.0f}% P/L={p['pl']:+.2f}u{tag}"
            )
        lines.append("")

        # === Top times ===
        top_teams = report.get("top_teams", [])
        worst_teams = report.get("worst_teams", [])
        if top_teams:
            lines.append("━━━ <b>TIMES DESTAQUE</b> ━━━")
            for t in top_teams[:3]:
                lines.append(f"  ⚽ {t['name']}: {t['n']}x P/L={t['pl']:+.2f}u")
            if worst_teams:
                for t in worst_teams[-2:]:
                    if t["pl"] < 0:
                        lines.append(f"  🚫 {t['name']}: {t['n']}x P/L={t['pl']:+.2f}u")
            lines.append("")

        # === Insights ===
        insights = report.get("insights", [])
        if insights:
            lines.append("━━━ <b>INSIGHTS & RECOMENDAÇÕES</b> ━━━")
            for insight in insights:
                lines.append(f"  {insight}")
            lines.append("")

        # === Legenda ===
        lines.append("<i>✅=filtro salvou $ | ⚠️=filtro custou $ | 🟢=lucro | 🔴=prejuízo</i>")

        text = "\n".join(lines)

        # Telegram tem limite de 4096 chars — dividir se necessário
        if len(text) <= 4096:
            await self.notifier.send_message(text)
        else:
            # Dividir em partes
            parts = self._split_message(text, 4000)
            for i, part in enumerate(parts):
                if i > 0:
                    part = f"🔍 <b>AUDITORIA (cont. {i+1})</b>\n\n" + part
                await self.notifier.send_message(part)

    def _split_message(self, text: str, max_len: int) -> list[str]:
        """Divide mensagem em partes respeitando quebras de linha."""
        parts = []
        current = ""
        for line in text.split("\n"):
            if len(current) + len(line) + 1 > max_len:
                parts.append(current)
                current = line
            else:
                current = current + "\n" + line if current else line
        if current:
            parts.append(current)
        return parts
