"""Migration: SHADOW protocol M2.

1. Adiciona coluna alerts_v2.suppressed (BOOLEAN DEFAULT FALSE)
2. Cria tabela blocked_lines_v2 (clone de blocked_lines)

Idempotente — nao falha se ja existir.
"""
import asyncio

from sqlalchemy import text

from src.db.database import async_session_factory, init_db


async def main():
    await init_db()
    async with async_session_factory() as s:
        # 1) coluna alerts_v2.suppressed
        result = await s.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='alerts_v2' AND column_name='suppressed'"
            )
        )
        if not result.scalar_one_or_none():
            await s.execute(
                text(
                    "ALTER TABLE alerts_v2 "
                    "ADD COLUMN suppressed BOOLEAN NOT NULL DEFAULT FALSE"
                )
            )
            print("alerts_v2.suppressed adicionado")
        else:
            print("alerts_v2.suppressed ja existe — skip")

        # 2) tabela blocked_lines_v2
        result = await s.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_name='blocked_lines_v2'"
            )
        )
        if not result.scalar_one_or_none():
            await s.execute(text("""
                CREATE TABLE blocked_lines_v2 (
                    player VARCHAR NOT NULL,
                    line VARCHAR NOT NULL,
                    state VARCHAR NOT NULL DEFAULT 'ACTIVE',
                    block_count INTEGER NOT NULL DEFAULT 0,
                    shadow_start_pl DOUBLE PRECISION,
                    shadow_start_at TIMESTAMP,
                    last_block_at TIMESTAMP,
                    last_unblock_at TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT NOW() NOT NULL,
                    PRIMARY KEY (player, line)
                )
            """))
            await s.execute(text(
                "CREATE INDEX ix_blocked_lines_v2_state "
                "ON blocked_lines_v2 (state)"
            ))
            print("blocked_lines_v2 criada")
        else:
            print("blocked_lines_v2 ja existe — skip")

        await s.commit()
        print("Migration M2 concluida")


asyncio.run(main())
