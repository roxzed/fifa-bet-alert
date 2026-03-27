"""Migration: adicionar coluna over15_odds ao Alert.

IMPORTANTE: Parar o sistema principal antes de rodar esta migration!
O ALTER TABLE precisa de lock exclusivo que conflita com o sistema rodando.

Uso: python -m scripts.migrate_add_over15_odds
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
        try:
            await conn.execute(text(
                "ALTER TABLE alerts ADD COLUMN IF NOT EXISTS over15_odds FLOAT"
            ))
            print("  + alerts.over15_odds adicionada")
        except Exception as e:
            print(f"  ! alerts.over15_odds: {str(e)[:80]}")

    await engine.dispose()
    print("\nMigration concluida! Pode reiniciar o sistema.")


if __name__ == "__main__":
    asyncio.run(main())
