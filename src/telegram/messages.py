"""Message template functions for Telegram (HTML format)."""

from __future__ import annotations

import html
from datetime import datetime


def _esc(text: str | None) -> str:
    """Escape HTML special chars in user-supplied strings."""
    return html.escape(str(text or ""))


def _stars(n: int) -> str:
    return "⭐" * n + "☆" * (5 - n)


def _level_emoji(level: str) -> str:
    return {"green": "🟢", "yellow": "🟡", "red_special": "🔴"}.get(level, "⚪")


def _pct(value: float | None, decimals: int = 1) -> str:
    if value is None:
        return "N/A"
    return f"{value * 100:.{decimals}f}%"


def _odds(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"@{value:.2f}"


def format_alert(d: dict) -> str:
    """Format a betting opportunity alert message — clean version."""
    stars = d.get("star_rating", 0)
    level = d.get("alert_level", "yellow")
    level_emoji = _level_emoji(level)

    # Kickoff em BRT
    kickoff_str = ""
    kickoff = d.get("kickoff_time")
    if isinstance(kickoff, datetime):
        from zoneinfo import ZoneInfo
        kickoff_brt = kickoff.astimezone(ZoneInfo("America/Sao_Paulo"))
        kickoff_str = kickoff_brt.strftime("%H:%M")
    elif kickoff:
        kickoff_str = str(kickoff)
    minutes_left = d.get("minutes_to_kickoff", "?")

    # Best line
    alert_label = d.get("alert_label", "Over 2.5")
    alert_odds = d.get("alert_odds") or d.get("over25_odds")

    # Winrate
    winrate = d.get("true_prob")
    winrate_str = _pct(winrate) if winrate else "N/A"

    # Multi-linha: mostrar TODAS as linhas com edge
    all_lines = d.get("all_lines", [])
    if len(all_lines) > 1:
        lines_block = "\n".join(
            f"  {l['label']} {_odds(l['odds'])} — {_pct(l['true_prob'])} (edge {l['edge']:.0%}, EV {l['ev']:.0%})"
            for l in sorted(all_lines, key=lambda x: x.get("ev", 0), reverse=True)
        )
        bet_section = f"<b>Apostas com edge:</b>\n{lines_block}"
    else:
        bet_type = "Aposta" if "Vitória" not in str(alert_label) else "Aposta ML"
        bet_section = f"<b>{bet_type}:</b> {_esc(alert_label)} {_odds(alert_odds)}\nWinrate: {winrate_str}"

    return (
        f"{_stars(stars)} {level_emoji}\n"
        f"\n"
        f"<b>Jogo anterior:</b>\n"
        f"{_esc(d.get('game1_player_home'))} ({_esc(d.get('game1_team_home') or d.get('return_team_home'))}) "
        f"{d.get('game1_score_home', '?')}-{d.get('game1_score_away', '?')} "
        f"{_esc(d.get('game1_player_away'))} ({_esc(d.get('game1_team_away') or d.get('return_team_away'))})\n"
        f"\n"
        f"{bet_section}\n"
        f"\n"
        f"<b>Jogo:</b> {_esc(d.get('return_player_home'))} vs {_esc(d.get('return_player_away'))}\n"
        f"Kickoff: {kickoff_str} (em {minutes_left} min)\n"
        f"\n"
        f"\U0001f517 <a href=\"{d.get('bet365_url', 'https://www.bet365.bet.br/#/IP/B151/')}\">bet365</a>"
    )


def format_validation(d: dict) -> str:
    """Format post-game validation result."""
    actual_goals = d.get("actual_goals", 0)
    over25_hit = d.get("over25_hit", False)
    over35_hit = d.get("over35_hit", False)

    o25 = "✅ BATEU" if over25_hit else "❌ NÃO bateu"
    o35 = "✅ BATEU" if over35_hit else "❌ NÃO bateu"

    new_rate = _pct(d.get("player_new_hit_rate"))
    new_n = d.get("player_new_sample", 0)

    score_home = d.get("score_home", "?")
    score_away = d.get("score_away", "?")
    player_home = _esc(d.get("player_home", ""))
    player_away = _esc(d.get("player_away", ""))

    return (
        f"📋 <b>RESULTADO - Battle 8 min</b>\n"
        f"\n"
        f"Jogo de Volta: {player_home} {score_home} - {score_away} {player_away}\n"
        f"Jogador Alvo: <b>{_esc(d.get('losing_player'))}</b> (era perdedor)\n"
        f"Gols feitos: <b>{actual_goals}</b>\n"
        f"\n"
        f"{o25} Over 2.5 ({actual_goals} gols) — odd era {_odds(d.get('over25_odds'))}\n"
        f"{o35} Over 3.5 ({actual_goals} gols) — odd era {_odds(d.get('over35_odds'))}\n"
        f"\n"
        f"📊 <b>Stats atualizadas de {_esc(d.get('losing_player'))}:</b>\n"
        f"   Após derrota: {new_rate} over 2.5 (n={new_n})"
    )


def format_daily_report(d: dict) -> str:
    """Format daily performance summary report."""
    date_str = d.get("date", datetime.now().strftime("%Y-%m-%d"))
    total = d.get("total_alerts", 0)
    over25_hits = d.get("over25_hits", 0)
    over35_hits = d.get("over35_hits", 0)
    over25_rate = (over25_hits / total * 100) if total > 0 else 0
    over35_rate = (over35_hits / total * 100) if total > 0 else 0
    roi = d.get("roi_flat", 0)
    roi_str = f"+{roi:.1f}%" if roi >= 0 else f"{roi:.1f}%"

    best = d.get("best_result", {})
    worst = d.get("worst_result", {})
    monthly_rate = _pct(d.get("monthly_hit_rate"))
    top_player = d.get("top_player", {})

    best_line = ""
    if best:
        best_line = (
            f"\n🏆 <b>Melhor resultado:</b>\n"
            f"   {_esc(best.get('player'))}: {best.get('goals', 0)} gols após perder de "
            f"{best.get('game1_score', '?')}"
        )

    worst_line = ""
    if worst:
        worst_line = (
            f"\n❌ <b>Pior resultado:</b>\n"
            f"   {_esc(worst.get('player'))}: {worst.get('goals', 0)} gols após perder de "
            f"{worst.get('game1_score', '?')}"
        )

    top_line = ""
    if top_player:
        tp_name = _esc(top_player.get("name", ""))
        tp_hits = top_player.get("hits", 0)
        tp_total = top_player.get("total", 0)
        tp_rate = (tp_hits / tp_total * 100) if tp_total > 0 else 0
        top_line = (
            f"\n🔥 <b>Jogador mais confiável hoje:</b>\n"
            f"   {tp_name}: {tp_hits}/{tp_total} over 2.5 ({tp_rate:.0f}%)"
        )

    return (
        f"📋 <b>RELATÓRIO DIÁRIO - {date_str}</b>\n"
        f"\n"
        f"📊 <b>Resumo:</b>\n"
        f"   • Alertas enviados: {total}\n"
        f"   • Over 2.5 bateu: {over25_hits}/{total} ({over25_rate:.1f}%)\n"
        f"   • Over 3.5 bateu: {over35_hits}/{total} ({over35_rate:.1f}%)\n"
        f"   • ROI simulado (flat): {roi_str}\n"
        f"{best_line}"
        f"{worst_line}"
        f"\n📈 Acumulado do mês: {monthly_rate} acerto over 2.5"
        f"{top_line}"
    )


def format_cold_start_progress(d: dict) -> str:
    """Format cold start collection progress."""
    day = d.get("days_collected", 0)
    total_days = d.get("cold_start_days", 90)
    games = d.get("total_games", 0)
    pairs = d.get("total_pairs", 0)
    players = d.get("unique_players", 0)
    teams = d.get("unique_teams", 0)
    pct = (day / total_days) * 100
    bar_filled = int(pct / 10)
    bar = "█" * bar_filled + "░" * (10 - bar_filled)
    eta = d.get("activation_date", "calculando...")

    return (
        f"📊 <b>COLETA EM ANDAMENTO - Dia {day}/{total_days}</b>\n"
        f"\n"
        f"   • Jogos registrados: {games:,}\n"
        f"   • Pares ida/volta: {pairs:,}\n"
        f"   • Jogadores únicos: {players}\n"
        f"   • Times únicos: {teams}\n"
        f"   • Progresso: {pct:.1f}% {bar}\n"
        f"\n"
        f"⏳ Previsão de ativação: {_esc(str(eta))}"
    )


def format_regime_warning(d: dict) -> str:
    """Format regime degradation warning."""
    status = d.get("status", "WARNING")
    emoji = "🚨" if status == "DEGRADED" else "⚠️"
    recent = _pct(d.get("recent_rate"))
    historical = _pct(d.get("historical_rate"))
    z = d.get("z_score", 0.0)
    action = _esc(d.get("action", "Monitorar"))

    return (
        f"{emoji} <b>ALERTA DE REGIME - {status}</b>\n"
        f"\n"
        f"Status: <b>{status}</b>\n"
        f"Taxa recente: {recent}\n"
        f"Taxa histórica: {historical}\n"
        f"Z-score: {z:.2f}\n"
        f"\n"
        f"Ação: {action}"
    )


def format_system_status(d: dict) -> str:
    """Format system health status."""
    uptime = d.get("uptime", "?")
    monitoring = d.get("games_monitoring", 0)
    pending_pairs = d.get("pending_pairs", 0)
    alerts_today = d.get("alerts_today", 0)
    regime = d.get("regime_status", "HEALTHY")
    cold_done = d.get("cold_start_complete", False)
    paused = d.get("alerts_paused", False)

    regime_emoji = {"HEALTHY": "🟢", "WARNING": "🟡", "DEGRADED": "🔴"}.get(regime, "⚪")
    paused_str = " (PAUSADO)" if paused else ""

    return (
        f"🖥️ <b>STATUS DO SISTEMA{paused_str}</b>\n"
        f"\n"
        f"   • Uptime: {uptime}\n"
        f"   • Jogos monitorando: {monitoring}\n"
        f"   • Pares pendentes: {pending_pairs}\n"
        f"   • Alertas hoje: {alerts_today}\n"
        f"   • Regime: {regime_emoji} {regime}\n"
        f"   • Cold start: {'✅ Completo' if cold_done else '⏳ Em andamento'}"
    )
