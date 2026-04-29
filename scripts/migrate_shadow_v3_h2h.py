"""Migration v3 H2H granular (2026-04-29).

Transforma blocked_lines de PK (player, line) -> (player, line, opponent).

Estrategia (idempotente):
1. Adiciona coluna `opponent` com default '' (nullable=False)
2. Drop PRIMARY KEY (player, line) atual
3. Recria PRIMARY KEY (player, line, opponent)
4. Cria indices novos (player, line) e (opponent)
5. DELETE FROM blocked_lines (soft reset — primeiro recompute v3 popula do zero)

Nao destrutivo: tabela alerts (com suppressed=true) fica intacta.
Linhas atualmente bloqueadas no v2K sao removidas; primeiro recompute (cron 5min)
recomputa todas as cells (player, line, opp) sob nova politica.

Uso:
  cd /c/Users/zed/Desktop/fifa-bet-alert
  SCRIPT_B64=$(base64 -w0 < scripts/migrate_shadow_v3_h2h.py)
  railway ssh "echo $SCRIPT_B64 | base64 -d > /tmp/_m.py && cd /app && PYTHONPATH=/app python /tmp/_m.py; rm /tmp/_m.py"
"""
import asyncio
from sqlalchemy import text
from src.db.database import async_session_factory, init_db


async def main():
    await init_db()
    async with async_session_factory() as s:
        # Snapshot pre-migration
        before = await s.execute(text(
            "SELECT state, COUNT(*) FROM blocked_lines GROUP BY state"
        ))
        snapshot = {row[0]: row[1] for row in before.all()}
        print(f"Antes: {snapshot}")

        # Verificar se a coluna opponent ja existe
        col_check = await s.execute(text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name='blocked_lines' AND column_name='opponent'
        """))
        has_opponent = col_check.scalar_one_or_none() is not None

        if not has_opponent:
            print("Adicionando coluna 'opponent'...")
            await s.execute(text(
                "ALTER TABLE blocked_lines ADD COLUMN opponent VARCHAR NOT NULL DEFAULT ''"
            ))
            print("  coluna 'opponent' adicionada com default ''")
        else:
            print("Coluna 'opponent' ja existe — skip ALTER")

        # Drop PK existente (qualquer que seja a forma — Postgres usa name automatico)
        # E recriar com 3 colunas
        print("Recriando PRIMARY KEY pra incluir opponent...")
        try:
            await s.execute(text("""
                ALTER TABLE blocked_lines
                  DROP CONSTRAINT IF EXISTS blocked_lines_pkey
            """))
            await s.execute(text("""
                ALTER TABLE blocked_lines
                  ADD CONSTRAINT blocked_lines_pkey
                  PRIMARY KEY (player, line, opponent)
            """))
            print("  PK reconstruida")
        except Exception as e:
            print(f"  AVISO PK: {e} (pode ser ja correta)")

        # Indices novos (idempotentes)
        await s.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_blocked_lines_player_line "
            "ON blocked_lines (player, line)"
        ))
        await s.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_blocked_lines_opponent "
            "ON blocked_lines (opponent)"
        ))
        print("Indices criados/garantidos")

        # SOFT RESET — primeiro recompute v3 popula do zero
        result = await s.execute(text("DELETE FROM blocked_lines"))
        rows_deleted = result.rowcount or 0
        await s.commit()

        print(f"DELETE FROM blocked_lines — {rows_deleted} entradas removidas")
        print("Primeiro recompute (cron 5min) vai popular sob v3 H2H granular")
        print("Schema final: PK = (player, line, opponent)")


asyncio.run(main())
