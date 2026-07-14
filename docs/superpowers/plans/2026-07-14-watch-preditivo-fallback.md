# Watch Preditivo (Fallback) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enviar os pré-alertas (watch) M1/M2/M3 pelo horário PREVISTO da volta quando a BetsAPI não expõe a volta a tempo, tornando o pré-alerta imune à oscilação do `upcoming`.

**Architecture:** Caminho normal (via API) continua primário. Quando a volta não casa e vai pro `_pending`, agenda-se um watch preditivo por método que dorme até T-30s do horário previsto (início do G1 + mediana histórica); ao acordar, se a volta ainda não casou, monta um `return_match` sintético e emite o pré-alerta reusando a mesma lógica de emissão dos watch loops (extraída pra `_emit_watch_mN`). Trava por `(game1_id, metodo)` impede duplicata entre o preditivo e o watch real.

**Tech Stack:** Python 3.14, asyncio, SQLAlchemy 2.0 async, loguru, pydantic-settings, pytest-asyncio (auto).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-14-watch-preditivo-fallback-design.md`
- Escopo: fallback cobre M1/M2/M3. Destinos: **M1 → VIP** (`send_watch`, to_admin=False), **M2 → DM** (`send_watch`, to_admin=True), **M3 → DM** (`send_watch_v3`).
- Cobre só o **pré-alerta**. O alerta live com odds NÃO muda (depende de in-play).
- `horario_previsto = g1.started_at + mediana_offset`; disparo em `T - _WATCH_LEAD_SECONDS` (30s) do previsto.
- `mediana_offset`: mediana de `Match.time_between_games` sobre pares válidos (20–120 min); fallback `WATCH_RETURN_OFFSET_FALLBACK_MIN=58` se < 20 pares. Query eficiente (agregação), nunca N+1.
- Anti-duplicata: trava por `(game1_id, metodo)`. Preditivo cancelado se a volta casou antes; watch real não repete se preditivo já emitiu.
- Refatorações de extração (`_emit_watch_mN`) DEVEM preservar comportamento — cobertas por regressão (suíte completa verde).
- `WATCH_PREDICTIVE_ENABLED=false` ⇒ comportamento idêntico ao atual (nenhum preditivo agendado).
- Nenhuma mudança no comportamento de M1/M2/M3 live. Convenções: ruff line-length 100, comentários pt, loguru. NÃO fazer git push até a task de deploy.

---

### Task 1: Estimador do offset da volta + flags de config

**Files:**
- Create: `src/core/return_offset.py`
- Modify: `src/config.py`
- Test: `tests/test_return_offset.py`

**Interfaces:**
- Consumes: `MatchRepository` (sessão via `self._session()`), modelo `Match` (`time_between_games`, `is_return_match`).
- Produces:
  - `async def estimate_return_offset_minutes(match_repo, *, fallback_min: float, min_sample: int = 20) -> float` — mediana de `time_between_games` dos pares de volta válidos (valores entre 20 e 120); retorna `fallback_min` se amostra < `min_sample`.
  - settings novas: `watch_predictive_enabled: bool = True`, `watch_return_offset_fallback_min: float = 58.0`.

- [ ] **Step 1: Adicionar settings em `src/config.py`** (após o bloco M3, junto de `m3_min_odds`):

```python
    # Watch preditivo (fallback quando a API nao expoe a volta antes do kickoff)
    watch_predictive_enabled: bool = True
    watch_return_offset_fallback_min: float = 58.0  # offset default se sem historico
```

- [ ] **Step 2: Write the failing test** (`tests/test_return_offset.py`):

```python
"""Testes do estimador de offset G1->volta (mediana historica)."""

from unittest.mock import AsyncMock, MagicMock

from src.core.return_offset import estimate_return_offset_minutes


def _repo(values: list[int]):
    """match_repo cujo agregador retorna os time_between_games dados."""
    repo = MagicMock()
    repo.get_return_time_gaps = AsyncMock(return_value=values)
    return repo


async def test_mediana_com_amostra_suficiente():
    gaps = [50, 55, 60, 60, 65, 70] + [58] * 20  # 26 pares
    repo = _repo(gaps)
    off = await estimate_return_offset_minutes(repo, fallback_min=58.0)
    assert 55 <= off <= 60  # mediana proxima de 58


async def test_fallback_com_amostra_pequena():
    repo = _repo([55, 60, 62])  # so 3 pares < min_sample
    off = await estimate_return_offset_minutes(repo, fallback_min=58.0)
    assert off == 58.0


async def test_ignora_outliers_fora_de_20_120():
    # valores fora da faixa nao devem entrar (o repo ja filtra, mas garantimos)
    gaps = [5, 200] + [60] * 25
    repo = _repo(gaps)
    off = await estimate_return_offset_minutes(repo, fallback_min=58.0)
    assert 55 <= off <= 65
```

Nota pro executor: `get_return_time_gaps` é um método novo do `MatchRepository` (Step 3b) que retorna `list[int]` de `time_between_games` já filtrado por `is_return_match=True`, `time_between_games between 20 and 120`. O teste mocka esse método; a filtragem de faixa fica no SQL.

- [ ] **Step 3a: Implementar `src/core/return_offset.py`:**

```python
"""Estimador do intervalo tipico entre o G1 e a volta (mediana historica).

Usado pelo watch preditivo pra estimar quando a volta comeca quando a
BetsAPI nao expoe o jogo futuro a tempo.
"""

from __future__ import annotations

import statistics

from loguru import logger


async def estimate_return_offset_minutes(
    match_repo, *, fallback_min: float, min_sample: int = 20
) -> float:
    """Mediana de time_between_games dos pares de volta. Fallback se amostra rasa."""
    try:
        gaps = await match_repo.get_return_time_gaps(low=20, high=120)
    except Exception as e:
        logger.warning(f"estimate_return_offset falhou ({e}); usando fallback {fallback_min}")
        return fallback_min
    valid = [g for g in gaps if g is not None and 20 <= g <= 120]
    if len(valid) < min_sample:
        logger.info(
            f"Offset da volta: amostra {len(valid)} < {min_sample} — usando fallback "
            f"{fallback_min}min"
        )
        return fallback_min
    med = statistics.median(valid)
    logger.info(f"Offset da volta estimado: {med:.1f}min (n={len(valid)})")
    return float(med)
```

- [ ] **Step 3b: Adicionar `get_return_time_gaps` em `MatchRepository`** (`src/db/repositories.py`) — seguir o estilo `self._session()` real do arquivo:

```python
    async def get_return_time_gaps(self, low: int = 20, high: int = 120) -> list[int]:
        """time_between_games dos pares de volta, filtrado pela faixa [low, high]."""
        async with self._session() as session:
            stmt = select(Match.time_between_games).where(
                Match.is_return_match == True,  # noqa: E712
                Match.time_between_games.is_not(None),
                Match.time_between_games >= low,
                Match.time_between_games <= high,
            )
            return [r for r in (await session.execute(stmt)).scalars().all()]
```

- [ ] **Step 4: Rodar testes**

Run: `python -m pytest tests/test_return_offset.py -v`
Expected: 3 PASSED

- [ ] **Step 5: Commit**

```bash
git add src/core/return_offset.py src/config.py src/db/repositories.py tests/test_return_offset.py
git commit -m "feat(watch): estimador de offset G1->volta (mediana) + flags preditivo"
```

---

### Task 2: Builder do return_match sintético

**Files:**
- Create: `src/core/synthetic_match.py`
- Test: `tests/test_synthetic_match.py`

**Interfaces:**
- Consumes: um objeto `game1_match` com `player_home/away`, `team_home/away`, `id`; um `datetime` previsto.
- Produces: `build_synthetic_return(game1_match, started_at: datetime) -> SyntheticReturnMatch` — objeto com atributos `id=None`, `player_home`, `player_away` (invertidos do G1), `team_home`, `team_away` (invertidos), `started_at`, `is_return_match=True`, `api_event_id=None`, `score_home=None`, `score_away=None`, `game1_id` (=game1_match.id, pra chavear).

- [ ] **Step 1: Write the failing test** (`tests/test_synthetic_match.py`):

```python
"""Testes do builder de return_match sintetico pro watch preditivo."""

from datetime import datetime
from unittest.mock import MagicMock

from src.core.synthetic_match import build_synthetic_return


def _g1():
    g1 = MagicMock()
    g1.id = 999
    g1.player_home = "Sena"; g1.player_away = "Bosko"
    g1.team_home = "Barcelona"; g1.team_away = "Real Madrid"
    return g1


def test_inverte_jogadores_e_times_e_seta_horario():
    prev = datetime(2026, 7, 14, 18, 7)
    r = build_synthetic_return(_g1(), prev)
    assert r.id is None
    assert r.player_home == "Bosko" and r.player_away == "Sena"   # invertidos
    assert r.team_home == "Real Madrid" and r.team_away == "Barcelona"
    assert r.started_at == prev
    assert r.is_return_match is True
    assert r.game1_id == 999
    assert r.score_home is None and r.score_away is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_synthetic_match.py -v`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Implementar `src/core/synthetic_match.py`:**

```python
"""return_match sintetico pro watch preditivo (quando a API nao expoe a volta).

Nao eh persistido no DB — existe so pra alimentar predict_watch_candidate /
send_watch* com os campos que eles consomem. A volta troca o mando, entao os
jogadores/times sao invertidos em relacao ao G1.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class SyntheticReturnMatch:
    id: None
    game1_id: int
    player_home: str | None
    player_away: str | None
    team_home: str | None
    team_away: str | None
    started_at: datetime
    is_return_match: bool = True
    api_event_id: None = None
    score_home: None = None
    score_away: None = None


def build_synthetic_return(game1_match, started_at: datetime) -> SyntheticReturnMatch:
    return SyntheticReturnMatch(
        id=None,
        game1_id=game1_match.id,
        player_home=game1_match.player_away,   # invertido: volta troca mando
        player_away=game1_match.player_home,
        team_home=game1_match.team_away,
        team_away=game1_match.team_home,
        started_at=started_at,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_synthetic_match.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/core/synthetic_match.py tests/test_synthetic_match.py
git commit -m "feat(watch): builder de return_match sintetico"
```

---

### Task 3: Extrair `_emit_watch_m1` do `_watch_loop` (M1)

**Files:**
- Modify: `src/core/odds_monitor.py` (`_watch_loop`)
- Test: suíte completa (regressão) — refatoração de comportamento preservado

**Interfaces:**
- Produces: `async def _emit_watch_m1(self, return_match, game1_match, loser: str, winner: str, loser_goals_g1: int) -> bool` — TODA a lógica pós-guard do `_watch_loop` atual (predict_watch_candidate M1, checagem SHADOW, tier H2H por linha, montar `watch_data`, `notifier.send_watch(...)`). Retorna True se emitiu. NÃO faz sleep nem checagem de kickoff (isso fica no chamador).
- Consome: `self.alert_engine.stats`, `self.alert_engine.notifier`, `self.alert_engine.blocked`/`alerts`, `self._WATCH_AUTO_DELETE_SECONDS`.

- [ ] **Step 1: Ler o `_watch_loop` atual inteiro** e identificar a fronteira: tudo APÓS o guard de kickoff (`if (kickoff - now2)... return`) até o envio `send_watch(...)` é o corpo a extrair. O sleep e o guard de kickoff PERMANECEM no `_watch_loop`.

- [ ] **Step 2: Criar `_emit_watch_m1`** movendo o corpo (predict → SHADOW → tier → watch_data → send_watch) verbatim pra o novo método, recebendo `return_match, game1_match, loser, winner, loser_goals_g1` como parâmetros. Usar `return_match.id` só onde já se usa; onde precisar de chave e `return_match.id is None` (caso sintético), usar `getattr(return_match, 'game1_id', None) or return_match.id`. `send_watch` continua `to_admin=False` (VIP).

- [ ] **Step 3: `_watch_loop` passa a chamar `_emit_watch_m1`** após o guard de kickoff:

```python
            await self._emit_watch_m1(return_match, game1_match, loser, winner, loser_goals_g1)
```

- [ ] **Step 4: Rodar a suíte completa (regressão)**

Run: `python -m pytest`
Expected: todos PASSED (nenhuma mudança de comportamento). Smoke: `python -c "import src.core.odds_monitor"`.

- [ ] **Step 5: Commit**

```bash
git add src/core/odds_monitor.py
git commit -m "refactor(watch): extrair _emit_watch_m1 do _watch_loop (M1)"
```

---

### Task 4: Extrair `_emit_watch_m2` do `_watch_loop_v2`

**Files:**
- Modify: `src/core/odds_monitor.py` (`_watch_loop_v2`)
- Test: suíte completa (regressão)

**Interfaces:**
- Produces: `async def _emit_watch_m2(self, return_match, game1_match, loser, winner, loser_goals_g1) -> bool` — corpo pós-guard do `_watch_loop_v2` (predict_watch_candidate M2 via `self.alert_engine_v2.stats`, SHADOW v2, tier v2, watch_data com `method="M2"`, `notifier.send_watch(watch_data, auto_delete_seconds=..., to_admin=True)`). **Mantém `to_admin=True`** (DM do owner).

- [ ] **Step 1: Ler `_watch_loop_v2`**, identificar fronteira (após guard de kickoff até `send_watch(..., to_admin=True)`).

- [ ] **Step 2: Criar `_emit_watch_m2`** movendo o corpo verbatim; parâmetros idem Task 3; preservar `to_admin=True` e o `method="M2"` no watch_data.

- [ ] **Step 3: `_watch_loop_v2` chama `_emit_watch_m2`** após o guard.

- [ ] **Step 4: Suíte completa**

Run: `python -m pytest`
Expected: todos PASSED. Smoke import OK.

- [ ] **Step 5: Commit**

```bash
git add src/core/odds_monitor.py
git commit -m "refactor(watch): extrair _emit_watch_m2 do _watch_loop_v2 (DM)"
```

---

### Task 5: Extrair `_emit_watch_m3` do `_watch_loop_v3`

**Files:**
- Modify: `src/core/odds_monitor.py` (`_watch_loop_v3`)
- Test: suíte completa (regressão)

**Interfaces:**
- Produces: `async def _emit_watch_m3(self, return_match, game1_match, loser, winner) -> bool` — corpo pós-guard do `_watch_loop_v3` (`self.alert_engine_v3.stats.evaluate(loser, winner)`, montar `watch_data` com as linhas M3, `notifier.send_watch_v3(watch_data, auto_delete_seconds=...)`). M3 não usa `loser_goals_g1` (assinatura sem ele, como o `_watch_loop_v3` atual).

- [ ] **Step 1: Ler `_watch_loop_v3`**, identificar fronteira (após guard de kickoff até `send_watch_v3(...)`).

- [ ] **Step 2: Criar `_emit_watch_m3`** movendo o corpo verbatim; parâmetros `return_match, game1_match, loser, winner`.

- [ ] **Step 3: `_watch_loop_v3` chama `_emit_watch_m3`** após o guard.

- [ ] **Step 4: Suíte completa**

Run: `python -m pytest`
Expected: todos PASSED. Smoke import OK.

- [ ] **Step 5: Commit**

```bash
git add src/core/odds_monitor.py
git commit -m "refactor(watch): extrair _emit_watch_m3 do _watch_loop_v3 (DM)"
```

---

### Task 6: Loop preditivo no OddsMonitor

**Files:**
- Modify: `src/core/odds_monitor.py` (`__init__`, novos métodos)
- Test: `tests/test_predictive_watch.py`

**Interfaces:**
- Consumes: `_emit_watch_m1/m2/m3` (Tasks 3-5); `build_synthetic_return` (Task 2); `settings.watch_predictive_enabled`; `self._WATCH_LEAD_SECONDS`.
- Produces:
  - `__init__`: `self._predictive_offset_min: float = 58.0` (setado pelo main); `self._predictive_tasks: dict[int, asyncio.Task]` (chave game1_id); `self._predictive_sent: set[tuple[int, str]]` (game1_id, metodo).
  - `def schedule_predictive_watch(self, game1_match, loser, winner, loser_goals_g1) -> None` — agenda a task preditiva (no-op se `not settings.watch_predictive_enabled`, se já agendada, ou se sem `game1_match.started_at`).
  - `def cancel_predictive_watch(self, game1_id: int) -> None` — cancela a task preditiva (chamado quando a volta casa via API).
  - `async def _predictive_watch_loop(self, game1_match, loser, winner, loser_goals_g1) -> None` — dorme até T-30s do previsto; se a volta já casou (par presente em `self._task_meta` por game1_id, ver Step) aborta; monta sintético; chama os 3 `_emit_watch_mN` guardados por `_predictive_sent`.

- [ ] **Step 1: Write the failing test** (`tests/test_predictive_watch.py`) — testa a decisão de emitir e a trava, com `_emit_*` e sleep mockados:

```python
"""Testes do watch preditivo (fallback quando a volta nao casou via API)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.odds_monitor import OddsMonitor


def _monitor():
    m = OddsMonitor(MagicMock(), MagicMock(), MagicMock(),
                    alert_engine_v2=MagicMock(), alert_engine_v3=MagicMock())
    m._predictive_offset_min = 58.0
    m._emit_watch_m1 = AsyncMock(return_value=True)
    m._emit_watch_m2 = AsyncMock(return_value=True)
    m._emit_watch_m3 = AsyncMock(return_value=True)
    return m


def _g1(started_min_ago=57):
    g1 = MagicMock()
    g1.id = 111
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    g1.started_at = now - timedelta(minutes=started_min_ago)
    g1.player_home = "Sena"; g1.player_away = "Bosko"
    g1.team_home = "A"; g1.team_away = "B"
    return g1


async def test_emite_os_tres_metodos_quando_volta_nao_casou(monkeypatch):
    monkeypatch.setattr("asyncio.sleep", AsyncMock())  # nao dormir
    m = _monitor()
    # volta NAO casou: game1_id nao esta em _task_meta
    await m._predictive_watch_loop(_g1(), "Sena", "Bosko", 2)
    m._emit_watch_m1.assert_awaited_once()
    m._emit_watch_m2.assert_awaited_once()
    m._emit_watch_m3.assert_awaited_once()
    assert (111, "m1") in m._predictive_sent


async def test_aborta_se_volta_ja_casou(monkeypatch):
    monkeypatch.setattr("asyncio.sleep", AsyncMock())
    m = _monitor()
    # volta casou: registrar o par por game1_id
    m._task_meta[555] = {"game1_id": 111}
    await m._predictive_watch_loop(_g1(), "Sena", "Bosko", 2)
    m._emit_watch_m1.assert_not_awaited()
    m._emit_watch_m3.assert_not_awaited()


def test_schedule_noop_quando_desabilitado(monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "watch_predictive_enabled", False)
    m = _monitor()
    m.schedule_predictive_watch(_g1(), "Sena", "Bosko", 2)
    assert 111 not in m._predictive_tasks
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_predictive_watch.py -v`
Expected: FAIL (métodos não existem)

- [ ] **Step 3: Implementar no `OddsMonitor`.** No `__init__` (após `_watch_v3_tasks`):

```python
        self._predictive_offset_min: float = settings.watch_return_offset_fallback_min
        self._predictive_tasks: dict[int, asyncio.Task] = {}  # game1_id → task
        self._predictive_sent: set[tuple[int, str]] = set()   # (game1_id, metodo)
```
(garantir `from src.config import settings` no topo do módulo)

Métodos novos:

```python
    def _return_ja_casou(self, game1_id: int) -> bool:
        """True se a volta desse G1 ja esta sendo monitorada (casou via API)."""
        for meta in self._task_meta.values():
            if meta.get("game1_id") == game1_id:
                return True
        return False

    def schedule_predictive_watch(self, game1_match, loser, winner, loser_goals_g1) -> None:
        if not settings.watch_predictive_enabled:
            return
        gid = game1_match.id
        if gid in self._predictive_tasks or game1_match.started_at is None:
            return
        task = asyncio.create_task(
            self._predictive_watch_loop(game1_match, loser, winner, loser_goals_g1),
            name=f"predictive_watch_{gid}",
        )
        self._predictive_tasks[gid] = task

    def cancel_predictive_watch(self, game1_id: int) -> None:
        t = self._predictive_tasks.pop(game1_id, None)
        if t and not t.done():
            t.cancel()

    async def _predictive_watch_loop(self, game1_match, loser, winner, loser_goals_g1) -> None:
        from datetime import datetime, timezone
        from src.core.synthetic_match import build_synthetic_return
        gid = game1_match.id
        try:
            started = game1_match.started_at
            if started.tzinfo is not None:
                started = started.replace(tzinfo=None)
            previsto = started + timedelta(minutes=self._predictive_offset_min)
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            wait = (previsto - now).total_seconds() - self._WATCH_LEAD_SECONDS
            if wait > 0:
                await asyncio.sleep(wait)
            # Se a volta casou via API nesse meio tempo, o watch real cuida.
            if self._return_ja_casou(gid):
                logger.info(f"WatchPreditivo {gid}: volta casou via API — abortando preditivo")
                return
            synth = build_synthetic_return(game1_match, previsto)
            logger.info(
                f"WatchPreditivo {gid} DISPARANDO ({loser} vs {winner}) — "
                f"API nao expos a volta; usando horario previsto {previsto:%H:%M}"
            )
            # M1 (VIP), M2 (DM), M3 (DM) — cada um so uma vez
            for metodo, emit, engine in [
                ("m1", self._emit_watch_m1, self.alert_engine),
                ("m2", self._emit_watch_m2, self.alert_engine_v2),
                ("m3", self._emit_watch_m3, self.alert_engine_v3),
            ]:
                if engine is None or (gid, metodo) in self._predictive_sent:
                    continue
                try:
                    if metodo == "m3":
                        await emit(synth, game1_match, loser, winner)
                    else:
                        await emit(synth, game1_match, loser, winner, loser_goals_g1)
                    self._predictive_sent.add((gid, metodo))
                except Exception as e:
                    logger.warning(f"WatchPreditivo {gid} {metodo} erro: {e}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"WatchPreditivo {gid} loop erro: {e!r}")
        finally:
            self._predictive_tasks.pop(gid, None)
```

Nota pro executor: confirmar que `_task_meta` guarda `game1_id`. O `start_monitoring` atual guarda `self._task_meta[match_id] = {"game1_match": game1_match, "loser": loser}`. Ajustar pra incluir `"game1_id": game1_match.id` (Step 3b), pra o `_return_ja_casou` funcionar.

- [ ] **Step 3b: Em `start_monitoring`**, incluir `game1_id` no meta:

```python
        self._task_meta[match_id] = {"game1_match": game1_match, "loser": loser,
                                     "game1_id": game1_match.id}
```

- [ ] **Step 4: Guardar `_emit_watch_mN` contra re-emissão do watch real** — nos `_emit_watch_m1/m2/m3` (Tasks 3-5), no início, checar a trava e marcar:

```python
        gid = getattr(return_match, "game1_id", None) or game1_match.id
        if (gid, "m1") in self._predictive_sent:   # m2/m3 nos respectivos
            return False
        # ... apos enviar com sucesso:
        self._predictive_sent.add((gid, "m1"))
```
(Isso faz o watch real e o preditivo se excluírem mutuamente. Aplicar o par certo em cada `_emit`.)

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_predictive_watch.py -v && python -m pytest`
Expected: novos PASSED + suíte completa verde.

- [ ] **Step 6: Commit**

```bash
git add src/core/odds_monitor.py tests/test_predictive_watch.py
git commit -m "feat(watch): loop preditivo (fallback) no OddsMonitor + trava anti-duplicata"
```

---

### Task 7: Agendar/cancelar preditivo a partir do pair_matcher

**Files:**
- Modify: `src/core/pair_matcher.py` (`_add_pending`, e o ponto do `link_pair`/`start_monitoring`)
- Test: `tests/test_pair_predictive_hook.py`

**Interfaces:**
- Consumes: `odds_monitor.schedule_predictive_watch(game1_match, loser, winner, loser_goals_g1)` e `odds_monitor.cancel_predictive_watch(game1_id)` (Task 6).
- Produces: quando a volta NÃO casa (`_add_pending`), agenda o preditivo; quando casa (antes de `start_monitoring`), cancela o preditivo pendente.

- [ ] **Step 1: Write the failing test** (`tests/test_pair_predictive_hook.py`):

```python
"""Hook: pair_matcher agenda preditivo no pending e cancela ao casar."""

from unittest.mock import MagicMock

from src.core.pair_matcher import PairMatcher


def _pm():
    om = MagicMock()
    pm = PairMatcher(MagicMock(), MagicMock(), om)
    return pm, om


def _g1():
    g1 = MagicMock(); g1.id = 111
    g1.player_home = "Sena"; g1.player_away = "Bosko"
    return g1


def test_add_pending_agenda_preditivo():
    pm, om = _pm()
    pm._add_pending(_g1(), "Sena", "Bosko", {"sena", "bosko"}, "22614", 2)
    om.schedule_predictive_watch.assert_called_once()
```

(O teste de cancelamento ao casar é coberto por inspeção do caminho `link`+`start_monitoring`; se viável, adicionar um teste de que `cancel_predictive_watch(game1.id)` é chamado antes de `start_monitoring`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_pair_predictive_hook.py -v`
Expected: FAIL (schedule_predictive_watch não é chamado)

- [ ] **Step 3: Em `_add_pending`** (após registrar no `_pending`), agendar:

```python
        # Fallback preditivo: se a API nao expuser a volta a tempo, o watch
        # sai pelo horario previsto. No-op se desabilitado.
        try:
            self.odds_monitor.schedule_predictive_watch(
                game1_match, loser, winner, loser_goals_g1
            )
        except Exception as e:
            logger.warning(f"schedule_predictive_watch falhou p/ game1={game1_match.id}: {e}")
```

- [ ] **Step 4: Nos dois pontos onde a volta casa** (`_match_from_candidates` ~linha 272 e o segundo caminho de link ~linha 512), ANTES de `start_monitoring`, cancelar o preditivo pendente:

```python
        self.odds_monitor.cancel_predictive_watch(game1_match.id)
```
(colocar junto do `self._pending.pop(game1_match.id, None)` existente)

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_pair_predictive_hook.py -v && python -m pytest`
Expected: PASSED + suíte verde.

- [ ] **Step 6: Commit**

```bash
git add src/core/pair_matcher.py tests/test_pair_predictive_hook.py
git commit -m "feat(watch): pair_matcher agenda preditivo no pending, cancela ao casar"
```

---

### Task 8: Wiring no main.py + deploy

**Files:**
- Modify: `src/main.py`
- Test: suíte completa + smoke + verificação em produção

- [ ] **Step 1: No `src/main.py`**, após construir o `odds_monitor` e o `MatchRepository`, computar e injetar o offset:

```python
    from src.core.return_offset import estimate_return_offset_minutes
    if settings.watch_predictive_enabled:
        odds_monitor._predictive_offset_min = await estimate_return_offset_minutes(
            MatchRepository(sf),
            fallback_min=settings.watch_return_offset_fallback_min,
        )
        logger.info(f"Watch preditivo ON (offset={odds_monitor._predictive_offset_min:.1f}min)")
    else:
        logger.info("Watch preditivo OFF (WATCH_PREDICTIVE_ENABLED=false)")
```

- [ ] **Step 2: `.env` local** — adicionar:

```
WATCH_PREDICTIVE_ENABLED=true
WATCH_RETURN_OFFSET_FALLBACK_MIN=58
```

- [ ] **Step 3: Suíte completa + smoke import**

Run: `python -m pytest && python -c "import src.main"`
Expected: todos PASSED, import OK.

- [ ] **Step 4: Commit**

```bash
git add src/main.py
git commit -m "feat(watch): wiring do preditivo no main (offset no startup)"
```

- [ ] **Step 5: Env vars em produção ANTES do push**

```bash
cd /c/Users/Plini/fifa-bet-alert && railway variables --set "WATCH_PREDICTIVE_ENABLED=true" --set "WATCH_RETURN_OFFSET_FALLBACK_MIN=58" --skip-deploys
```

- [ ] **Step 6: Push (dispara deploy)**

```bash
git push origin master
```

- [ ] **Step 7: Verificar produção**

```bash
railway logs -n 100 | grep -E "Watch preditivo|WatchPreditivo|Offset da volta"
```
Expected: `Watch preditivo ON (offset=...)` no startup; nas rodadas seguintes, `WatchPreditivo <id> DISPARANDO` quando a API não expõe a volta, ou nada quando o caminho normal casa a volta a tempo.

---

## Verificação final (pós-deploy)

1. Startup loga `Watch preditivo ON (offset=~58min)`.
2. Quando o `upcoming` está vazio e a volta não casa, `WatchPreditivo DISPARANDO` e o pré-alerta M1 chega no VIP (+ M2/M3 no DM) ~T-30s do horário previsto.
3. Quando a API casa a volta a tempo, o preditivo loga "volta casou via API — abortando" e NÃO duplica.
4. Nenhum pré-alerta duplicado em nenhum cenário.
5. `WATCH_PREDICTIVE_ENABLED=false` restaura o comportamento anterior.
