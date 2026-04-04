"""
Backfill method_stats table from historical match pairs.

Populates all stat_key variants used by stats_engine.py:
  - global              → overall hit rates
  - loss_{type}         → per loss-margin type
  - player_general_{n}  → per player (as loser)
  - recent_form_{n}     → last 20 pairs per player
  - h2h_{a}_vs_{b}      → loser vs opponent pairs
  - y_post_win_{n}      → winner's goals in return match
  - time_{slot}         → 6-hour time buckets

Melhorias v2 (2026-03-25):
  - MELHORIA 3: Upsert incremental — usa INSERT ... ON CONFLICT DO UPDATE
    em vez de DELETE + INSERT. Elimina a janela de indisponibilidade de dados.
  - Batch upsert: acumula em lotes de 500 para reduzir round-trips.
  - Modo --full: força recriação completa (DELETE + INSERT) se necessário.

Uso:
    python scripts/backfill_method_stats.py            # incremental (upsert)
    python scripts/backfill_method_stats.py --full     # full rebuild
"""

import asyncio
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text
from loguru import logger

from src.db.database import get_session
from src.core.probability import classify_loss


@dataclass
class Bucket:
    total: int = 0
    over15: int = 0
    over25: int = 0
    over35: int = 0
    over45: int = 0
    total_goals: float = 0.0


def _time_slot(hour: int | None) -> str | None:
    if hour is None:
        return None
    base = (hour // 6) * 6
    return f"{base:02d}-{base + 6:02d}h"


def _stat_type_from_key(stat_key: str) -> str:
    """Determine stat_type from key prefix."""
    if stat_key == "global":
        return "global"
    elif stat_key.startswith("loss_"):
        return "loss_type"
    elif stat_key.startswith("player_general_"):
        return "player"
    elif stat_key.startswith("recent_form_"):
        return "recent_form"
    elif stat_key.startswith("h2h_"):
        return "h2h"
    elif stat_key.startswith("y_post_win_"):
        return "y_post_win"
    elif stat_key.startswith("time_"):
        return "time_slot"
    return "other"


async def _upsert_batch(session, batch: list[dict]) -> int:
    """Upsert a batch of method_stats rows using INSERT ... ON CONFLICT.

    Requires a UNIQUE constraint on method_stats.stat_key.
    Falls back to individual UPDATE+INSERT if ON CONFLICT is not available.
    """
    if not batch:
        return 0

    upserted = 0
    for row in batch:
        try:
            await session.execute(text("""
                INSERT INTO method_stats
                    (stat_key, stat_type, total_samples, over15_hits, over25_hits,
                     over35_hits, over45_hits, hit_rate_15, hit_rate_25, hit_rate_35,
                     hit_rate_45, avg_goals, last_updated)
                VALUES
                    (:k, :st, :n, :o15, :o25, :o35, :o45, :hr15, :hr25, :hr35, :hr45, :avg, :ts)
                ON CONFLICT (stat_key) DO UPDATE SET
                    stat_type     = EXCLUDED.stat_type,
                    total_samples = EXCLUDED.total_samples,
                    over15_hits   = EXCLUDED.over15_hits,
                    over25_hits   = EXCLUDED.over25_hits,
                    over35_hits   = EXCLUDED.over35_hits,
                    over45_hits   = EXCLUDED.over45_hits,
                    hit_rate_15   = EXCLUDED.hit_rate_15,
                    hit_rate_25   = EXCLUDED.hit_rate_25,
                    hit_rate_35   = EXCLUDED.hit_rate_35,
                    hit_rate_45   = EXCLUDED.hit_rate_45,
                    avg_goals     = EXCLUDED.avg_goals,
                    last_updated  = EXCLUDED.last_updated
            """), row)
            upserted += 1
        except Exception as e:
            # Fallback: try UPDATE then INSERT
            try:
                res = await session.execute(text("""
                    UPDATE method_stats SET
                        stat_type = :st, total_samples = :n,
                        over15_hits = :o15, over25_hits = :o25, over35_hits = :o35,
                        over45_hits = :o45, hit_rate_15 = :hr15, hit_rate_25 = :hr25,
                        hit_rate_35 = :hr35, hit_rate_45 = :hr45,
                        avg_goals = :avg, last_updated = :ts
                    WHERE stat_key = :k
                """), row)
                if res.rowcount == 0:
                    await session.execute(text("""
                        INSERT INTO method_stats
                            (stat_key, stat_type, total_samples, over15_hits, over25_hits,
                             over35_hits, over45_hits, hit_rate_15, hit_rate_25, hit_rate_35,
                             hit_rate_45, avg_goals, last_updated)
                        VALUES
                            (:k, :st, :n, :o15, :o25, :o35, :o45, :hr15, :hr25, :hr35, :hr45, :avg, :ts)
                    """), row)
                upserted += 1
            except Exception as e2:
                logger.warning(f"Failed to upsert {row['k']}: {e2}")

    return upserted


async def backfill(full_rebuild: bool = False) -> None:
    async with get_session() as session:
        # Fetch all completed pairs with hour info
        result = await session.execute(text("""
            SELECT
                m1.player_home  AS g1_ph,
                m1.player_away  AS g1_pa,
                m1.score_home   AS g1_sh,
                m1.score_away   AS g1_sa,
                m2.player_home  AS g2_ph,
                m2.player_away  AS g2_pa,
                m2.score_home   AS g2_sh,
                m2.score_away   AS g2_sa,
                m1.hour_of_day  AS hour
            FROM matches m2
            JOIN matches m1 ON m2.pair_match_id = m1.id
            WHERE m2.is_return_match = true
              AND m1.score_home IS NOT NULL AND m1.score_away IS NOT NULL
              AND m2.score_home IS NOT NULL AND m2.score_away IS NOT NULL
            ORDER BY m1.id
        """))
        rows = result.fetchall()
        logger.info(f"Pares encontrados: {len(rows)}")

        # In-memory accumulators
        buckets: dict[str, Bucket] = defaultdict(Bucket)
        player_history: dict[str, list[tuple[bool, bool, bool, bool, int]]] = defaultdict(list)
        h2h_history: dict[str, list[tuple[bool, bool, bool, bool, int]]] = defaultdict(list)

        skipped = 0
        for row in rows:
            g1_sh, g1_sa = row.g1_sh, row.g1_sa
            if g1_sh == g1_sa:
                skipped += 1
                continue

            if g1_sh < g1_sa:
                loser = row.g1_ph
                winner = row.g1_pa
                score_winner, score_loser = g1_sa, g1_sh
            else:
                loser = row.g1_pa
                winner = row.g1_ph
                score_winner, score_loser = g1_sh, g1_sa

            if row.g2_ph == loser:
                loser_goals_g2 = row.g2_sh
                winner_goals_g2 = row.g2_sa
            else:
                loser_goals_g2 = row.g2_sa
                winner_goals_g2 = row.g2_sh

            loss_type = classify_loss(score_winner, score_loser)
            o15 = loser_goals_g2 > 1
            o25 = loser_goals_g2 > 2
            o35 = loser_goals_g2 > 3
            o45 = loser_goals_g2 > 4

            def _add(b: Bucket) -> None:
                b.total += 1; b.over15 += int(o15); b.over25 += int(o25)
                b.over35 += int(o35); b.over45 += int(o45)
                b.total_goals += loser_goals_g2

            # global
            _add(buckets["global"])

            # loss type
            _add(buckets[f"loss_{loss_type}"])

            # player_general
            _add(buckets[f"player_general_{loser}"])

            player_history[loser].append((o15, o25, o35, o45, int(loser_goals_g2)))

            # h2h — acumula no historico para depois limitar a 20 jogos
            h2h_history[f"h2h_{loser}_vs_{winner}"].append(
                (o15, o25, o35, o45, int(loser_goals_g2))
            )

            # y_post_win
            yw_o15 = winner_goals_g2 > 1
            yw_o25 = winner_goals_g2 > 2
            yw_o35 = winner_goals_g2 > 3
            yw_o45 = winner_goals_g2 > 4
            bw = buckets[f"y_post_win_{winner}"]
            bw.total += 1; bw.over15 += int(yw_o15); bw.over25 += int(yw_o25)
            bw.over35 += int(yw_o35); bw.over45 += int(yw_o45)
            bw.total_goals += winner_goals_g2

            # time slot
            slot = _time_slot(row.hour)
            if slot:
                _add(buckets[f"time_{slot}"])

        # Build H2H buckets from last 20 games per matchup direction
        for h2h_key, history in h2h_history.items():
            recent = history[-20:]
            b = buckets[h2h_key]
            b.total = len(recent)
            b.over15 = sum(1 for h in recent if h[0])
            b.over25 = sum(1 for h in recent if h[1])
            b.over35 = sum(1 for h in recent if h[2])
            b.over45 = sum(1 for h in recent if h[3])
            b.total_goals = sum(h[4] for h in recent)

        # Build recent_form buckets from last 20 per player
        for player, history in player_history.items():
            recent = history[-20:]
            b = buckets[f"recent_form_{player}"]
            b.total = len(recent)
            b.over15 = sum(1 for h in recent if h[0])
            b.over25 = sum(1 for h in recent if h[1])
            b.over35 = sum(1 for h in recent if h[2])
            b.over45 = sum(1 for h in recent if h[3])
            b.total_goals = sum(h[4] for h in recent)

        logger.info(f"Buckets em memória: {len(buckets)} chaves, {skipped} empates ignorados")

        # ── MELHORIA 3: Upsert incremental (ou full rebuild) ────────────
        if full_rebuild:
            logger.warning("Modo --full: limpando tabela method_stats antes de inserir")
            await session.execute(text("DELETE FROM method_stats"))

        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        total_upserted = 0
        batch: list[dict] = []
        BATCH_SIZE = 500

        for stat_key, b in buckets.items():
            if b.total == 0:
                continue

            hr15 = b.over15 / b.total
            hr25 = b.over25 / b.total
            hr35 = b.over35 / b.total
            hr45 = b.over45 / b.total
            avg_goals = b.total_goals / b.total

            batch.append({
                "k": stat_key, "st": _stat_type_from_key(stat_key),
                "n": b.total, "o15": b.over15, "o25": b.over25,
                "o35": b.over35, "o45": b.over45,
                "hr15": hr15, "hr25": hr25, "hr35": hr35, "hr45": hr45,
                "avg": avg_goals, "ts": now_naive,
            })

            if len(batch) >= BATCH_SIZE:
                total_upserted += await _upsert_batch(session, batch)
                batch.clear()

        # Flush remaining
        if batch:
            total_upserted += await _upsert_batch(session, batch)

        mode = "full rebuild" if full_rebuild else "upsert incremental"
        logger.info(f"Concluído ({mode}): {total_upserted} linhas em method_stats")

        # ── Resumo ────────────────────────────────────────────────────────
        g = buckets.get("global")
        if g and g.total > 0:
            logger.info("=" * 50)
            logger.info(f"Global: {g.total} pares")
            logger.info(f"Hit rate O1.5: {g.over15 / g.total:.1%}")
            logger.info(f"Hit rate O2.5: {g.over25 / g.total:.1%}")
            logger.info(f"Hit rate O3.5: {g.over35 / g.total:.1%}")
            logger.info(f"Hit rate O4.5: {g.over45 / g.total:.1%}")
            logger.info(f"Avg gols loser G2: {g.total_goals / g.total:.2f}")
            logger.info("=" * 50)

        for lt in ["tight", "medium", "blowout"]:
            b = buckets.get(f"loss_{lt}")
            if b and b.total > 0:
                logger.info(
                    f"loss_{lt:8s}: {b.total:4d} pares | "
                    f"O2.5: {b.over25 / b.total:.1%} | O3.5: {b.over35 / b.total:.1%}"
                )

        player_buckets = [
            (k[len("player_general_"):], v)
            for k, v in buckets.items()
            if k.startswith("player_general_") and v.total >= 10
        ]
        player_buckets.sort(key=lambda x: x[1].total, reverse=True)
        logger.info(f"\nTop 10 players (min 10 pares):")
        for name, b in player_buckets[:10]:
            logger.info(
                f"  {name:20s} | {b.total:4d} retornos | "
                f"O2.5: {b.over25 / b.total:.1%} | O3.5: {b.over35 / b.total:.1%}"
            )


if __name__ == "__main__":
    full = "--full" in sys.argv
    asyncio.run(backfill(full_rebuild=full))
