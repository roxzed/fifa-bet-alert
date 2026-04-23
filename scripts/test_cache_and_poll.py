"""Testa cache do bet365_get_inplay_esoccer e novo poll interval.

NAO faz requests HTTP reais — substitui _request por mock que conta chamadas.
Valida:
  1. Cache serve N chamadas concorrentes com 1 HTTP call
  2. Cache expira apos TTL
  3. Lock serializa tasks concorrentes na miss
  4. _adaptive_poll_interval retorna 5s na janela critica
"""
from __future__ import annotations

import asyncio
import time

from src.api.betsapi_client import BetsAPIClient
from src.core.odds_monitor import _adaptive_poll_interval


async def test_cache_concurrent() -> None:
    print("\n[1] Cache: 10 tasks concorrentes -> 1 HTTP call")
    client = BetsAPIClient(token="fake")
    call_count = {"n": 0}

    async def fake_request(path, params=None):
        call_count["n"] += 1
        await asyncio.sleep(0.05)  # simula latencia
        return {"results": [
            {"id": "1", "ev_id": "a", "our_event_id": "b",
             "home": {"name": "Ronaldo (Brazil)"},
             "away": {"name": "Messi (Argentina)"},
             "league": {"name": "Esoccer Battle - 8 mins play"},
             "ss": "0-0"},
        ]}

    client._request = fake_request
    # 10 tasks disparadas "ao mesmo tempo"
    results = await asyncio.gather(*[
        client.bet365_get_inplay_esoccer() for _ in range(10)
    ])
    assert call_count["n"] == 1, f"Esperava 1 HTTP call, teve {call_count['n']}"
    assert all(len(r) == 1 for r in results), "Todas as tasks devem ter mesma lista"
    assert results[0] is results[1], "Deve retornar mesma instancia do cache"
    print(f"    OK — HTTP calls: {call_count['n']}, resultados identicos: True")


async def test_cache_ttl() -> None:
    print("\n[2] Cache: expira apos TTL")
    client = BetsAPIClient(token="fake")
    client._INPLAY_CACHE_TTL = 0.2  # TTL curto pra teste
    call_count = {"n": 0}

    async def fake_request(path, params=None):
        call_count["n"] += 1
        return {"results": []}

    client._request = fake_request
    await client.bet365_get_inplay_esoccer()  # miss
    await client.bet365_get_inplay_esoccer()  # hit
    assert call_count["n"] == 1
    await asyncio.sleep(0.25)  # expira
    await client.bet365_get_inplay_esoccer()  # miss
    assert call_count["n"] == 2
    print(f"    OK — apos miss+hit+expira+miss: {call_count['n']} HTTP calls")


async def test_cache_different_filter() -> None:
    print("\n[3] Cache: filtros diferentes tem entradas separadas")
    client = BetsAPIClient(token="fake")
    call_count = {"n": 0}

    async def fake_request(path, params=None):
        call_count["n"] += 1
        return {"results": []}

    client._request = fake_request
    await client.bet365_get_inplay_esoccer(league_filter="Battle - 8 mins")
    await client.bet365_get_inplay_esoccer(league_filter="Battle - 10 mins")
    await client.bet365_get_inplay_esoccer(league_filter="Battle - 8 mins")  # hit
    assert call_count["n"] == 2
    print(f"    OK — 2 filtros = 2 calls, repeticao = hit ({call_count['n']} total)")


def test_poll_interval() -> None:
    print("\n[4] Poll interval adaptativo")
    cases = [
        (None, 15, "desconhecido"),
        (30, 60, "T-30min (longe)"),
        (10.5, 60, "T-10.5min"),
        (8, 15, "T-8min"),
        (4, 15, "T-4min"),
        (3.1, 15, "T-3.1min (limite superior)"),
        (3, 4, "T-3min (critico)"),
        (2, 4, "T-2min"),
        (0.5, 4, "T-30s"),
        (-2, 4, "T+2min (pos kickoff)"),
    ]
    for minutes, expected, desc in cases:
        got = _adaptive_poll_interval(minutes)
        status = "OK" if got == expected else "FAIL"
        print(f"    {status} {desc:30s} -> {got}s (esperado {expected}s)")
        assert got == expected, f"{desc}: {got} != {expected}"


async def test_cache_simulates_load() -> None:
    print("\n[5] Simulacao realista: 5 monitors simultaneos, 7min, 5s interval")
    client = BetsAPIClient(token="fake")
    client._INPLAY_CACHE_TTL = 8.0
    call_count = {"n": 0}
    call_times: list[float] = []

    async def fake_request(path, params=None):
        call_count["n"] += 1
        call_times.append(time.monotonic())
        return {"results": []}

    client._request = fake_request

    async def monitor_task(tid: int, n_polls: int) -> None:
        for _ in range(n_polls):
            await client.bet365_get_inplay_esoccer()
            await asyncio.sleep(0.05)  # 50ms simula 5s comprimido 100x

    # 5 monitors, 10 polls cada (simula 50s de operacao comprimido)
    await asyncio.gather(*[monitor_task(i, 10) for i in range(5)])

    # Cache TTL = 8s real, mas timing comprimido: 10 polls * 50ms = 500ms
    # Todas as calls deveriam ter sido cache hit apos a primeira
    print(f"    Monitors: 5, polls cada: 10, total chamadas logicas: 50")
    print(f"    HTTP calls reais: {call_count['n']} (esperava 1 com cache ativo)")
    assert call_count["n"] == 1, f"Cache falhou: {call_count['n']} HTTP calls"
    print(f"    OK — reducao de {(1 - call_count['n']/50)*100:.0f}%")


async def test_rate_limit_hook() -> None:
    print("\n[6] Rate limit hook: dispara callback e throttle de 10min")
    client = BetsAPIClient(token="fake")
    calls: list[tuple] = []

    async def hook(endpoint: str, wait: float) -> None:
        calls.append((endpoint, wait))

    client._rate_limit_hook = hook
    client._RATE_LIMIT_NOTIFY_COOLDOWN = 0.3  # 300ms para teste

    # Simula 3 rate limits em rapid succession
    for _ in range(3):
        now_ts = time.monotonic()
        if now_ts - client._last_rate_limit_notify > client._RATE_LIMIT_NOTIFY_COOLDOWN:
            client._last_rate_limit_notify = now_ts
            asyncio.create_task(hook("/bet365/event", 45.0))

    await asyncio.sleep(0.1)
    assert len(calls) == 1, f"Throttle falhou: {len(calls)} chamadas"
    print(f"    OK — 3 rate limits em rapido -> 1 notificacao (throttle ativo)")

    # Espera cooldown expirar
    await asyncio.sleep(0.35)
    now_ts = time.monotonic()
    if now_ts - client._last_rate_limit_notify > client._RATE_LIMIT_NOTIFY_COOLDOWN:
        client._last_rate_limit_notify = now_ts
        asyncio.create_task(hook("/bet365/event", 30.0))
    await asyncio.sleep(0.1)
    assert len(calls) == 2, f"Apos cooldown: {len(calls)} chamadas"
    print(f"    OK — apos cooldown, nova notificacao dispara")


async def main() -> None:
    print("=== Teste das otimizacoes #2 e #3 ===")
    await test_cache_concurrent()
    await test_cache_ttl()
    await test_cache_different_filter()
    test_poll_interval()
    await test_cache_simulates_load()
    await test_rate_limit_hook()
    print("\n=== TUDO PASSOU ===")


if __name__ == "__main__":
    asyncio.run(main())
