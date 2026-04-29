"""Rollback de SHADOW v3 H2H granular -> v2K (player, line).

EXECUTAR EM PROD APENAS DEPOIS DO `git revert` E REDEPLOY.

Etapas:
1. DROP TABLE blocked_lines (estado v3 inconsistente com v2K)
2. CREATE TABLE blocked_lines com schema v2K (PK = player, line)
3. Restore das 12 linhas pre-v3 do JSON backup

Uso:
  cd /c/Users/zed/Desktop/fifa-bet-alert
  # 1) git revert do commit v3
  git revert <hash-do-commit-v3>
  git push origin master
  # aguardar redeploy

  # 2) rodar este rollback
  SCRIPT_B64=$(base64 -w0 < scripts/rollback_shadow_v3.py)
  BACKUP_B64=$(base64 -w0 < backups/blocked_lines-pre-v3-2026-04-29.json)
  railway ssh "
    echo $BACKUP_B64 | base64 -d > /tmp/_backup.json &&
    echo $SCRIPT_B64 | base64 -d > /tmp/_r.py &&
    cd /app && PYTHONPATH=/app python /tmp/_r.py /tmp/_backup.json;
    rm /tmp/_r.py /tmp/_backup.json
  "
"""
import asyncio
import json
import sys
from datetime import datetime
from sqlalchemy import text
from src.db.database import async_session_factory, init_db


async def main(backup_path: str):
    with open(backup_path, "r") as f:
        backup = json.load(f)
    rows_backup = backup["rows"]
    print(f"Backup carregado: {len(rows_backup)} linhas")

    await init_db()
    async with async_session_factory() as s:
        # 1. Drop tabela (cascade pra remover indices/constraints)
        print("Dropping blocked_lines (estado v3)...")
        await s.execute(text("DROP TABLE IF EXISTS blocked_lines CASCADE"))

        # 2. Recriar com schema v2K (PK player, line — sem opponent)
        print("Recriando blocked_lines schema v2K...")
        await s.execute(text("""
            CREATE TABLE blocked_lines (
                player VARCHAR NOT NULL,
                line VARCHAR NOT NULL,
                state VARCHAR NOT NULL DEFAULT 'ACTIVE',
                block_count INTEGER NOT NULL DEFAULT 0,
                shadow_start_pl FLOAT,
                shadow_start_at TIMESTAMP,
                last_block_at TIMESTAMP,
                last_unblock_at TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
                PRIMARY KEY (player, line)
            )
        """))
        await s.execute(text(
            "CREATE INDEX ix_blocked_lines_state ON blocked_lines (state)"
        ))

        # 3. Restore das linhas
        print(f"Restaurando {len(rows_backup)} linhas...")
        for r in rows_backup:
            await s.execute(text("""
                INSERT INTO blocked_lines (
                    player, line, state, block_count,
                    shadow_start_pl, shadow_start_at,
                    last_block_at, last_unblock_at, updated_at
                ) VALUES (
                    :player, :line, :state, :block_count,
                    :shadow_start_pl,
                    :shadow_start_at,
                    :last_block_at,
                    :last_unblock_at,
                    NOW()
                )
            """), {
                "player": r["player"],
                "line": r["line"],
                "state": r["state"],
                "block_count": r.get("block_count", 0),
                "shadow_start_pl": r.get("shadow_start_pl"),
                "shadow_start_at": r.get("shadow_start_at"),
                "last_block_at": r.get("last_block_at"),
                "last_unblock_at": r.get("last_unblock_at"),
            })

        await s.commit()
        print("ROLLBACK COMPLETO - schema v2K restaurado")
        # Verificar
        v = await s.execute(text("SELECT state, COUNT(*) FROM blocked_lines GROUP BY state"))
        for row in v.all():
            print(f"  {row[0]}: {row[1]}")


if __name__ == "__main__":
    backup_path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/_backup.json"
    asyncio.run(main(backup_path))
