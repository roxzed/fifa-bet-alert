"""Migration soft-reset SHADOW v2 (2026-04-28).

Limpa a tabela `blocked_lines` pra que o primeiro recompute sob protocolo
v2K-pl1n5 popule o estado do zero, em vez de carregar bloqueios antigos
disparados por criterios v1 (-1u cumulativo).

Idempotente. Nao destroi dados — `blocked_lines` e tabela de estado, nao
historico. Os alertas (e seus suppressed=True flags) ficam intactos.

Resultado esperado apos primeiro recompute v2:
- Linhas que cumprem novos criterios (rolling -1.5u + n>=4 OU cliff) -> SHADOW
- Linhas que nao cumprem -> ACTIVE (livres novamente)

Uso:
  cd /c/Users/zed/Desktop/fifa-bet-alert
  SCRIPT_B64=$(base64 -w0 < scripts/migrate_shadow_v2_reset.py)
  railway ssh "echo $SCRIPT_B64 | base64 -d > /tmp/_m.py && cd /app && PYTHONPATH=/app python /tmp/_m.py; rm /tmp/_m.py"
"""
import asyncio
from sqlalchemy import text
from src.db.database import async_session_factory, init_db


async def main():
    await init_db()
    async with async_session_factory() as s:
        # Snapshot do estado atual pra log
        before = await s.execute(text(
            "SELECT state, COUNT(*) FROM blocked_lines GROUP BY state"
        ))
        snapshot = {row[0]: row[1] for row in before.all()}
        print(f"Antes do reset: {snapshot}")

        # Soft reset
        result = await s.execute(text("DELETE FROM blocked_lines"))
        rows_deleted = result.rowcount or 0
        await s.commit()
        print(f"Removidas {rows_deleted} entradas de blocked_lines.")
        print("Primeiro recompute (cron 5min) vai popular sob v2K-pl1n5.")


asyncio.run(main())
