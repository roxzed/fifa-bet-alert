"""
Backfill player stats from historical match pairs.

Computa tudo em memória e faz upsert incremental no final.

Melhorias v2 (2026-03-25):
  - MELHORIA 3: Upsert incremental — não zera mais a tabela players inteira.
    Cada jogador é atualizado individualmente sem perda de dados.
  - Modo --full: força reset completo (zera + atualiza) se necessário.

Uso:
    python scripts/backfill_player_stats.py            # incremental (upsert)
    python scripts/backfill_player_stats.py --full     # full rebuild
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
class PlayerStats:
    total_return_matches: int = 0
    over25_after_loss: int = 0
    over35_after_loss: int = 0
    total_goals: float = 0.0
    tight_loss_count: int = 0
    tight_loss_over25: int = 0
    medium_loss_count: int = 0
    medium_loss_over25: int = 0
    blowout_loss_count: int = 0
    blowout_loss_over25: int = 0


async def backfill(full_rebuild: bool = False) -> None:
    async with get_session() as session:
        # 1. Buscar todos os pares com resultado
        result = await session.execute(text("""
            SELECT
                m1.player_home  AS g1_ph,
                m1.player_away  AS g1_pa,
                m1.score_home   AS g1_sh,
                m1.score_away   AS g1_sa,
                m2.player_home  AS g2_ph,
                m2.score_home   AS g2_sh,
                m2.score_away   AS g2_sa
            FROM matches m2
            JOIN matches m1 ON m2.pair_match_id = m1.id
            WHERE m2.is_return_match = true
              AND m1.score_home IS NOT NULL AND m1.score_away IS NOT NULL
              AND m2.score_home IS NOT NULL AND m2.score_away IS NOT NULL
            ORDER BY m1.id
        """))
        rows = result.fetchall()
        logger.info(f"Pares encontrados: {len(rows)}")

        # 2. Computar tudo em memória
        stats: dict[str, PlayerStats] = defaultdict(PlayerStats)
        skipped = 0

        for row in rows:
            g1_sh, g1_sa = row.g1_sh, row.g1_sa

            if g1_sh == g1_sa:
                skipped += 1
                continue

            if g1_sh < g1_sa:
                loser = row.g1_ph
                score_winner, score_loser = g1_sa, g1_sh
            else:
                loser = row.g1_pa
                score_winner, score_loser = g1_sh, g1_sa

            loser_goals_g2 = row.g2_sh if row.g2_ph == loser else row.g2_sa

            loss_type = classify_loss(score_winner, score_loser)
            over25_hit = loser_goals_g2 > 2
            over35_hit = loser_goals_g2 > 3

            s = stats[loser]
            s.total_return_matches += 1
            s.total_goals += loser_goals_g2
            if over25_hit:
                s.over25_after_loss += 1
            if over35_hit:
                s.over35_after_loss += 1

            margin = 1 if "tight" in loss_type else (2 if "medium" in loss_type else 3)
            if margin == 1:
                s.tight_loss_count += 1
                if over25_hit:
                    s.tight_loss_over25 += 1
            elif margin == 2:
                s.medium_loss_count += 1
                if over25_hit:
                    s.medium_loss_over25 += 1
            else:
                s.blowout_loss_count += 1
                if over25_hit:
                    s.blowout_loss_over25 += 1

        logger.info(f"Computado em memória: {len(stats)} jogadores, {skipped} empates ignorados")

        # 3. MELHORIA 3: Upsert incremental (ou full rebuild)
        if full_rebuild:
            logger.warning("Modo --full: zerando stats de todos os players antes de atualizar")
            await session.execute(text("""
                UPDATE players SET
                    total_return_matches = 0, over25_after_loss = 0, over35_after_loss = 0,
                    hit_rate_25 = 0.0, hit_rate_35 = 0.0, avg_goals_after_loss = 0.0,
                    tight_loss_count = 0, tight_loss_over25 = 0,
                    medium_loss_count = 0, medium_loss_over25 = 0,
                    blowout_loss_count = 0, blowout_loss_over25 = 0,
                    is_reliable = false, reliability_score = 0.0
            """))

        now_naive = datetime.now(timezone.utc).replace(tzinfo=None)
        updated = 0
        inserted = 0

        for name, s in stats.items():
            n = s.total_return_matches
            hit25 = s.over25_after_loss / n
            hit35 = s.over35_after_loss / n
            avg_goals = s.total_goals / n
            is_reliable = n >= 10
            reliability_score = 0.0
            if is_reliable:
                reliability_score = min(n / 50.0, 1.0) * 50 + hit25 * 50

            # Upsert: update se existe, insert se não existe
            res = await session.execute(text("""
                UPDATE players SET
                    total_return_matches  = :trm,
                    over25_after_loss     = :o25,
                    over35_after_loss     = :o35,
                    hit_rate_25           = :hr25,
                    hit_rate_35           = :hr35,
                    avg_goals_after_loss  = :avg,
                    tight_loss_count      = :tlc,
                    tight_loss_over25     = :tlo,
                    medium_loss_count     = :mlc,
                    medium_loss_over25    = :mlo,
                    blowout_loss_count    = :blc,
                    blowout_loss_over25   = :blo,
                    is_reliable           = :rel,
                    reliability_score     = :rs,
                    last_seen             = :ls
                WHERE name = :name
            """), {
                "name": name, "trm": n, "o25": s.over25_after_loss,
                "o35": s.over35_after_loss, "hr25": hit25, "hr35": hit35,
                "avg": avg_goals, "tlc": s.tight_loss_count, "tlo": s.tight_loss_over25,
                "mlc": s.medium_loss_count, "mlo": s.medium_loss_over25,
                "blc": s.blowout_loss_count, "blo": s.blowout_loss_over25,
                "rel": is_reliable, "rs": reliability_score, "ls": now_naive,
            })

            if res.rowcount > 0:
                updated += 1
            else:
                # Jogador não existe ainda — criar
                await session.execute(text("""
                    INSERT INTO players (name, total_return_matches, over25_after_loss,
                        over35_after_loss, hit_rate_25, hit_rate_35, avg_goals_after_loss,
                        tight_loss_count, tight_loss_over25, medium_loss_count,
                        medium_loss_over25, blowout_loss_count, blowout_loss_over25,
                        is_reliable, reliability_score, last_seen)
                    VALUES (:name, :trm, :o25, :o35, :hr25, :hr35, :avg,
                        :tlc, :tlo, :mlc, :mlo, :blc, :blo, :rel, :rs, :ls)
                """), {
                    "name": name, "trm": n, "o25": s.over25_after_loss,
                    "o35": s.over35_after_loss, "hr25": hit25, "hr35": hit35,
                    "avg": avg_goals, "tlc": s.tight_loss_count, "tlo": s.tight_loss_over25,
                    "mlc": s.medium_loss_count, "mlo": s.medium_loss_over25,
                    "blc": s.blowout_loss_count, "blo": s.blowout_loss_over25,
                    "rel": is_reliable, "rs": reliability_score, "ls": now_naive,
                })
                inserted += 1

        mode = "full rebuild" if full_rebuild else "upsert incremental"
        logger.info(f"DB ({mode}): {updated} atualizados, {inserted} inseridos")

        # 5. Resumo
        result2 = await session.execute(text("""
            SELECT
                COUNT(*) as total_players,
                SUM(total_return_matches) as total_returns,
                ROUND(AVG(hit_rate_25)::numeric, 3) as avg_hit25,
                ROUND(AVG(hit_rate_35)::numeric, 3) as avg_hit35,
                COUNT(CASE WHEN is_reliable THEN 1 END) as reliable_players,
                MAX(total_return_matches) as max_returns,
                (SELECT name FROM players ORDER BY total_return_matches DESC LIMIT 1) as top_player
            FROM players WHERE total_return_matches > 0
        """))
        r = result2.fetchone()
        logger.info("=" * 50)
        logger.info(f"Players com dados:              {r.total_players}")
        logger.info(f"Total retornos processados:     {r.total_returns}")
        logger.info(f"Hit rate média over2.5:         {float(r.avg_hit25):.1%}")
        logger.info(f"Hit rate média over3.5:         {float(r.avg_hit35):.1%}")
        logger.info(f"Players confiáveis (>=10):      {r.reliable_players}")
        logger.info(f"Jogador com mais dados:         {r.top_player} ({r.max_returns} retornos)")
        logger.info("=" * 50)

        # Top 10 players
        top = await session.execute(text("""
            SELECT name, total_return_matches, hit_rate_25, hit_rate_35, avg_goals_after_loss
            FROM players
            WHERE total_return_matches >= 10
            ORDER BY total_return_matches DESC
            LIMIT 10
        """))
        logger.info("Top 10 jogadores por volume:")
        for row in top.fetchall():
            logger.info(
                f"  {row.name:20s} | {row.total_return_matches:4d} retornos | "
                f"O2.5: {row.hit_rate_25:.1%} | O3.5: {row.hit_rate_35:.1%} | "
                f"avg gols: {row.avg_goals_after_loss:.2f}"
            )


if __name__ == "__main__":
    full = "--full" in sys.argv
    asyncio.run(backfill(full_rebuild=full))
