"""Migration: adicionar campos de P&L ao Alert e criar tabela layer_weights.

IMPORTANTE: Parar o sistema principal antes de rodar esta migration!
O ALTER TABLE precisa de lock exclusivo que conflita com o sistema rodando.

Uso: python -m scripts.migrate_add_pnl
"""
import asyncio

async def main():
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy import text
    from src.config import settings

    engine = create_async_engine(
        settings.database_url,
        pool_pre_ping=True,
        connect_args={"command_timeout": 30},
    )

    async with engine.begin() as conn:
        # Adicionar colunas ao Alert
        for col, typ in [
            ("profit_flat", "FLOAT"),
            ("over15_hit", "BOOLEAN"),
        ]:
            try:
                await conn.execute(text(f"ALTER TABLE alerts ADD COLUMN IF NOT EXISTS {col} {typ}"))
                print(f"  + alerts.{col}")
            except Exception as e:
                print(f"  ! alerts.{col}: {str(e)[:80]}")

        # Criar tabela layer_weights
        try:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS layer_weights (
                    id SERIAL PRIMARY KEY,
                    layer_name VARCHAR NOT NULL UNIQUE,
                    weight FLOAT NOT NULL DEFAULT 1.0,
                    hit_rate_30d FLOAT,
                    sample_size_30d INT DEFAULT 0,
                    last_calibrated_at TIMESTAMP,
                    calibration_notes TEXT
                )
            """))
            print("  + layer_weights criada")
        except Exception as e:
            print(f"  ! layer_weights: {str(e)[:80]}")

    await engine.dispose()
    print("\nMigration concluida! Pode reiniciar o sistema.")

if __name__ == "__main__":
    asyncio.run(main())
