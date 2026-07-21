# Modelo FREE Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rodar o critério do M3 como modelo público no grupo FREE: pré-alerta T-30s (linha de maior taxa), odd mínima 1.70 monitorada ao vivo o jogo inteiro, e resultado GREEN/RED/ANULADO editando a mensagem.

**Architecture:** Fluxo isolado espelhando o M3 (`AlertEngineV3`/`ValidatorV3`). Reusa `StatsEngineV3` pro critério; grava numa tabela própria `alerts_free`; hooks aditivos no `odds_monitor` (pré-alerta T-30s + rastreamento da odd da linha durante o in-play). Ligável por `FREE_MODEL_ENABLED`.

**Tech Stack:** Python 3.14, SQLAlchemy 2.0 async, python-telegram-bot, loguru, pydantic-settings, pytest-asyncio (auto).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-07-20-modelo-free-design.md`
- Pré-alerta **T-30s** antes do kickoff, no grupo FREE (`TELEGRAM_FREE_GROUP_ID`), com **UMA linha — a de maior `rate`** do `StatsEngineV3`.
- **Odd mínima 1.70** (`free_min_odd=1.70`), monitorada **o jogo inteiro** (até `ended`, não para após gol). `entry_odd` = 1ª odd ≥1.70 vista; `max_odd` = maior odd vista.
- Resultado: odd atingiu ≥1.70 **e** bateu → GREEN; ≥1.70 **e** não bateu → RED; nunca ≥1.70 → **VOID** (fora do placar green/red).
- **Mensagem SEMPRE exibe "odd mínima 1.70"**.
- **PÚBLICO — NUNCA revelar o método**: proibido "volta", "G1", "G2", "jogo de volta", "perdedor", edge/EV/matemática na copy do FREE. Só: jogador, linha over, horário, odd mínima 1.70, resultado.
- Sem limite de volume (todos que qualificam).
- Nenhuma mudança no comportamento de M1/M2/M3 nem no `send_alert_free` atual (M1 filtrado). Isolado, aditivo, `FREE_MODEL_ENABLED=false` = zero efeito.
- Convenções: async I/O, loguru, ruff line-length 100, comentários pt. NÃO fazer git push até a task de deploy.

---

### Task 1: Config + modelo AlertFree + repositório

**Files:**
- Modify: `src/config.py`
- Modify: `src/db/models.py` (nova classe `AlertFree`, tabela `alerts_free`)
- Modify: `src/db/repositories.py` (`AlertFreeRepository`; `MatchRepository.get_unvalidated_return_matches_free`)
- Test: `tests/test_alert_free_repo.py`

**Interfaces:**
- Produces:
  - settings: `free_model_enabled: bool = False`, `free_min_odd: float = 1.70`
  - modelo `AlertFree` (tabela `alerts_free`): `id`, `match_id:int`, `losing_player:str`, `opponent_player:str|None`, `game1_score:str`, `line:str`, `rate:float`, `hits:int`, `n_h2h:int`, `recent_hits:int`, `entry_odd:float|None`, `max_odd:float|None`, `status:str` (default "pending"), `telegram_message_id:int|None`, `sent_at:datetime`, `actual_goals:int|None`, `hit:bool|None`, `validated_at:datetime|None`
  - `AlertFreeRepository(session_factory)`: `create(**kwargs)->AlertFree`, `exists_for_match(match_id)->bool`, `update_odds(alert_id, entry_odd, max_odd)`, `update_telegram_message_id(alert_id, message_id)`, `validate(alert_id, actual_goals, hit, status)->AlertFree|None`, `get_all_by_match_id(match_id)->Sequence[AlertFree]`, `get_validated_since(since)->Sequence[AlertFree]`
  - `MatchRepository.get_unvalidated_return_matches_free()->list[Match]` (espelha `_v3`)

- [ ] **Step 1: settings em `src/config.py`** (após o bloco M3):

```python
    # Modelo FREE (M3 publico no grupo gratis)
    free_model_enabled: bool = False   # vazio/false = modelo FREE desligado
    free_min_odd: float = 1.70         # odd minima pra entrada valida (senao ANULADO)
```

- [ ] **Step 2: Write the failing test** (`tests/test_alert_free_repo.py`):

```python
"""Testes do AlertFreeRepository."""
from src.db.database import Database
from src.db.repositories import AlertFreeRepository


async def _repo(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/free.db")
    await db.create_tables()
    return AlertFreeRepository(db.session_factory)


async def test_create_update_odds_e_validate(tmp_path):
    repo = await _repo(tmp_path)
    a = await repo.create(
        match_id=1, losing_player="Sena", opponent_player="Bosko",
        game1_score="1-3", line="over15", rate=0.80, hits=16, n_h2h=20, recent_hits=6,
    )
    assert a.id is not None and a.status == "pending"
    assert await repo.exists_for_match(1) is True
    await repo.update_odds(a.id, entry_odd=1.75, max_odd=2.10)
    await repo.update_telegram_message_id(a.id, 555)
    v = await repo.validate(a.id, actual_goals=3, hit=True, status="green")
    assert v.status == "green" and v.entry_odd == 1.75 and v.telegram_message_id == 555


async def test_get_validated_since(tmp_path):
    from datetime import datetime, timedelta
    repo = await _repo(tmp_path)
    a = await repo.create(match_id=2, losing_player="X", opponent_player="Y",
                          game1_score="0-2", line="over25", rate=0.7, hits=14, n_h2h=20, recent_hits=5)
    await repo.validate(a.id, actual_goals=3, hit=True, status="green")
    assert len(await repo.get_validated_since(datetime.utcnow()-timedelta(days=1))) == 1
```

Nota pro executor: confira o padrão real de `Database`/`create_tables`/`_session()` em `tests/test_m3_repository.py` e `src/db/repositories.py` (a classe `AlertV3Repository` é o modelo a espelhar). Ajuste os helpers do teste ao padrão real.

- [ ] **Step 3: Run test to verify it fails**

Run: `python -m pytest tests/test_alert_free_repo.py -v`
Expected: FAIL (ImportError AlertFreeRepository)

- [ ] **Step 4: Implementar** — `AlertFree` em `src/db/models.py` espelhando `AlertV3` (mesmos idioms: `Mapped`/`mapped_column`, `sent_at` com `server_default=func.now()`), adicionando `entry_odd`, `max_odd` (Float nullable) e `status` (String default "pending"). `AlertFreeRepository` em `src/db/repositories.py` espelhando `AlertV3Repository` (usa `self._session()`), com os métodos da interface. `validate` seta `actual_goals`, `hit`, `status`, `validated_at=datetime.utcnow()`. `get_unvalidated_return_matches_free` espelha `get_unvalidated_return_matches_v3` trocando `AlertV3`→`AlertFree`.

- [ ] **Step 5: Run tests + suíte**

Run: `python -m pytest tests/test_alert_free_repo.py -v && python -m pytest -q`
Expected: novos PASSED, suíte verde.

- [ ] **Step 6: Commit**

```bash
git add src/config.py src/db/models.py src/db/repositories.py tests/test_alert_free_repo.py
git commit -m "feat(free): modelo AlertFree + repositorio + flags"
```

---

### Task 2: Regra de status (pura) + mensagens + envios Telegram

**Files:**
- Create: `src/core/free_status.py`
- Modify: `src/telegram/messages.py` (`format_free_prealert`, `format_free_result`)
- Modify: `src/telegram/bot.py` (`send_watch_free`, `edit_free_result`)
- Test: `tests/test_free_status.py`, `tests/test_free_telegram.py`

**Interfaces:**
- Consumes: `_free_group_id`, `_breaker_free` (já existem no `TelegramNotifier`); `settings.free_min_odd`.
- Produces:
  - `free_status.py`: `LINE_LABELS = {"over15":"Over 1.5","over25":"Over 2.5","over35":"Over 3.5","over45":"Over 4.5"}`; `LINE_THRESH = {"over15":1.5,"over25":2.5,"over35":3.5,"over45":4.5}`; `def decide_status(entry_odd: float|None, loser_goals: int, line: str, min_odd: float) -> tuple[str,bool|None]` → retorna `("void", None)` se `entry_odd is None` (nunca ≥min_odd); senão `("green", True)` se `loser_goals > LINE_THRESH[line]`, `("red", False)` caso contrário.
  - `messages.py`: `format_free_prealert(d: dict) -> str` (chaves: `player`, `line_label`, `kickoff_str`) e `format_free_result(d: dict, status: str) -> str` (chaves: `player`, `line_label`, `actual_goals`, `entry_odd`).
  - `bot.py`: `async send_watch_free(data: dict, auto_delete_seconds: int = 0) -> int|None` (pré-alerta pro `_free_group_id`; sem auto-delete por padrão — a mensagem é editada com o resultado); `async edit_free_result(message_id: int, data: dict, status: str) -> bool`.

- [ ] **Step 1: Write failing tests** (`tests/test_free_status.py`):

```python
from src.core.free_status import decide_status

def test_void_quando_odd_nunca_atingiu_minimo():
    assert decide_status(None, 3, "over15", 1.70) == ("void", None)

def test_green_com_entrada_valida_e_bateu():
    # over15 = >1.5; 3 gols bate
    assert decide_status(1.75, 3, "over15", 1.70) == ("green", True)

def test_red_com_entrada_valida_e_nao_bateu():
    # over25 = >2.5; 2 gols nao bate
    assert decide_status(1.90, 2, "over25", 1.70) == ("red", False)
```

`tests/test_free_telegram.py`:

```python
from unittest.mock import AsyncMock, MagicMock
from src.telegram.bot import TelegramNotifier
from src.telegram.messages import format_free_prealert, format_free_result

FORBIDDEN = ["volta", "g1", "g2", "perdedor", "edge", "ev "]

def _notifier():
    n = TelegramNotifier(token="1:x", chat_id="-100vip", free_group_id="-100free")
    n.bot = MagicMock(); m = MagicMock(); m.message_id = 77
    n.bot.send_message = AsyncMock(return_value=m); n.bot.edit_message_text = AsyncMock()
    return n

def _data():
    return {"player": "Sena", "line_label": "Over 1.5", "kickoff_str": "19:43",
            "actual_goals": 3, "entry_odd": 1.75}

def test_prealert_tem_odd_minima_170_e_nao_revela_metodo():
    t = format_free_prealert(_data()).lower()
    assert "1.70" in t
    assert not any(f in t for f in FORBIDDEN)

def test_result_green_mostra_entrada():
    t = format_free_result(_data(), "green")
    assert "GREEN" in t and "1.75" in t

async def test_send_watch_free_vai_pro_free_group():
    n = _notifier()
    mid = await n.send_watch_free(_data())
    assert mid == 77
    assert n.bot.send_message.await_args.kwargs["chat_id"] == "-100free"

async def test_send_watch_free_noop_sem_free_group():
    n = _notifier(); n._free_group_id = ""
    assert await n.send_watch_free(_data()) is None
    n.bot.send_message.assert_not_awaited()
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest tests/test_free_status.py tests/test_free_telegram.py -v`
Expected: FAIL (ModuleNotFoundError / ImportError)

- [ ] **Step 3: Implementar `src/core/free_status.py`:**

```python
"""Regra de resultado do Modelo FREE (pura, sem I/O)."""

from __future__ import annotations

LINE_LABELS = {"over15": "Over 1.5", "over25": "Over 2.5",
               "over35": "Over 3.5", "over45": "Over 4.5"}
LINE_THRESH = {"over15": 1.5, "over25": 2.5, "over35": 3.5, "over45": 4.5}


def decide_status(
    entry_odd: float | None, loser_goals: int, line: str, min_odd: float
) -> tuple[str, bool | None]:
    """VOID se a odd nunca atingiu min_odd (entry_odd None). Senao GREEN/RED
    conforme os gols do jogador batem a linha."""
    if entry_odd is None:
        return ("void", None)
    hit = loser_goals > LINE_THRESH[line]
    return ("green", True) if hit else ("red", False)
```

- [ ] **Step 4: Implementar mensagens em `src/telegram/messages.py`** (copy pública, `html.escape` nos nomes, seguindo o estilo dos formatters existentes):

```python
def format_free_prealert(d: dict) -> str:
    """Pre-alerta publico FREE — SEM revelar metodo. Sempre 'odd minima 1.70'."""
    import html as _html
    p = _html.escape(str(d.get("player") or "?"))
    return (
        f"🔥 <b>ENTRADA FIFA eSports</b>\n"
        f"🎮 {p}  —  <b>{d.get('line_label')} gols</b>\n"
        f"⏰ Jogo às {d.get('kickoff_str', '?')}\n"
        f"💰 <b>Odd mínima: 1.70</b>\n"
        f"<i>Fique atento e entre quando a odd chegar em 1.70+</i>"
    )


def format_free_result(d: dict, status: str) -> str:
    """Edita o pre-alerta com o resultado. status: green|red|void."""
    import html as _html
    p = _html.escape(str(d.get("player") or "?"))
    lbl = d.get("line_label"); g = d.get("actual_goals")
    odd = d.get("entry_odd")
    if status == "void":
        return (
            f"⚪ <b>ANULADO</b> — {p} {lbl}\n"
            f"A odd não atingiu 1.70 (sem entrada)."
        )
    head = "✅ <b>GREEN</b>" if status == "green" else "❌ <b>RED</b>"
    return (
        f"{head} — {p} {lbl}\n"
        f"🎯 {p} fez {g} gols  |  entrada @ odd {odd:.2f}"
    )
```

- [ ] **Step 5: Implementar envios em `src/telegram/bot.py`** (espelhar `send_alert_free`/`edit_alert_free_result`; usar `_free_group_id`, `_breaker_free`, `_sanitize_text`, `ParseMode.HTML`):

```python
    async def send_watch_free(self, data: dict, auto_delete_seconds: int = 0) -> int | None:
        """Pre-alerta do Modelo FREE no grupo gratis. Editado depois com resultado."""
        if not self._free_group_id or self._paused:
            return None
        if not self._breaker_free.allow_request():
            logger.warning("FREE breaker OPEN — skip send_watch_free")
            return None
        from src.telegram.messages import format_free_prealert
        text = _sanitize_text(format_free_prealert(data))
        try:
            msg = await self.bot.send_message(
                chat_id=self._free_group_id, text=text,
                parse_mode=ParseMode.HTML, disable_web_page_preview=True,
            )
            self._breaker_free.record_success()
            logger.bind(category="free_model").info(
                f"FREE pre-alerta: {data.get('player')} {data.get('line_label')}"
            )
            return msg.message_id
        except TelegramError as e:
            self._breaker_free.record_failure()
            logger.error(f"Failed to send FREE pre-alerta: {e}")
            return None

    async def edit_free_result(self, message_id: int, data: dict, status: str) -> bool:
        """Edita o pre-alerta FREE com GREEN/RED/ANULADO."""
        if not self._free_group_id:
            return False
        from src.telegram.messages import format_free_result
        text = _sanitize_text(format_free_result(data, status))
        try:
            await self.bot.edit_message_text(
                chat_id=self._free_group_id, message_id=message_id, text=text,
                parse_mode=ParseMode.HTML, disable_web_page_preview=True,
            )
            self._breaker_free.record_success()
            return True
        except TelegramError as e:
            self._breaker_free.record_failure()
            logger.warning(f"Failed to edit FREE result {message_id}: {e}")
            return False
```

- [ ] **Step 6: Run tests + suíte**

Run: `python -m pytest tests/test_free_status.py tests/test_free_telegram.py -v && python -m pytest -q`
Expected: PASSED + suíte verde.

- [ ] **Step 7: Commit**

```bash
git add src/core/free_status.py src/telegram/messages.py src/telegram/bot.py tests/test_free_status.py tests/test_free_telegram.py
git commit -m "feat(free): regra de status + mensagens publicas + envios Telegram"
```

---

### Task 3: AlertEngineFree (pré-alerta com a linha de maior taxa)

**Files:**
- Create: `src/core/alert_engine_free.py`
- Test: `tests/test_alert_engine_free.py`

**Interfaces:**
- Consumes: `StatsEngineV3.evaluate(loser, opponent) -> EvaluationV3` (`.lines[LineEvalV3: line/rate/hits/n/recent_hits]`, `.should_alert`); `AlertFreeRepository` (Task 1); `TelegramNotifier.send_watch_free` (Task 2); `free_status.LINE_LABELS`.
- Produces: `AlertEngineFree(stats_engine_v3, alert_free_repo, notifier)` com
  `async def prealert(self, return_match, game1_match, loser, winner, kickoff_str: str) -> tuple[int, str] | None` — avalia; se `should_alert`, escolhe a linha de **maior rate**, cria `AlertFree` (status pending), envia `send_watch_free`, grava `telegram_message_id`, retorna `(match_id_ignored, line)`... na prática retorna a `line` escolhida (pra o odds_monitor saber qual odd rastrear) ou `None`. Assinatura final: retorna `str | None` (a `line` alertada) — `None` se não alertou. Dedup: `exists_for_match(return_match.id)`.

- [ ] **Step 1: Write failing tests** (`tests/test_alert_engine_free.py`):

```python
from unittest.mock import AsyncMock, MagicMock
from src.core.alert_engine_free import AlertEngineFree
from src.core.stats_engine_v3 import EvaluationV3, LineEvalV3


def _line(line, rate):
    return LineEvalV3(line=line, threshold=0, hits=int(rate*20), n=20, rate=rate,
                      recent_hits=6, recent_n=7, qualified=True)


def _engine(ev, exists=False):
    stats = MagicMock(); stats.evaluate = AsyncMock(return_value=ev)
    repo = MagicMock(); repo.exists_for_match = AsyncMock(return_value=exists)
    created = MagicMock(); created.id = 1; repo.create = AsyncMock(return_value=created)
    repo.update_telegram_message_id = AsyncMock()
    notifier = MagicMock(); notifier.send_watch_free = AsyncMock(return_value=88)
    return AlertEngineFree(stats, repo, notifier), repo, notifier


def _m():
    g2 = MagicMock(); g2.id = 5; g2.player_home = "Sena"; g2.player_away = "Bosko"
    g1 = MagicMock(); g1.player_home = "Bosko"; g1.player_away = "Sena"
    g1.score_home = 3; g1.score_away = 1
    return g2, g1


async def test_escolhe_linha_de_maior_taxa():
    ev = EvaluationV3(should_alert=True, lines=[_line("over25",0.70), _line("over15",0.85)], n_h2h=20)
    eng, repo, notifier = _engine(ev)
    g2, g1 = _m()
    line = await eng.prealert(g2, g1, "Sena", "Bosko", "19:43")
    assert line == "over15"  # maior rate
    assert repo.create.await_args.kwargs["line"] == "over15"
    notifier.send_watch_free.assert_awaited_once()

async def test_nao_alerta_sem_should_alert():
    ev = EvaluationV3(should_alert=False, reason="x")
    eng, repo, notifier = _engine(ev)
    g2, g1 = _m()
    assert await eng.prealert(g2, g1, "Sena", "Bosko", "19:43") is None
    notifier.send_watch_free.assert_not_awaited()

async def test_dedup_por_match():
    ev = EvaluationV3(should_alert=True, lines=[_line("over15",0.85)], n_h2h=20)
    eng, repo, notifier = _engine(ev, exists=True)
    g2, g1 = _m()
    assert await eng.prealert(g2, g1, "Sena", "Bosko", "19:43") is None
    repo.create.assert_not_awaited()
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest tests/test_alert_engine_free.py -v`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Implementar `src/core/alert_engine_free.py`:**

```python
"""Modelo FREE — orquestra o pre-alerta publico (linha de maior taxa)."""

from __future__ import annotations

from loguru import logger

from src.core.free_status import LINE_LABELS


class AlertEngineFree:
    def __init__(self, stats_engine_v3, alert_free_repo, notifier) -> None:
        self.stats = stats_engine_v3
        self.alerts = alert_free_repo
        self.notifier = notifier

    async def prealert(
        self, return_match, game1_match, loser: str, winner: str, kickoff_str: str
    ) -> str | None:
        """Se o criterio M3 qualifica, envia pre-alerta FREE da linha de maior
        taxa. Retorna a `line` alertada (pra o monitor rastrear a odd) ou None."""
        if await self.alerts.exists_for_match(return_match.id):
            return None
        evaluation = await self.stats.evaluate(loser, winner)
        if not evaluation.should_alert or not evaluation.lines:
            return None
        best = max(evaluation.lines, key=lambda le: le.rate)
        if game1_match.player_home == loser:
            g1_score = f"{game1_match.score_home}-{game1_match.score_away}"
        else:
            g1_score = f"{game1_match.score_away}-{game1_match.score_home}"
        alert = await self.alerts.create(
            match_id=return_match.id, losing_player=loser, opponent_player=winner,
            game1_score=g1_score, line=best.line, rate=best.rate, hits=best.hits,
            n_h2h=best.n, recent_hits=best.recent_hits,
        )
        data = {"player": loser, "line_label": LINE_LABELS[best.line], "kickoff_str": kickoff_str}
        msg_id = await self.notifier.send_watch_free(data)
        if msg_id:
            await self.alerts.update_telegram_message_id(alert.id, msg_id)
        logger.info(f"FREE pre-alerta {loser} {best.line} rate={best.rate:.0%} (match {return_match.id})")
        return best.line
```

- [ ] **Step 4: Run tests + suíte**

Run: `python -m pytest tests/test_alert_engine_free.py -v && python -m pytest -q`
Expected: 3 PASSED + suíte verde.

- [ ] **Step 5: Commit**

```bash
git add src/core/alert_engine_free.py tests/test_alert_engine_free.py
git commit -m "feat(free): AlertEngineFree — pre-alerta da linha de maior taxa"
```

---

### Task 4: Hooks no OddsMonitor (pré-alerta T-30s + rastreamento da odd)

**Files:**
- Modify: `src/core/odds_monitor.py`
- Test: suíte completa (regressão) + `tests/test_free_odds_tracking.py`

**Interfaces:**
- Consumes: `AlertEngineFree.prealert(...)` (Task 3); `AlertFreeRepository.update_odds` (Task 1); `settings.free_min_odd`.
- Produces:
  - `__init__`: param `free_engine=None`; `self.free_engine = free_engine`; `self._free_tracking: dict[int, dict] = {}` (match_id → {line, entry_odd, max_odd}); `self._watch_free_tasks: dict[int, asyncio.Task] = {}`; constante `_WATCH_FREE_LEAD_SECONDS: int = 30`.
  - `_watch_loop_free(return_match, game1_match, loser, winner)`: dorme até T-30s do kickoff; se kickoff passou aborta; chama `free_engine.prealert(...)`; se retornar uma `line`, registra `self._free_tracking[match_id] = {"line": line, "entry_odd": None, "max_odd": 0.0}`.
  - método `_track_free_odd(self, match_id, over15, over25, over35, over45)`: se `match_id` em `_free_tracking`, pega a odd da `line` rastreada; atualiza `max_odd`; grava `entry_odd` na 1ª vez que odd ≥ `settings.free_min_odd`.
  - chamada de `_track_free_odd(...)` dentro do `_monitor_loop` (após extrair as odds), e persistência via `AlertFreeRepository.update_odds` — o `update_odds` é chamado quando o jogo encerra (no cleanup do monitor) OU a cada atualização de entry_odd. Detalhe no Step 3.

- [ ] **Step 1: `__init__`** — adicionar (após os dicts do preditivo/v3):

```python
        self.free_engine = free_engine
        self._free_tracking: dict[int, dict] = {}   # match_id -> {line, entry_odd, max_odd}
        self._watch_free_tasks: dict[int, asyncio.Task] = {}
```
e a constante de classe junto das outras: `_WATCH_FREE_LEAD_SECONDS: int = 30`.
Adicionar `free_engine=None` na assinatura do `__init__`.

- [ ] **Step 2: `_track_free_odd`** (método novo) + teste (`tests/test_free_odds_tracking.py`):

```python
from unittest.mock import MagicMock
from src.core.odds_monitor import OddsMonitor

def _mon():
    return OddsMonitor(MagicMock(), MagicMock(), MagicMock())

def test_grava_entry_odd_na_primeira_vez_acima_de_170():
    m = _mon(); m._free_tracking[9] = {"line": "over15", "entry_odd": None, "max_odd": 0.0}
    m._track_free_odd(9, over15=1.55, over25=None, over35=None, over45=None)
    assert m._free_tracking[9]["entry_odd"] is None       # 1.55 < 1.70
    assert m._free_tracking[9]["max_odd"] == 1.55
    m._track_free_odd(9, over15=1.80, over25=None, over35=None, over45=None)
    assert m._free_tracking[9]["entry_odd"] == 1.80       # 1a vez >= 1.70
    assert m._free_tracking[9]["max_odd"] == 1.80
    m._track_free_odd(9, over15=1.72, over25=None, over35=None, over45=None)
    assert m._free_tracking[9]["entry_odd"] == 1.80       # nao sobrescreve
    assert m._free_tracking[9]["max_odd"] == 1.80         # max mantem o maior

def test_ignora_match_sem_tracking():
    m = _mon()
    m._track_free_odd(1, over15=2.0, over25=None, over35=None, over45=None)  # nao lanca
    assert 1 not in m._free_tracking
```

Implementação:

```python
    def _track_free_odd(self, match_id, over15, over25, over35, over45) -> None:
        """Atualiza entry_odd (1a vez >= free_min_odd) e max_odd da linha FREE."""
        tr = self._free_tracking.get(match_id)
        if tr is None:
            return
        odd = {"over15": over15, "over25": over25, "over35": over35,
               "over45": over45}.get(tr["line"])
        if odd is None or odd <= 0:
            return
        if odd > (tr["max_odd"] or 0):
            tr["max_odd"] = odd
        if tr["entry_odd"] is None and odd >= settings.free_min_odd:
            tr["entry_odd"] = odd
```
(garantir `from src.config import settings` no topo do módulo)

- [ ] **Step 3: Chamar `_track_free_odd` no `_monitor_loop`** — logo após as odds serem extraídas (onde `over15_odds`/`over25_odds`/... já existem, perto do log "Bet365 odds for"):

```python
                    if self.free_engine:
                        self._track_free_odd(match_id, over15_odds, over25_odds, over35_odds, over45_odds)
```
E persistir as odds no `AlertFree` quando o monitor encerra o match (no ponto onde o loop sai / cleanup do match, junto do pop das tasks): se `match_id` em `_free_tracking`, chamar (best-effort, try/except):
```python
                    # ao encerrar o monitoramento do match:
                    tr = self._free_tracking.pop(match_id, None)
                    if tr and self.free_engine:
                        try:
                            alerts = await self.free_engine.alerts.get_all_by_match_id(match_id)
                            for a in alerts:
                                await self.free_engine.alerts.update_odds(a.id, tr["entry_odd"], tr["max_odd"])
                        except Exception as e:
                            logger.warning(f"FREE update_odds falhou match={match_id}: {e}")
```
Nota pro executor: posicionar essa persistência no MESMO ponto onde o `_monitor_loop` já faz cleanup ao detectar fim de jogo (procure o `break`/fim do while e o local onde `self._tasks.pop`/`finally` acontece). Ler o `_monitor_loop` real pra achar o ponto certo.

- [ ] **Step 4: Agendar `_watch_loop_free` em `start_monitoring`** — após o agendamento do watch M1 (padrão idêntico, com done-callback), guardado por `if self.free_engine and match_id not in self._watch_free_tasks:`. O `_watch_loop_free`:

```python
    async def _watch_loop_free(self, return_match, game1_match, loser: str, winner: str) -> None:
        match_id = return_match.id
        kickoff = return_match.started_at
        if kickoff is None:
            return
        try:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            wait = (kickoff - now).total_seconds() - self._WATCH_FREE_LEAD_SECONDS
            if wait > 0:
                await asyncio.sleep(wait)
            now2 = datetime.now(timezone.utc).replace(tzinfo=None)
            if (kickoff - now2).total_seconds() < 0:
                logger.info(f"WatchFREE {match_id}: kickoff ja passou, abortando")
                return
            from zoneinfo import ZoneInfo
            kickoff_brt = kickoff.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("America/Sao_Paulo"))
            line = await self.free_engine.prealert(
                return_match, game1_match, loser, winner, kickoff_brt.strftime("%H:%M")
            )
            if line:
                self._free_tracking[match_id] = {"line": line, "entry_odd": None, "max_odd": 0.0}
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"WatchFREE loop erro match {match_id}: {e!r}")
        finally:
            self._watch_free_tasks.pop(match_id, None)
```

- [ ] **Step 5: Cleanup** — onde `_watch_tasks`/`_watch_v3_tasks` são cancelados (fim de jogo e `stop()`), fazer o mesmo pra `_watch_free_tasks`.

- [ ] **Step 6: Rodar tests + suíte + smoke**

Run: `python -m pytest tests/test_free_odds_tracking.py -v && python -m pytest -q && python -c "import src.core.odds_monitor"`
Expected: PASSED + suíte verde + import OK.

- [ ] **Step 7: Commit**

```bash
git add src/core/odds_monitor.py tests/test_free_odds_tracking.py
git commit -m "feat(free): watch T-30s + rastreamento da odd (1a vez >=1.70) no OddsMonitor"
```

---

### Task 5: ValidatorFree

**Files:**
- Create: `src/core/validator_free.py`
- Test: `tests/test_validator_free.py`

**Interfaces:**
- Consumes: `MatchRepository.get_unvalidated_return_matches_free()` (Task 1); `AlertFreeRepository.get_all_by_match_id`/`validate` (Task 1); `TelegramNotifier.edit_free_result` (Task 2); `free_status.decide_status`, `LINE_LABELS`; `settings.free_min_odd`.
- Produces: `ValidatorFree(match_repo, alert_free_repo, notifier)` com `async start(poll_interval=60)` (loop bloqueante, espelha `ValidatorV3`), `stop()`, e `async validate_match(match)`:
  - pra cada `AlertFree` do match com `hit is None`: `loser_goals` = gols do `losing_player` no placar da volta; `status, hit = decide_status(entry_odd, loser_goals, line, settings.free_min_odd)`; `validate(id, actual_goals, hit, status)`; edita a mensagem via `edit_free_result(message_id, data, status)` (data: player, line_label, actual_goals, entry_odd).

- [ ] **Step 1: Write failing tests** (`tests/test_validator_free.py`):

```python
from unittest.mock import AsyncMock, MagicMock
from src.core.validator_free import ValidatorFree

def _alert(line="over15", entry_odd=1.75, hit=None):
    a = MagicMock(); a.id=1; a.line=line; a.entry_odd=entry_odd
    a.losing_player="Sena"; a.telegram_message_id=88; a.hit=hit
    return a

def _v(alerts):
    mr=MagicMock(); ar=MagicMock()
    ar.get_all_by_match_id=AsyncMock(return_value=alerts); ar.validate=AsyncMock()
    n=MagicMock(); n.edit_free_result=AsyncMock(return_value=True)
    return ValidatorFree(mr, ar, n), ar, n

def _match(home_goals=3):
    m=MagicMock(); m.id=5; m.player_home="Sena"; m.player_away="Bosko"
    m.score_home=home_goals; m.score_away=1
    return m

async def test_green_com_entrada_valida():
    v, ar, n = _v([_alert("over15", 1.75)])  # over1.5, 3 gols
    await v.validate_match(_match(3))
    assert ar.validate.await_args.kwargs["status"] == "green"
    n.edit_free_result.assert_awaited_once()

async def test_void_quando_entry_odd_none():
    v, ar, n = _v([_alert("over15", None)])
    await v.validate_match(_match(3))
    assert ar.validate.await_args.kwargs["status"] == "void"

async def test_red_com_entrada_valida_nao_bateu():
    v, ar, n = _v([_alert("over25", 1.90)])  # over2.5, 2 gols
    await v.validate_match(_match(2))
    assert ar.validate.await_args.kwargs["status"] == "red"

async def test_ignora_ja_validado():
    v, ar, n = _v([_alert("over15", 1.75, hit=True)])
    await v.validate_match(_match(3))
    ar.validate.assert_not_awaited()
```

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest tests/test_validator_free.py -v`
Expected: FAIL (ModuleNotFoundError)

- [ ] **Step 3: Implementar `src/core/validator_free.py`** (espelhar `ValidatorV3`: `start` bloqueante + `stop` síncrona):

```python
"""Modelo FREE — validacao pos-jogo: GREEN/RED/VOID + edicao da mensagem."""

from __future__ import annotations

import asyncio

from loguru import logger

from src.config import settings
from src.core.free_status import LINE_LABELS, decide_status


class ValidatorFree:
    def __init__(self, match_repo, alert_free_repo, notifier) -> None:
        self.matches = match_repo
        self.alerts = alert_free_repo
        self.notifier = notifier
        self._running = False

    async def start(self, poll_interval: int = 60) -> None:
        self._running = True
        logger.info("ValidatorFree started")
        while self._running:
            try:
                for match in await self.matches.get_unvalidated_return_matches_free():
                    await self.validate_match(match)
            except Exception as e:
                logger.error(f"ValidatorFree cycle error: {e}")
            await asyncio.sleep(poll_interval)

    def stop(self) -> None:
        self._running = False

    async def validate_match(self, match) -> None:
        pending = [a for a in await self.alerts.get_all_by_match_id(match.id) if a.hit is None]
        if not pending:
            return
        loser = pending[0].losing_player
        loser_goals = match.score_home if match.player_home == loser else match.score_away
        if loser_goals is None:
            return
        for a in pending:
            status, hit = decide_status(a.entry_odd, loser_goals, a.line, settings.free_min_odd)
            await self.alerts.validate(a.id, actual_goals=loser_goals, hit=hit, status=status)
            logger.bind(category="free_model").info(
                f"FREE validado: {loser} {a.line} -> {status.upper()} "
                f"({loser_goals} gols, entry_odd={a.entry_odd})"
            )
            if a.telegram_message_id:
                data = {"player": loser, "line_label": LINE_LABELS[a.line],
                        "actual_goals": loser_goals, "entry_odd": a.entry_odd}
                await self.notifier.edit_free_result(a.telegram_message_id, data, status)
```

- [ ] **Step 4: Run tests + suíte**

Run: `python -m pytest tests/test_validator_free.py -v && python -m pytest -q`
Expected: 4 PASSED + suíte verde.

- [ ] **Step 5: Commit**

```bash
git add src/core/validator_free.py tests/test_validator_free.py
git commit -m "feat(free): ValidatorFree — GREEN/RED/VOID + edicao da mensagem"
```

---

### Task 6: Relatório diário FREE (tips + resultado total)

**Files:**
- Modify: `src/core/reporter.py` (`send_daily_report_free`)
- Modify: `src/telegram/bot.py` (`send_free_raw` — mensagem crua pro grupo FREE)
- Test: `tests/test_free_report.py`

**Interfaces:**
- Consumes: `AlertFreeRepository.get_validated_since(since)` (Task 1); `_free_group_id`/`_breaker_free`.
- Produces:
  - `TelegramNotifier.send_free_raw(text: str) -> int | None` — envia texto cru pro `_free_group_id` (padrão do `send_message_v3_raw`).
  - `Reporter.send_daily_report_free() -> None` — resumo do dia no grupo FREE: lista das tips (jogador + linha + resultado) e o total (greens/reds, anulados à parte, e P&L pela `entry_odd` real). NO-OP se `alert_free_repo` None. Copy pública, sem revelar método.

- [ ] **Step 1: Write the failing tests** (`tests/test_free_report.py`):

```python
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock
from src.core.reporter import Reporter

def _al(player, line, status, entry_odd, goals):
    a = MagicMock(); a.losing_player=player; a.line=line; a.status=status
    a.entry_odd=entry_odd; a.actual_goals=goals; a.hit=(status=="green")
    return a

def _reporter(alerts):
    repo = MagicMock(); repo.get_validated_since = AsyncMock(return_value=alerts)
    notifier = MagicMock(); notifier.send_free_raw = AsyncMock(return_value=1)
    return Reporter(alert_repo=MagicMock(), player_repo=MagicMock(),
                    method_stats_repo=MagicMock(), notifier=notifier,
                    alert_free_repo=repo), notifier

async def test_relatorio_free_agrega_green_red_e_ignora_void():
    alerts = [_al("Sena","over15","green",1.75,3), _al("Bosko","over25","red",1.90,2),
              _al("X","over15","green",1.80,4), _al("Y","over15","void",None,3)]
    r, n = _reporter(alerts)
    await r.send_daily_report_free()
    n.send_free_raw.assert_awaited_once()
    t = n.send_free_raw.await_args.args[0]
    assert "2" in t and "GREEN" in t.upper()   # 2 greens
    # nao revela metodo
    for f in ["volta","g1","g2","perdedor"]:
        assert f not in t.lower()

async def test_free_report_noop_sem_repo():
    r = Reporter(alert_repo=MagicMock(), player_repo=MagicMock(),
                 method_stats_repo=MagicMock(), notifier=MagicMock(), alert_free_repo=None)
    await r.send_daily_report_free()  # nao lanca, nao envia
```

Nota: confirme a assinatura real do `Reporter.__init__` e adicione o param `alert_free_repo=None` (mesmo padrão do `alert_v3_repo` que o M3 usou). Se o Reporter atual não tem `alert_v3_repo`/`notifier` nesses nomes, siga os nomes reais.

- [ ] **Step 2: Run to verify fail**

Run: `python -m pytest tests/test_free_report.py -v`
Expected: FAIL

- [ ] **Step 3: Implementar** — `send_free_raw` no bot (espelha `send_message_v3_raw`, usa `_free_group_id`/`_breaker_free`). No `Reporter`: adicionar `alert_free_repo=None` ao `__init__` e:

```python
    async def send_daily_report_free(self) -> None:
        """Resumo diario do Modelo FREE no grupo gratis (tips + total). NO-OP se off."""
        if not self.alert_free_repo:
            return
        from datetime import datetime, timedelta
        from src.core.free_status import LINE_LABELS
        since = datetime.utcnow() - timedelta(hours=24)
        alerts = await self.alert_free_repo.get_validated_since(since)
        counted = [a for a in alerts if a.status in ("green", "red")]
        if not counted:
            return
        greens = [a for a in counted if a.status == "green"]
        reds = [a for a in counted if a.status == "red"]
        voids = [a for a in alerts if a.status == "void"]
        pnl = sum((a.entry_odd - 1.0) for a in greens) - len(reds)
        emoji = "🟢" if pnl >= 0 else "🔴"
        linhas = "\n".join(
            f"{'✅' if a.status=='green' else '❌'} {a.losing_player} {LINE_LABELS[a.line]} "
            f"@ {a.entry_odd:.2f}" for a in counted
        )
        text = (
            f"📊 <b>RESULTADO DO DIA — FIFA eSports</b>\n\n"
            f"{linhas}\n\n"
            f"✅ {len(greens)} GREEN | ❌ {len(reds)} RED"
            + (f" | ⚪ {len(voids)} anuladas" if voids else "")
            + f"\n{emoji} <b>Saldo: {pnl:+.2f}u</b>  ({len(counted)} entradas)"
        )
        await self.notifier.send_free_raw(text)
```

- [ ] **Step 4: Run tests + suíte**

Run: `python -m pytest tests/test_free_report.py -v && python -m pytest -q`
Expected: PASSED + suíte verde.

- [ ] **Step 5: Commit**

```bash
git add src/core/reporter.py src/telegram/bot.py tests/test_free_report.py
git commit -m "feat(free): relatorio diario no grupo (tips + saldo, sem revelar metodo)"
```

---

### Task 7: Wiring no main.py + deploy

**Files:**
- Modify: `src/main.py`
- Test: suíte completa + smoke + verificação em produção

- [ ] **Step 1: Wiring em `src/main.py`** — após o bloco do M3, espelhando-o:

```python
    # Modelo FREE (M3 publico no grupo gratis)
    from src.core.alert_engine_free import AlertEngineFree
    from src.core.stats_engine_v3 import StatsEngineV3
    from src.db.repositories import AlertFreeRepository

    free_engine = None
    alert_free_repo = None
    if settings.free_model_enabled and settings.telegram_free_group_id:
        stats_free = StatsEngineV3(match_repo=MatchRepository(sf))
        alert_free_repo = AlertFreeRepository(sf)
        free_engine = AlertEngineFree(stats_free, alert_free_repo, notifier)
        logger.info(f"Modelo FREE enabled (grupo: {settings.telegram_free_group_id})")
    else:
        logger.info("Modelo FREE disabled (FREE_MODEL_ENABLED / TELEGRAM_FREE_GROUP_ID)")
```
Passar `free_engine=free_engine` no construtor do `OddsMonitor`.
Após o `validator_v3`:
```python
    validator_free = None
    if free_engine:
        from src.core.validator_free import ValidatorFree
        validator_free = ValidatorFree(MatchRepository(sf), alert_free_repo, notifier)
```
No gather de `_supervised_task` (junto do validator_v3):
```python
    if validator_free:
        tasks.append(_supervised_task("ValidatorFree", validator_free.start, poll_interval=60))
```
No shutdown: `if validator_free: validator_free.stop()`.
(Confirmar o nome real da setting do free group: `settings.telegram_free_group_id`.)

Passar `alert_free_repo=alert_free_repo` na construção do `Reporter` (para o relatório
diário). E agendar o relatório no scheduler (junto do `daily_report_v3`, ~23:50):
```python
    if free_engine:
        scheduler.add_daily_task(reporter.send_daily_report_free, hour=23, minute=50,
                                 task_id="daily_report_free")
```

- [ ] **Step 2: `.env` local** — adicionar:
```
FREE_MODEL_ENABLED=true
```

- [ ] **Step 3: Suíte + smoke**

Run: `python -m pytest -q && python -c "import src.main"`
Expected: todos PASSED, import OK.

- [ ] **Step 4: Commit**

```bash
git add src/main.py
git commit -m "feat(free): wiring no main — ativa via FREE_MODEL_ENABLED"
```

- [ ] **Step 5: Env var em produção ANTES do push**

```bash
cd /c/Users/Plini/fifa-bet-alert && railway variables --set "FREE_MODEL_ENABLED=true" --skip-deploys
```

- [ ] **Step 6: Push (dispara deploy)**

```bash
git push origin master
```

- [ ] **Step 7: Verificar produção**

```bash
railway logs -n 100 | grep -E "Modelo FREE|ValidatorFree|FREE pre-alerta|FREE validado"
```
Expected: `Modelo FREE enabled` e `ValidatorFree started` no startup; depois `FREE pre-alerta ...` T-30s antes dos jogos e `FREE validado ... GREEN/RED/VOID` após.

---

## Verificação final (pós-deploy)

1. Startup: `Modelo FREE enabled` + `ValidatorFree started`.
2. T-30s antes de um jogo qualificado: pré-alerta chega no grupo FREE com "Odd mínima: 1.70" e SEM termos do método.
3. Durante o jogo: `_free_tracking` grava entry_odd na 1ª vez que a odd ≥1.70.
4. Pós-jogo: mensagem editada com GREEN / RED / ⚪ ANULADO conforme a regra.
5. `alerts_free` populando (green/red/void + entry_odd) pra medir o modelo.
6. `FREE_MODEL_ENABLED=false` ⇒ nenhum efeito; M1/M2/M3 intactos.
