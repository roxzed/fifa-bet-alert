"""Migration idempotente: cria tabela blocked_lines + adiciona alerts.suppressed.

Roda 1 vez antes de subir o filtro automatico.
Pode rodar varias vezes (seguro — checa antes de alterar)."""
import asyncio
import asyncpg

DB = "postgresql://postgres:CEKA2uwnKGPGAws6@db.aoxwotodixhzfgcbuoem.supabase.co:5432/postgres"


async def main() -> None:
    c = await asyncpg.connect(DB)

    # 1) Adicionar coluna alerts.suppressed se nao existir
    has_col = await c.fetchval("""
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'alerts' AND column_name = 'suppressed'
    """)
    if has_col:
        print("OK: alerts.suppressed ja existe")
    else:
        await c.execute(
            "ALTER TABLE alerts ADD COLUMN suppressed BOOLEAN NOT NULL DEFAULT FALSE"
        )
        print("CRIADA: coluna alerts.suppressed (default FALSE)")

    # 2) Index composto (player, best_line) pra performance da state machine
    has_idx = await c.fetchval("""
        SELECT 1 FROM pg_indexes
        WHERE tablename = 'alerts' AND indexname = 'ix_alerts_player_line'
    """)
    if has_idx:
        print("OK: ix_alerts_player_line ja existe")
    else:
        await c.execute(
            "CREATE INDEX ix_alerts_player_line ON alerts (losing_player, best_line)"
        )
        print("CRIADO: ix_alerts_player_line")

    # 3) Criar tabela blocked_lines se nao existir
    has_tab = await c.fetchval("""
        SELECT 1 FROM information_schema.tables WHERE table_name = 'blocked_lines'
    """)
    if has_tab:
        print("OK: tabela blocked_lines ja existe")
    else:
        await c.execute("""
            CREATE TABLE blocked_lines (
                player VARCHAR NOT NULL,
                line VARCHAR NOT NULL,
                state VARCHAR NOT NULL DEFAULT 'ACTIVE',
                block_count INTEGER NOT NULL DEFAULT 0,
                shadow_start_pl NUMERIC,
                shadow_start_at TIMESTAMP,
                last_block_at TIMESTAMP,
                last_unblock_at TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
                PRIMARY KEY (player, line)
            )
        """)
        await c.execute(
            "CREATE INDEX ix_blocked_lines_state ON blocked_lines (state)"
        )
        print("CRIADA: tabela blocked_lines")

    await c.close()
    print("\nMigration concluida.")


asyncio.run(main())
