"""Roda recompute_all_states uma vez e imprime relatorio. Manual."""
import asyncio

from src.db.database import async_session_factory, init_db
from src.db.repositories import BlockedLineRepository
from src.core.blocked_lines import recompute_all_states, get_status


async def main() -> None:
    await init_db()
    repo = BlockedLineRepository(async_session_factory)

    print("=== recompute_all_states ===")
    transitions = await recompute_all_states(repo)
    for k, items in transitions.items():
        print(f"\n{k}: {len(items)}")
        for it in items[:30]:
            print(f"  {it}")
        if len(items) > 30:
            print(f"  ... +{len(items)-30} mais")

    print("\n\n=== STATUS DAS LINHAS BLOQUEADAS ===")
    statuses = await get_status(repo)
    if not statuses:
        print("  (nenhuma linha bloqueada)")
    for s in statuses:
        print(
            f"\n{s['state']:<10} {s['player']:<14} {s['line']:<7} "
            f"strikes={s['block_count']}"
        )
        print(
            f"  PL_total = {s['pl_total']:+.2f}u em {s['n_total']} alertas"
        )
        if s["state"] == "SHADOW":
            print(
                f"  shadow desde {s['shadow_start_at']} (PL inicial = "
                f"{s['shadow_start_pl']:+.2f}u)"
            )
            print(
                f"  shadow_pl = {s['shadow_pl']:+.2f}u em {s['shadow_n']} alertas | "
                f"precisa >= {s['needed_pl']:+.2f}u E n >= {s['needed_n']}"
            )


asyncio.run(main())
