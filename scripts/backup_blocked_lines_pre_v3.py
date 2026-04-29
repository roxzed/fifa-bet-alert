"""Dump completo da tabela blocked_lines atual (v2K) pra backup pre-v3.

Saida: JSON via stdout. Capturar localmente pra arquivo.
"""
import asyncio
import json
from sqlalchemy import text
from src.db.database import async_session_factory, init_db


async def main():
    await init_db()
    async with async_session_factory() as s:
        r = await s.execute(text("SELECT * FROM blocked_lines"))
        rows = []
        for row in r.all():
            rows.append({k: (v.isoformat() if hasattr(v, "isoformat") else
                             (float(v) if isinstance(v, (int, float)) and
                              not isinstance(v, bool) else v))
                         for k, v in row._mapping.items()})
        print("===JSON_START===")
        print(json.dumps({"rows": rows, "count": len(rows)},
                         indent=2, default=str))
        print("===JSON_END===")


asyncio.run(main())
