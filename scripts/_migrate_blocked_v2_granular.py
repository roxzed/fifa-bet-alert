"""Migration: recria blocked_lines_v2 com coluna opponent na PK.

ANTES: PK (player, line)
DEPOIS: PK (player, line, opponent)

A tabela e re-computada automaticamente pelo recompute_all_states() do M2,
entao e seguro dropar e recriar — nenhum dado critico e perdido.
"""
import asyncio
from sqlalchemy import text
from src.db.database import get_session


RECREATE_SQL = """
DROP TABLE IF EXISTS blocked_lines_v2;

CREATE TABLE blocked_lines_v2 (
    player          VARCHAR NOT NULL,
    line            VARCHAR NOT NULL,
    opponent        VARCHAR NOT NULL DEFAULT '',
    state           VARCHAR NOT NULL DEFAULT 'ACTIVE',
    block_count     INTEGER NOT NULL DEFAULT 0,
    shadow_start_pl FLOAT,
    shadow_start_at TIMESTAMP,
    last_block_at   TIMESTAMP,
    last_unblock_at TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (player, line, opponent)
);

CREATE INDEX ix_blocked_lines_v2_state        ON blocked_lines_v2 (state);
CREATE INDEX ix_blocked_lines_v2_player_line  ON blocked_lines_v2 (player, line);
CREATE INDEX ix_blocked_lines_v2_opponent     ON blocked_lines_v2 (opponent);
"""


async def main():
    async with get_session() as s:
        await s.execute(text(RECREATE_SQL))
        await s.commit()
    print("OK: blocked_lines_v2 recriada com PK (player, line, opponent).")
    print("Execute recompute_all_states() no proximo ciclo para repopular.")


if __name__ == "__main__":
    asyncio.run(main())
