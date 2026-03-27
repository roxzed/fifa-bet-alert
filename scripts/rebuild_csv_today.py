"""Rebuild CSV from today's validated alerts in PostgreSQL.

Uso: python -m scripts.rebuild_csv_today
"""
import asyncio
import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path


CSV_PATH = Path("data/results.csv")


async def main():
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text
    from src.config import settings

    try:
        from zoneinfo import ZoneInfo
        tz_local = ZoneInfo(settings.timezone)
    except Exception:
        tz_local = timezone(timedelta(hours=-3))

    engine = create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        connect_args={"command_timeout": 30},
    )

    async with engine.connect() as conn:
        result = await conn.execute(text("""
            SELECT a.id, a.losing_player, a.best_line,
                   a.over25_odds, a.over35_odds, a.over45_odds,
                   a.true_prob, a.actual_goals,
                   a.over25_hit, a.over35_hit, a.over45_hit, a.over15_hit,
                   a.ml_odds, a.ml_hit,
                   a.sent_at, a.validated_at,
                   m.player_home, m.player_away, m.team_home, m.team_away,
                   m.score_home, m.score_away
            FROM alerts a
            JOIN matches m ON a.match_id = m.id
            WHERE a.sent_at >= :today
              AND a.validated_at IS NOT NULL
            ORDER BY a.sent_at
        """), {"today": "2026-03-27 00:00:00"})

        rows = result.mappings().all()

    await engine.dispose()

    if not rows:
        print("Nenhum alerta validado encontrado hoje.")
        return

    CSV_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "alert_id", "data", "hora", "jogador_perdedor", "linha", "odds",
            "winrate", "resultado", "placar_g2", "gols_perdedor",
            "green", "profit",
            "player_home", "player_away", "team_home", "team_away",
        ])

        total_profit = 0.0
        greens = 0
        total = 0

        for r in rows:
            bl = r["best_line"] or "over25"

            # Line label
            labels = {"over15": "Over 1.5", "over25": "Over 2.5", "over35": "Over 3.5",
                      "over45": "Over 4.5", "ml": "ML Vitoria"}
            line_label = labels.get(bl, bl)

            # Odds
            if bl == "over15":
                odds = None  # over15_odds column doesn't exist yet in DB
            elif bl == "over25":
                odds = r["over25_odds"]
            elif bl == "over35":
                odds = r["over35_odds"]
            elif bl == "over45":
                odds = r["over45_odds"]
            elif bl == "ml":
                odds = r["ml_odds"]
            else:
                odds = r["over25_odds"]

            # Determine loser goals
            actual = r["actual_goals"]
            if actual is None:
                continue

            # Hit
            if bl == "over15":
                hit = actual > 1
            elif bl == "over25":
                hit = actual > 2
            elif bl == "over35":
                hit = actual > 3
            elif bl == "over45":
                hit = actual > 4
            elif bl == "ml":
                hit = bool(r["ml_hit"])
            else:
                hit = actual > 2

            profit = (odds - 1.0) if hit and odds else -1.0
            total_profit += profit
            total += 1
            if hit:
                greens += 1

            # Convert to local time
            sent_utc = r["sent_at"]
            if sent_utc:
                sent_local = sent_utc.replace(tzinfo=timezone.utc).astimezone(tz_local)
            else:
                sent_local = datetime.now(tz_local)

            placar = f"{r['score_home']}-{r['score_away']}" if r["score_home"] is not None else "?"

            writer.writerow([
                r["id"],
                sent_local.strftime("%Y-%m-%d"),
                sent_local.strftime("%H:%M"),
                r["losing_player"],
                line_label,
                f"{odds:.2f}" if odds else "",
                f"{r['true_prob']:.1%}" if r["true_prob"] else "",
                "GREEN" if hit else "RED",
                placar,
                actual,
                1 if hit else 0,
                f"{profit:.2f}",
                r["player_home"] or "",
                r["player_away"] or "",
                r["team_home"] or "",
                r["team_away"] or "",
            ])

    losses = total - greens
    roi = (total_profit / total * 100) if total > 0 else 0
    print(f"CSV reconstruido: {CSV_PATH}")
    print(f"  {total} alertas | {greens}W {losses}L | Net: {total_profit:+.2f}u | ROI: {roi:+.1f}%")


if __name__ == "__main__":
    asyncio.run(main())
