"""Mock do relatorio do dia ao meio-dia, usando alertas de ONTEM como
'movimento hoje' simulado. So pra revisar formato — nao toca producao."""
import asyncio
import sys
from datetime import datetime, timezone, timedelta

import asyncpg

DB = "postgresql://postgres:CEKA2uwnKGPGAws6@db.aoxwotodixhzfgcbuoem.supabase.co:5432/postgres"


async def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    c = await asyncpg.connect(DB)
    # PL acumulado total por (player, line)
    all_rows = await c.fetch("""
        SELECT losing_player AS player, best_line AS line,
               SUM(profit_flat) pl, COUNT(*) n
        FROM alerts
        WHERE sent_at >= '2026-04-15 01:07:00'
          AND profit_flat IS NOT NULL AND best_line IS NOT NULL
        GROUP BY 1,2
    """)
    # Mock "hoje" = pega ontem BRT (24/04) como exemplo de dia cheio
    today_rows = await c.fetch("""
        SELECT losing_player AS player, best_line AS line,
               SUM(profit_flat) pl, COUNT(*) n
        FROM alerts
        WHERE (sent_at AT TIME ZONE 'UTC' AT TIME ZONE 'America/Sao_Paulo')::date = '2026-04-24'
          AND profit_flat IS NOT NULL AND best_line IS NOT NULL
        GROUP BY 1,2
    """)
    # Estado atual blocked
    blocked = await c.fetch("""
        SELECT player, line, state, block_count, shadow_start_pl
        FROM blocked_lines
        WHERE state IN ('SHADOW','PERMANENT')
        ORDER BY player, line
    """)
    await c.close()

    line_label = {"over15": "O1.5", "over25": "O2.5", "over35": "O3.5", "over45": "O4.5"}
    pl_map = {(r["player"], r["line"]): (float(r["pl"] or 0), int(r["n"] or 0)) for r in all_rows}
    today_map = {(r["player"], r["line"]): (float(r["pl"] or 0), int(r["n"] or 0)) for r in today_rows}

    blocked_pairs = {(b["player"], b["line"]) for b in blocked}

    parts = []
    parts.append("📊 <b>BLOCKED LINES — 25/04 12:00 BRT</b>")
    parts.append("")

    if blocked:
        parts.append(f"🔒 <b>Bloqueadas ({len(blocked)}):</b>")
        parts.append("<pre>")
        for bl in blocked:
            tot_pl, tot_n = pl_map.get((bl["player"], bl["line"]), (0.0, 0))
            today_p, today_n = today_map.get((bl["player"], bl["line"]), (0.0, 0))
            label = line_label.get(bl["line"], bl["line"])
            tag = "⛔PERM" if bl["state"] == "PERMANENT" else "🔇SHAD"
            arrow = "↑" if today_p > 0 else ("↓" if today_p < 0 else "=")
            parts.append(
                f"{tag} {bl['player'][:12]:<12} {label:<5} "
                f"PL={tot_pl:+6.2f}u({tot_n:>2}) "
                f"hoje={today_p:+5.2f}u({today_n}){arrow}"
            )
        parts.append("</pre>")
        parts.append("")

    rows = []
    for (player, line), (pl_total, n_total) in pl_map.items():
        if line not in line_label:
            continue
        pl_t, n_t = today_map.get((player, line), (0.0, 0))
        rows.append({
            "player": player, "line": line,
            "pl_total": pl_total, "n_total": n_total,
            "pl_today": pl_t, "n_today": n_t,
            "blocked": (player, line) in blocked_pairs,
        })
    rows.sort(key=lambda r: -r["pl_total"])

    if rows:
        parts.append(f"📋 <b>Todas as linhas ({len(rows)}):</b>")
        parts.append("<pre>")
        parts.append(f"{'jogador':<13} {'lin':<4} {'PL_total':>11} {'hoje':>9}")
        for r in rows:
            label = line_label.get(r["line"], r["line"])
            if r["n_today"] > 0:
                arrow = "↑" if r["pl_today"] > 0 else ("↓" if r["pl_today"] < 0 else "=")
                today_str = f"{r['pl_today']:+5.2f}u{arrow}"
            else:
                today_str = "    —    "
            mark = "🔇" if r["blocked"] else "  "
            parts.append(
                f"{mark}{r['player'][:11]:<11} {label:<4} "
                f"{r['pl_total']:+7.2f}u({r['n_total']:>2}) "
                f"{today_str}"
            )
        parts.append("</pre>")

        sum_pl = sum(r["pl_today"] for r in rows if r["n_today"] > 0)
        sum_n = sum(r["n_today"] for r in rows)
        if sum_n:
            roi = sum_pl / sum_n * 100
            parts.append("")
            parts.append(f"🧮 <b>Hoje:</b> {sum_n} alertas, {sum_pl:+.2f}u, ROI {roi:+.1f}%")

    text = "\n".join(parts)
    print("=" * 72)
    print(text)
    print("=" * 72)
    print(f"\nTamanho: {len(text)} chars (limite Telegram = 4096)")


asyncio.run(main())
