"""Calcula carga real de requests BetsAPI/dia baseado em dados do DB.

Le quantos G2 foram pareados nos ultimos N dias e estima:
- OddsMonitor calls (2 req/poll * ~42 polls/match)
- GameWatcher calls (2 req * freq adaptativa)
- PairMatcher calls (3 req * 288 ciclos/dia + 3 req/G1 finalizado)
- Validator calls (1 req/alerta * ~8 polls)

NAO faz requests externas. Read-only no DB.
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from src.db.database import async_session_factory, init_db
from src.db.models import Alert, Match


async def run(days: int) -> None:
    await init_db()
    sf = async_session_factory
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)

    async with sf() as s:
        # G2 pareados (match com pair_match_id set)
        g2_count = (await s.execute(
            select(func.count(Match.id))
            .where(Match.pair_match_id.is_not(None))
            .where(Match.created_at >= cutoff)
        )).scalar_one()

        # G1 finalizados (trigger de pair_matcher direct)
        g1_count = (await s.execute(
            select(func.count(Match.id))
            .where(Match.score_home.is_not(None))
            .where(Match.created_at >= cutoff)
        )).scalar_one()

        # Alertas enviados (trigger de validator)
        alerts = (await s.execute(
            select(func.count(Alert.id))
            .where(Alert.sent_at >= cutoff)
        )).scalar_one()

        # Por hora do dia (G2 monitorados)
        g2_by_hour = (await s.execute(
            select(func.extract("hour", Match.created_at), func.count(Match.id))
            .where(Match.pair_match_id.is_not(None))
            .where(Match.created_at >= cutoff)
            .group_by(func.extract("hour", Match.created_at))
            .order_by(func.extract("hour", Match.created_at))
        )).all()

    per_day = g2_count / days

    # Estimativas
    # OddsMonitor: ~42 polls (T-3 ate T+4, 10s) * 2 req = 84 req/G2
    odds_per_g2 = 84
    odds_total = per_day * odds_per_g2

    # GameWatcher: 2 req a cada 30s medio = 5760/dia
    gw_total = 5760

    # PairMatcher loop: 3 req a cada 300s = 864/dia
    pm_loop = 864
    # PairMatcher reacao: 3 req/G1
    pm_reactive = (g1_count / days) * 3

    # Validator: ~8 polls * 1 req por alerta
    val_total = (alerts / days) * 8 * 2  # *2 porque tem Validator + ValidatorV2

    total = odds_total + gw_total + pm_loop + pm_reactive + val_total

    print(f"\n=== BetsAPI — Carga estimada ({days}d de dados reais) ===\n")
    print(f"G2 pareados/dia:   {per_day:>6.1f}")
    print(f"G1 finalizados/dia: {g1_count/days:>6.1f}")
    print(f"Alertas/dia:       {alerts/days:>6.1f}")
    print()
    print(f"{'Componente':<30} {'Req/dia':>10}")
    print("-" * 42)
    print(f"{'OddsMonitor (84/G2)':<30} {odds_total:>10.0f}")
    print(f"{'GameWatcher (adaptive)':<30} {gw_total:>10.0f}")
    print(f"{'PairMatcher loop (300s)':<30} {pm_loop:>10.0f}")
    print(f"{'PairMatcher reacao':<30} {pm_reactive:>10.0f}")
    print(f"{'Validators (Val + ValV2)':<30} {val_total:>10.0f}")
    print("-" * 42)
    print(f"{'TOTAL':<30} {total:>10.0f}")
    print(f"{'Media /hora':<30} {total/24:>10.0f}")
    print()

    # Distribuicao por hora
    hour_counts = {int(h): c for h, c in g2_by_hour}
    print("G2 monitorados por hora (UTC) — indicador de pico:")
    max_count = max(hour_counts.values()) if hour_counts else 1
    for h in range(24):
        c = hour_counts.get(h, 0)
        avg = c / days
        bar = "#" * int((c / max_count) * 30) if max_count > 0 else ""
        # Pico estimado de req/hora = avg_g2 * 84 (todos os polls ocorrem perto do kickoff)
        peak_req = avg * odds_per_g2 + (gw_total + pm_loop) / 24
        print(f"  {h:02d}h  {avg:>4.1f} G2/h  {bar:<30}  ~{peak_req:>4.0f} req/h")


if __name__ == "__main__":
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    asyncio.run(run(days))
