# Modo "sem odd" — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fazer o bot voltar ao ar sem chamar o bet365 premium (API cara de odds ao vivo), mandando tips pelo critério estatístico (sem odd) + GREEN/RED pelo placar real, nos 4 destinos (VIP/M1, DM/M2, DM/M3, FREE).

**Architecture:** Uma flag `BET365_LIVE_ODDS_ENABLED` (default `false`) corta 100% das chamadas ao `/bet365/*`. Os pré-alertas (watch loops), que já decidem a linha pelo histórico sem a odd ao vivo, passam a **persistir a tip** no modelo de cada método (odd nula) guardando o `telegram_message_id`. Os validators existentes validam GREEN/RED **só pelo placar** (`actual_goals` vs threshold) e editam a mensagem — via um **caminho de mensagem sem-odd** unificado (M1/M2/M3) e o FREE ajustado (sem VOID). Reversível: `=true` volta ao modo com odds.

**Tech Stack:** Python 3.14, SQLAlchemy 2.0 async, python-telegram-bot, pydantic-settings, loguru, pytest-asyncio (modo auto), ruff (line-length 100).

## Global Constraints

- `ruff` line-length 100; type hints em todas as funções novas; `loguru` (não `logging`).
- Testes com `pytest` (asyncio auto). Rodar do diretório `C:\Users\Plini\fifa-bet-alert`.
- **Copy pública do FREE NUNCA revela o método:** proibidas as palavras `volta`, `g1`, `g2`, `perdedor`, `edge`, `ev ` (ver `tests/test_free_telegram.py:12`). Só jogador, linha, horário e resultado.
- **Reversibilidade:** `BET365_LIVE_ODDS_ENABLED=true` deve restaurar 100% o comportamento atual (com odds). Todo branch novo é guardado por `settings.bet365_live_odds_enabled`.
- Console Windows é cp1252 — **nada de emoji em stdout de script** (`print`) pra evitar `UnicodeEncodeError`. Emojis em mensagens Telegram são OK (UTF-8).
- Commits frequentes, um por task no mínimo.

## Ordem de entrega

- **Task 1 + Task 2** = bot **volta ao ar no grupo FREE** sem gastar. Deploy aqui (Fase 1).
- **Task 3 (M3/DM), Task 4 (M1/VIP), Task 5 (M2/DM)** completam os 4 grupos. Deploy no fim (Fase 2).

---

### Task 1: Flag `BET365_LIVE_ODDS_ENABLED` + corte do premium

**Files:**
- Modify: `src/config.py` (bloco Modelo FREE, após linha 34)
- Modify: `src/core/odds_monitor.py` — `_fetch_loser_odds` (linha 1243)
- Test: `tests/test_no_odd_mode.py` (Create)

**Interfaces:**
- Produces: `settings.bet365_live_odds_enabled: bool` (default `False`). Quando `False`, `_fetch_loser_odds(return_match, loser)` retorna `(None, None, None)` **sem** chamar `self.api.bet365_get_inplay_esoccer()` nem `self.api.bet365_get_player_goals_odds(...)`.

- [ ] **Step 1: Escrever o teste que falha**

Criar `tests/test_no_odd_mode.py`:

```python
"""Modo sem odd: com BET365_LIVE_ODDS_ENABLED=false, zero chamada ao bet365 premium."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.odds_monitor import OddsMonitor


def _monitor_with_api():
    """OddsMonitor com dependencias mockadas; api espionavel."""
    api = MagicMock()
    api.bet365_get_inplay_esoccer = AsyncMock(return_value=[])
    api.bet365_get_player_goals_odds = AsyncMock(return_value=[])
    # OddsMonitor exige varios repos/engines; passamos MagicMock nos que nao usamos aqui.
    m = OddsMonitor.__new__(OddsMonitor)
    m.api = api
    return m, api


async def test_fetch_loser_odds_noop_quando_flag_off(monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "bet365_live_odds_enabled", False)
    m, api = _monitor_with_api()
    rm = MagicMock()
    rm.id = 1
    rm.player_home = "A"
    rm.player_away = "B"

    result = await m._fetch_loser_odds(rm, "A")

    assert result == (None, None, None)
    api.bet365_get_inplay_esoccer.assert_not_awaited()
    api.bet365_get_player_goals_odds.assert_not_awaited()
```

- [ ] **Step 2: Rodar o teste e ver falhar**

Run: `pytest tests/test_no_odd_mode.py::test_fetch_loser_odds_noop_quando_flag_off -v`
Expected: FAIL — a flag `bet365_live_odds_enabled` não existe (AttributeError) e/ou o premium é chamado.

- [ ] **Step 3: Adicionar a flag no config**

Em `src/config.py`, logo após a linha 34 (`free_min_odd: float = 1.70 ...`), no mesmo bloco:

```python
    # Modo sem odd — corta o bet365 premium (odds ao vivo). false = bot roda
    # so com a API barata (eventos/placar) + tips estatisticas sem odd.
    bet365_live_odds_enabled: bool = False
```

- [ ] **Step 4: Guard no `_fetch_loser_odds`**

Em `src/core/odds_monitor.py`, no início do corpo de `_fetch_loser_odds` (logo após a docstring, antes do `try:` da linha 1249), inserir:

```python
        from src.config import settings
        if not settings.bet365_live_odds_enabled:
            # Modo sem odd: nao tocar o bet365 premium (API cara).
            return None, None, None
```

- [ ] **Step 5: Rodar o teste e ver passar**

Run: `pytest tests/test_no_odd_mode.py::test_fetch_loser_odds_noop_quando_flag_off -v`
Expected: PASS

- [ ] **Step 6: Regressão + lint**

Run: `pytest tests/test_no_odd_mode.py -v && ruff check src/config.py src/core/odds_monitor.py`
Expected: PASS, sem erros de lint.

- [ ] **Step 7: Commit**

```bash
git add src/config.py src/core/odds_monitor.py tests/test_no_odd_mode.py
git commit -m "feat: flag BET365_LIVE_ODDS_ENABLED corta o bet365 premium (modo sem odd)"
```

---

### Task 2: Modelo FREE sem odd (sem VOID, mensagens sem odd)

**Files:**
- Modify: `src/core/free_status.py` — `decide_status` (linhas 5-22)
- Modify: `src/telegram/messages.py` — `format_free_prealert` (439-450), `format_free_result` (453-469)
- Modify: `src/core/validator_free.py` — `validate_match` (linha ~35, chamada a `decide_status`)
- Modify: `src/core/odds_monitor.py` — `_watch_loop_free` (665-700): não iniciar tracking de odd no modo sem odd
- Modify: `tests/test_free_telegram.py` — ajustar asserts que exigem odd/1.70/void
- Test: `tests/test_free_no_odd.py` (Create)

**Interfaces:**
- Consumes: `settings.bet365_live_odds_enabled` (Task 1).
- Produces: `decide_status(entry_odd, loser_goals, line, min_odd, require_odd: bool = True)`. Quando `require_odd=False`, **nunca** retorna `"void"` — retorna `("green", True)` / `("red", False)` só pelo placar. `format_free_prealert`/`format_free_result` deixam de exibir odd quando `settings.bet365_live_odds_enabled` é `False`.

- [ ] **Step 1: Escrever os testes que falham**

Criar `tests/test_free_no_odd.py`:

```python
"""FREE em modo sem odd: sem VOID, mensagens sem odd, green/red so pelo placar."""

import pytest

from src.core.free_status import decide_status
from src.telegram.messages import format_free_prealert, format_free_result

FORBIDDEN = ["volta", "g1", "g2", "perdedor", "edge", "ev "]


def test_decide_status_sem_odd_nunca_void_green():
    # entry_odd None mas require_odd=False -> green pelo placar (3 > 1.5)
    status, hit = decide_status(None, 3, "over15", 1.70, require_odd=False)
    assert status == "green" and hit is True


def test_decide_status_sem_odd_nunca_void_red():
    status, hit = decide_status(None, 1, "over15", 1.70, require_odd=False)
    assert status == "red" and hit is False


def test_decide_status_com_odd_mantem_void():
    # Comportamento legado intacto (modo com odd).
    status, hit = decide_status(None, 3, "over15", 1.70)
    assert status == "void" and hit is None


def test_prealert_sem_odd_nao_mostra_170(monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "bet365_live_odds_enabled", False)
    d = {"player": "Sena", "line_label": "Over 1.5", "kickoff_str": "19:43"}
    t = format_free_prealert(d).lower()
    assert "1.70" not in t
    assert "odd" not in t
    assert not any(f in t for f in FORBIDDEN)


def test_result_green_sem_odd(monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "bet365_live_odds_enabled", False)
    d = {"player": "Sena", "line_label": "Over 1.5", "actual_goals": 3}
    t = format_free_result(d, "green")
    assert "GREEN" in t
    assert "odd" not in t.lower()
    assert not any(f in t.lower() for f in FORBIDDEN)
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `pytest tests/test_free_no_odd.py -v`
Expected: FAIL — `decide_status` não aceita `require_odd`; mensagens ainda mostram odd/1.70.

- [ ] **Step 3: `decide_status` com `require_odd`**

Substituir o corpo de `decide_status` em `src/core/free_status.py` (linhas 12-22) por:

```python
def decide_status(
    entry_odd, loser_goals, line, min_odd, require_odd: bool = True
) -> tuple[str, bool | None]:
    """green/red pelo placar. Se require_odd e entry_odd is None -> void.

    require_odd=False (modo sem odd): ignora a odd, sempre green/red pelo placar.
    """
    if require_odd and entry_odd is None:
        return ("void", None)
    hit = loser_goals > LINE_THRESH[line]
    return ("green", True) if hit else ("red", False)
```

- [ ] **Step 4: Mensagens FREE sem odd**

Em `src/telegram/messages.py`, `format_free_prealert` (439-450) — tornar a odd condicional:

```python
def format_free_prealert(d: dict) -> str:
    """Pre-alerta publico FREE — SEM revelar metodo."""
    from src.config import settings
    p = _esc(d.get("player"))
    line_label = _esc(d.get("line_label"))
    kickoff_str = _esc(d.get("kickoff_str", "?"))
    if not settings.bet365_live_odds_enabled:
        # Modo sem odd: so a linha, sem mencionar odd.
        return (
            f"🔥 <b>ENTRADA FIFA eSports</b>\n"
            f"🎮 {p}  —  <b>{line_label} gols</b>\n"
            f"⏰ Jogo às {kickoff_str}\n"
            f"<i>Fique atento e faça sua entrada no jogo.</i>"
        )
    return (
        f"🔥 <b>ENTRADA FIFA eSports</b>\n"
        f"🎮 {p}  —  <b>{line_label} gols</b>\n"
        f"⏰ Jogo às {kickoff_str}\n"
        f"💰 <b>Odd mínima: 1.70</b>\n"
        f"<i>Fique atento e entre quando a odd chegar em 1.70+</i>"
    )
```

E `format_free_result` (453-469) — no modo sem odd, GREEN/RED sem odd e sem VOID:

```python
def format_free_result(d: dict, status: str) -> str:
    """Edita o pre-alerta com o resultado. status: green|red|void."""
    from src.config import settings
    p = _esc(d.get("player"))
    lbl = _esc(d.get("line_label"))
    g = d.get("actual_goals")
    odd = d.get("entry_odd")
    if status == "void":
        return (
            f"⚪ <b>ANULADO</b> — {p} {lbl}\n"
            f"A odd não atingiu 1.70 (sem entrada)."
        )
    head = "✅ <b>GREEN</b>" if status == "green" else "❌ <b>RED</b>"
    if not settings.bet365_live_odds_enabled:
        return (
            f"{head} — {p} {lbl}\n"
            f"🎯 {p} fez {g} gols"
        )
    odd_str = f"{odd:.2f}" if isinstance(odd, (int, float)) else "?"
    return (
        f"{head} — {p} {lbl}\n"
        f"🎯 {p} fez {g} gols  |  entrada @ odd {odd_str}"
    )
```

- [ ] **Step 5: `ValidatorFree` passa `require_odd` conforme a flag**

Em `src/core/validator_free.py`, na chamada a `decide_status` dentro de `validate_match`, trocar por:

```python
            from src.config import settings
            status, hit = decide_status(
                a.entry_odd, loser_goals, a.line, settings.free_min_odd,
                require_odd=settings.bet365_live_odds_enabled,
            )
```

- [ ] **Step 6: `_watch_loop_free` não inicia tracking de odd no modo sem odd**

Em `src/core/odds_monitor.py`, `_watch_loop_free` (linha ~694, onde faz `self._free_tracking[match_id] = {...}`), guardar:

```python
            if line and settings.bet365_live_odds_enabled:
                self._free_tracking[match_id] = {"line": line, "entry_odd": None, "max_odd": 0.0}
```

(garantir `from src.config import settings` disponível no módulo — já é usado em outros pontos; se não houver import no topo, adicionar.)

- [ ] **Step 7: Ajustar `tests/test_free_telegram.py`**

Os testes `test_prealert_tem_odd_minima_170_e_nao_revela_metodo` e os de void assumem modo com odd. Garantir que rodam com a flag em `True` (comportamento legado). No topo de cada um desses testes que dependem de odd/void, forçar a flag:

```python
def test_prealert_tem_odd_minima_170_e_nao_revela_metodo(monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "bet365_live_odds_enabled", True)
    t = format_free_prealert(_data()).lower()
    assert "1.70" in t
    assert not any(f in t for f in FORBIDDEN)
```

Aplicar o mesmo `monkeypatch.setattr(settings, "bet365_live_odds_enabled", True)` em `test_edit_free_result_void_agenda_auto_delete`, `test_result_green_mostra_entrada` (usa "1.75"), e nos demais que dependem de odd/void.

- [ ] **Step 8: Rodar tudo**

Run: `pytest tests/test_free_no_odd.py tests/test_free_telegram.py -v`
Expected: PASS (novos passam; legado passa com a flag forçada em True).

- [ ] **Step 9: Lint + commit**

```bash
ruff check src/core/free_status.py src/telegram/messages.py src/core/validator_free.py src/core/odds_monitor.py
git add src/core/free_status.py src/telegram/messages.py src/core/validator_free.py src/core/odds_monitor.py tests/test_free_no_odd.py tests/test_free_telegram.py
git commit -m "feat: FREE em modo sem odd — sem VOID, mensagens sem odd, green/red pelo placar"
```

---

### Task 3: Mensagem de resultado sem-odd compartilhada + M3 persiste e valida (DM)

**Files:**
- Modify: `src/telegram/messages.py` — adicionar `format_stat_result` (após `format_free_result`)
- Modify: `src/telegram/bot.py` — adicionar `edit_stat_result` (perto de `edit_free_result`, ~472)
- Modify: `src/db/repositories.py` — `AlertV3Repository`: adicionar `exists_for_match` (perto de create, ~1761)
- Modify: `src/core/odds_monitor.py` — `_emit_watch_m3` (1086-1151): persistir AlertV3 no modo sem odd
- Modify: `src/core/validator_v3.py` — `validate_match` edição (linha ~73-105): usar `edit_stat_result` no modo sem odd
- Test: `tests/test_stat_result_msg.py` (Create), `tests/test_m3_no_odd.py` (Create)

**Interfaces:**
- Consumes: `settings.bet365_live_odds_enabled`; `AlertV3Repository.create(**kwargs)`; `EvaluationV3.lines` (LineEvalV3: `line, line_label, rate, hits, n, recent_hits, recent_n`).
- Produces:
  - `format_stat_result(d: dict, hit: bool) -> str` — resultado GREEN/RED sem odd. `d`: `{target_player, line_label, actual_goals, method_tag?}`.
  - `TelegramNotifier.edit_stat_result(message_id: int, channel: str, data: dict, hit: bool) -> bool` — edita a mensagem no chat do `channel` (`"vip"|"admin"|"m3"`) com `format_stat_result`. Resolve o chat_id internamente (`vip→self.chat_id`, `admin→self._admin_chat_id`, `m3→self._m3_chat_id`).
  - `AlertV3Repository.exists_for_match(match_id: int) -> bool`.

- [ ] **Step 1: Teste do formatter sem-odd**

Criar `tests/test_stat_result_msg.py`:

```python
"""Mensagem de resultado sem odd (compartilhada M1/M2/M3)."""

from src.telegram.messages import format_stat_result


def test_stat_result_green_sem_odd():
    d = {"target_player": "Wboy", "line_label": "Over 2.5", "actual_goals": 3}
    t = format_stat_result(d, True)
    assert "GREEN" in t and "Wboy" in t and "3" in t
    assert "odd" not in t.lower()
    assert "@" not in t  # nenhuma odd


def test_stat_result_red_sem_odd():
    d = {"target_player": "OG", "line_label": "Over 3.5", "actual_goals": 2}
    t = format_stat_result(d, False)
    assert "RED" in t and "OG" in t
    assert "odd" not in t.lower()


def test_stat_result_com_method_tag():
    d = {"target_player": "X", "line_label": "Over 1.5", "actual_goals": 2, "method_tag": "M3"}
    t = format_stat_result(d, True)
    assert "M3" in t
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `pytest tests/test_stat_result_msg.py -v`
Expected: FAIL — `format_stat_result` não existe (ImportError).

- [ ] **Step 3: Implementar `format_stat_result`**

Em `src/telegram/messages.py`, após `format_free_result` (linha 469):

```python
def format_stat_result(d: dict, hit: bool) -> str:
    """Resultado GREEN/RED sem odd (modo sem odd, M1/M2/M3).

    Edita o pre-alerta com o desfecho pelo placar. d: target_player,
    line_label, actual_goals, method_tag (opcional).
    """
    p = _esc(d.get("target_player"))
    lbl = _esc(d.get("line_label"))
    g = d.get("actual_goals")
    tag = d.get("method_tag")
    tag_str = f"[{_esc(tag)}] " if tag else ""
    head = "✅ <b>GREEN</b>" if hit else "❌ <b>RED</b>"
    return (
        f"{tag_str}{head} — {p} {lbl}\n"
        f"🎯 {p} fez {g} gols"
    )
```

- [ ] **Step 4: Rodar e ver passar**

Run: `pytest tests/test_stat_result_msg.py -v`
Expected: PASS

- [ ] **Step 5: `edit_stat_result` no notifier**

Em `src/telegram/bot.py`, perto de `edit_free_result` (~472). Ler antes o padrão de `edit_free_result` (usa `self.bot.edit_message_text(chat_id=..., message_id=..., text=..., parse_mode="HTML")` e retorna bool). Adicionar:

```python
    async def edit_stat_result(
        self, message_id: int, channel: str, data: dict, hit: bool
    ) -> bool:
        """Edita a msg do pre-alerta (modo sem odd) com o resultado GREEN/RED.

        channel: 'vip' | 'admin' | 'm3'. Sem odd — usa format_stat_result.
        """
        chat_id = {
            "vip": self.chat_id,
            "admin": self._admin_chat_id,
            "m3": self._m3_chat_id,
        }.get(channel)
        if not chat_id or not message_id:
            return False
        from src.telegram.messages import format_stat_result
        try:
            await self.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=format_stat_result(data, hit),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"edit_stat_result falhou (msg {message_id}): {e}")
            return False
```

(Confirmar os nomes dos atributos de chat no `__init__` do `TelegramNotifier`: `self.chat_id`, `self._admin_chat_id`, `self._m3_chat_id`. Ajustar se diferirem.)

- [ ] **Step 6: `exists_for_match` no `AlertV3Repository`**

Em `src/db/repositories.py`, perto de `AlertV3Repository.create` (~1761). Espelhar o `exists_for_match` do `AlertFreeRepository` (1844-1853):

```python
    async def exists_for_match(self, match_id: int) -> bool:
        async with self._session() as session:
            result = await session.execute(
                select(AlertV3.id).where(AlertV3.match_id == match_id).limit(1)
            )
            return result.first() is not None
```

(Garantir `AlertV3` e `select` importados no módulo — já são usados.)

- [ ] **Step 7: Teste de persistência M3 no watch**

Criar `tests/test_m3_no_odd.py`:

```python
"""M3 no modo sem odd: o pre-alerta (watch) persiste AlertV3 sem odd."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.odds_monitor import OddsMonitor


async def test_emit_watch_m3_persiste_alertv3_sem_odd(monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "bet365_live_odds_enabled", False)

    # evaluation com 1 linha qualificada
    line = MagicMock(line="over25", line_label="Over 2.5", rate=0.7, hits=14, n=20,
                     recent_hits=5, recent_n=7, qualified=True)
    evaluation = MagicMock(should_alert=True, lines=[line])

    stats = MagicMock()
    stats.evaluate = AsyncMock(return_value=evaluation)

    repo = MagicMock()
    repo.exists_for_match = AsyncMock(return_value=False)
    created = MagicMock(id=99)
    repo.create = AsyncMock(return_value=created)
    repo.update_telegram_message_id = AsyncMock()

    engine_v3 = MagicMock(stats=stats, alerts=repo)

    notifier = MagicMock()
    notifier.send_watch_v3 = AsyncMock(return_value=555)

    m = OddsMonitor.__new__(OddsMonitor)
    m.alert_engine_v3 = engine_v3
    m.notifier = notifier
    m._predictive_sent = set()
    m._WATCH_AUTO_DELETE_SECONDS = 900

    rm = MagicMock(id=1)
    rm.game1_id = 1
    g1 = MagicMock(id=1, player_home="A", player_away="B", score_home=0, score_away=2)

    await m._emit_watch_m3(rm, g1, "A", "B")

    repo.create.assert_awaited_once()
    kwargs = repo.create.await_args.kwargs
    assert kwargs["match_id"] == 1
    assert kwargs["line"] == "over25"
    assert kwargs.get("odds") is None
    repo.update_telegram_message_id.assert_awaited_once_with(99, 555)
```

- [ ] **Step 8: Rodar e ver falhar**

Run: `pytest tests/test_m3_no_odd.py -v`
Expected: FAIL — `_emit_watch_m3` ainda não persiste.

- [ ] **Step 9: Persistir AlertV3 no `_emit_watch_m3`**

Em `src/core/odds_monitor.py`, `_emit_watch_m3` (1086-1151). Ler o método inteiro antes de editar. Após montar `watch_data` e enviar `msg_id = await notifier.send_watch_v3(...)` (linha ~1146), no modo sem odd persistir uma row por linha qualificada, guardando o `message_id` na primeira. Inserir logo após obter `msg_id`:

```python
            from src.config import settings
            if settings.bet365_live_odds_enabled is False and evaluation.lines:
                repo = self.alert_engine_v3.alerts
                if not await repo.exists_for_match(return_match.id):
                    g1_score = (
                        f"{game1_match.score_home}-{game1_match.score_away}"
                        if game1_match.player_home == loser
                        else f"{game1_match.score_away}-{game1_match.score_home}"
                    )
                    first = True
                    for le in evaluation.lines:
                        if not getattr(le, "qualified", True):
                            continue
                        alert = await repo.create(
                            match_id=return_match.id,
                            losing_player=loser,
                            opponent_player=winner,
                            game1_score=g1_score,
                            line=le.line,
                            odds=None,
                            rate=le.rate,
                            hits=le.hits,
                            n_h2h=le.n,
                            recent_hits=le.recent_hits,
                        )
                        if first and msg_id:
                            await repo.update_telegram_message_id(alert.id, msg_id)
                            first = False
```

(Ajustar o nome da variável do message_id ao real usado no método — o mapa indica `send_watch_v3` retorna o id; capturar em `msg_id` se ainda não for.)

- [ ] **Step 10: Rodar e ver passar**

Run: `pytest tests/test_m3_no_odd.py -v`
Expected: PASS

- [ ] **Step 11: `ValidatorV3` edita sem odd no modo sem odd**

Em `src/core/validator_v3.py`, no bloco de edição (linhas ~73-105). Ler o bloco antes. No modo sem odd, em vez de `notifier.edit_alert_v3_result(...)`, usar `edit_stat_result` com a linha que tem `telegram_message_id`. Guardar por flag:

```python
        from src.config import settings
        if settings.bet365_live_odds_enabled is False:
            msg_alert = next((a for a in alerts if a.telegram_message_id), None)
            if msg_alert:
                # a "melhor" linha valida a mensagem: usa a de maior rate.
                best = max(alerts, key=lambda a: (a.rate or 0))
                data = {
                    "target_player": best.losing_player,
                    "line_label": LINE_LABELS.get(best.line, best.line),
                    "actual_goals": loser_goals,
                    "method_tag": "M3",
                }
                await self.notifier.edit_stat_result(
                    msg_alert.telegram_message_id, "m3", data, bool(best.hit)
                )
            return
        # ... caminho legado com odd (edit_alert_v3_result) permanece abaixo ...
```

(Importar `LINE_LABELS` de `src.core.free_status` no topo do validator, ou de `M3_LINE_LABELS` em `stats_engine_v3` — usar o que já estiver disponível; ambos mapeiam `over25→"Over 2.5"`.)

- [ ] **Step 12: Rodar suíte M3 + lint**

Run: `pytest tests/test_m3_no_odd.py tests/test_stat_result_msg.py -v && ruff check src/telegram/messages.py src/telegram/bot.py src/db/repositories.py src/core/odds_monitor.py src/core/validator_v3.py`
Expected: PASS, lint limpo.

- [ ] **Step 13: Commit**

```bash
git add src/telegram/messages.py src/telegram/bot.py src/db/repositories.py src/core/odds_monitor.py src/core/validator_v3.py tests/test_stat_result_msg.py tests/test_m3_no_odd.py
git commit -m "feat: M3 sem odd — watch persiste AlertV3 + green/red pelo placar (DM)"
```

---

### Task 4: M1 persiste e valida (VIP), pré-alerta sem odd

**Files:**
- Modify: `src/telegram/messages.py` — `format_watch_message` (138-261): esconder odd no modo sem odd
- Modify: `src/db/repositories.py` — `AlertRepository`: adicionar `exists_for_match` (~617)
- Modify: `src/core/odds_monitor.py` — `_emit_watch_m1` (747-875): persistir Alert no modo sem odd
- Modify: `src/core/validator.py` — `_send_result_notification` (370-443): usar `edit_stat_result` no modo sem odd
- Test: `tests/test_m1_no_odd.py` (Create)

**Interfaces:**
- Consumes: `settings.bet365_live_odds_enabled`; `predict_watch_candidate` retorno (`{line, line_label, target_player, lines:[...]}`); `AlertRepository.create(**kwargs)`; `edit_stat_result` (Task 3).
- Produces: `AlertRepository.exists_for_match(match_id) -> bool`. `format_watch_message` omite `@ X.XX+` quando `settings.bet365_live_odds_enabled` é `False`.

- [ ] **Step 1: Teste — pré-alerta M1 sem odd + persistência**

Criar `tests/test_m1_no_odd.py`:

```python
"""M1 no modo sem odd: pre-alerta VIP sem odd + persiste Alert sem odd."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.telegram.messages import format_watch_message


def test_watch_message_vip_sem_odd(monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "bet365_live_odds_enabled", False)
    d = {
        "vip_clean_mode": True,
        "method": "M1",
        "player_home": "A", "player_away": "B",
        "target_player": "A",
        "kickoff_str": "19:43",
        "lines_eligible": [{"line_label": "Over 2.5", "target_odds": 1.80}],
    }
    t = format_watch_message(d)
    assert "Over 2.5" in t
    assert "1.80" not in t   # odd-alvo escondida
    assert "@" not in t


async def test_emit_watch_m1_persiste_alert_sem_odd(monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "bet365_live_odds_enabled", False)

    candidate = {
        "line": "over25", "line_label": "Over 2.5", "target_player": "A",
        "target_odds": 1.80,
        "lines": [{"line": "over25", "line_label": "Over 2.5", "target_odds": 1.80,
                   "predicted_tp": 0.7, "qualified": True}],
    }
    stats = MagicMock()
    stats.predict_watch_candidate = AsyncMock(return_value=candidate)

    repo = MagicMock()
    repo.exists_for_match = AsyncMock(return_value=False)
    repo.create = AsyncMock(return_value=MagicMock(id=42))
    repo.update_telegram_message_id = AsyncMock()

    engine = MagicMock(stats=stats, alerts=repo)
    notifier = MagicMock()
    notifier.send_watch = AsyncMock(return_value=777)

    m = OddsMonitor_stub(engine, notifier)

    rm = MagicMock(id=5)
    rm.game1_id = 5
    g1 = MagicMock(id=5, player_home="A", player_away="B", score_home=0, score_away=1)

    await m._emit_watch_m1(rm, g1, "A", "B", 0)

    repo.create.assert_awaited_once()
    kw = repo.create.await_args.kwargs
    assert kw["match_id"] == 5
    assert kw["best_line"] == "over25"
    assert kw.get("over25_odds") is None
    repo.update_telegram_message_id.assert_awaited_once_with(42, 777)


def OddsMonitor_stub(engine, notifier):
    from src.core.odds_monitor import OddsMonitor
    m = OddsMonitor.__new__(OddsMonitor)
    m.alert_engine = engine
    m.notifier = notifier
    m._predictive_sent = set()
    m._WATCH_AUTO_DELETE_SECONDS = 900
    m.blocked_repo = None
    return m
```

> Nota pro implementador: `_emit_watch_m1` consulta `blocked_repo.is_suppressed` e `compute_h2h_tier`. No teste, `blocked_repo=None` deve seguir o caminho "sem supressão". Se o código não tolerar `blocked_repo=None`, ajustar o stub para um MagicMock com `is_suppressed=AsyncMock(return_value=False)` e mockar `compute_h2h_tier` via monkeypatch para retornar tier neutro. Ler o método antes e adequar o stub — o objetivo do teste é só provar a persistência sem odd e a mensagem sem odd.

- [ ] **Step 2: Rodar e ver falhar**

Run: `pytest tests/test_m1_no_odd.py -v`
Expected: FAIL — mensagem ainda mostra odd; `_emit_watch_m1` não persiste.

- [ ] **Step 3: Esconder odd em `format_watch_message` (vip_clean_mode)**

Em `src/telegram/messages.py`, `format_watch_message`, bloco `vip_clean_mode` (152-180). Tornar a odd condicional:

```python
    if d.get("vip_clean_mode"):
        from src.config import settings
        no_odd = not settings.bet365_live_odds_enabled
        method = d.get("method", "M1")
        method_tag = f" [{method}]" if method != "M1" else ""
        eligible = d.get("lines_eligible") or []
        if not eligible:
            eligible = [{
                "line_label": d.get("line_label"),
                "target_odds": d.get("target_odds", 0),
            }]
        if no_odd:
            lines_txt = "\n".join(
                f"   ✅ {_esc(l.get('line_label','?'))}" for l in eligible
            )
        else:
            lines_txt = "\n".join(
                f"   ✅ {_esc(l.get('line_label','?'))} @ {(l.get('target_odds',0) or 0):.2f}+"
                for l in eligible
            )
        header_label = "Linha provável" if len(eligible) == 1 else "Linhas prováveis"
        return (
            f"🔔 <b>PRÉ-ALERTA{method_tag} — {_esc(d.get('kickoff_str', '?'))}</b>\n"
            f"\n"
            f"⚽ {_esc(d.get('player_home'))} vs {_esc(d.get('player_away'))}\n"
            f"🎯 Alvo: <b>{target_player}</b>\n"
            f"\n"
            f"📊 {header_label}:\n"
            f"{lines_txt}\n"
            f"\n"
            f"━━━━━━━━━━━━━━━\n"
            f"⚠️ <b>AINDA NÃO É APOSTA</b>\n"
            f"Aguarde o início do jogo e faça sua entrada.\n"
            f"\n"
            f"📱 Já pode abrir o jogo na bet365."
        )
```

- [ ] **Step 4: `exists_for_match` no `AlertRepository`**

Em `src/db/repositories.py`, perto de `AlertRepository.create` (617-623):

```python
    async def exists_for_match(self, match_id: int) -> bool:
        async with self._session() as session:
            result = await session.execute(
                select(Alert.id).where(Alert.match_id == match_id).limit(1)
            )
            return result.first() is not None
```

- [ ] **Step 5: Persistir Alert no `_emit_watch_m1`**

Em `src/core/odds_monitor.py`, `_emit_watch_m1` (747-875). Ler o método inteiro. Após o envio `msg_id = await notifier.send_watch(watch_data, ...)` (~869), no modo sem odd persistir o Alert com `best_line = candidate["line"]` e odds nulas, guardando o message_id:

```python
            from src.config import settings
            if settings.bet365_live_odds_enabled is False and msg_id:
                repo = self.alert_engine.alerts
                if not await repo.exists_for_match(return_match.id):
                    g1_score = (
                        f"{game1_match.score_home}-{game1_match.score_away}"
                        if game1_match.player_home == loser
                        else f"{game1_match.score_away}-{game1_match.score_home}"
                    )
                    alert = await repo.create(
                        match_id=return_match.id,
                        losing_player=loser,
                        game1_score=g1_score,
                        best_line=candidate["line"],
                        loser_goals_g1=loser_goals_g1,
                    )
                    await repo.update_telegram_message_id(alert.id, msg_id)
```

(Confirmar que `send_watch` retorna o message_id e que ele está capturado em `msg_id` — se o código não captura, capturar. Confirmar que `AlertRepository` tem `update_telegram_message_id`; o mapa lista para AlertFree — se não existir no AlertRepository, adicionar espelhando o do AlertFree: `UPDATE alerts SET telegram_message_id=... WHERE id=...`.)

- [ ] **Step 6: `Validator._send_result_notification` sem odd**

Em `src/core/validator.py`, `_send_result_notification` (370-443). Ler o método. No início, no modo sem odd, editar via `edit_stat_result` (canal `vip`) e retornar antes do caminho com odd:

```python
        from src.config import settings
        if settings.bet365_live_odds_enabled is False:
            if alert.suppressed or not alert.telegram_message_id:
                return
            from src.core.free_status import LINE_LABELS
            data = {
                "target_player": alert.losing_player,
                "line_label": LINE_LABELS.get(alert.best_line or "over25", "Over 2.5"),
                "actual_goals": actual_goals,
                "method_tag": "M1",
            }
            await self.notifier.edit_stat_result(
                alert.telegram_message_id, "vip", data, bool(hit)
            )
            return
        # ... caminho legado (edit_alert_result) permanece abaixo ...
```

(Adequar os nomes `actual_goals`/`hit` aos reais no escopo de `_send_result_notification` — ler o método; o mapa indica que o hit é calculado em `_validate_alert` e a notificação recebe os dados via `_rebuild_alert_data`. Usar as variáveis presentes no escopo real.)

- [ ] **Step 7: Rodar e ver passar**

Run: `pytest tests/test_m1_no_odd.py -v`
Expected: PASS

- [ ] **Step 8: Lint + commit**

```bash
ruff check src/telegram/messages.py src/db/repositories.py src/core/odds_monitor.py src/core/validator.py
git add src/telegram/messages.py src/db/repositories.py src/core/odds_monitor.py src/core/validator.py tests/test_m1_no_odd.py
git commit -m "feat: M1 sem odd — watch VIP persiste Alert + green/red pelo placar"
```

---

### Task 5: M2 persiste e valida (DM)

**Files:**
- Modify: `src/db/repositories.py` — `AlertV2Repository`: adicionar `exists_for_match` (~1498)
- Modify: `src/core/odds_monitor.py` — `_emit_watch_m2` (920-1040): persistir AlertV2 no modo sem odd
- Modify: `src/core/validator_v2.py` — `_send_result_notification` (200-241): usar `edit_stat_result` (canal `admin`)
- Test: `tests/test_m2_no_odd.py` (Create)

**Interfaces:**
- Consumes: `settings.bet365_live_odds_enabled`; `predict_watch_candidate` V2 (retorno com `camada`); `AlertV2Repository.create(**kwargs)`; `edit_stat_result`.
- Produces: `AlertV2Repository.exists_for_match(match_id) -> bool`.

- [ ] **Step 1: Teste — persistência M2 sem odd**

Criar `tests/test_m2_no_odd.py`:

```python
"""M2 no modo sem odd: pre-alerta DM persiste AlertV2 sem odd."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.odds_monitor import OddsMonitor


async def test_emit_watch_m2_persiste_alertv2_sem_odd(monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "bet365_live_odds_enabled", False)

    candidate = {
        "line": "over25", "line_label": "Over 2.5", "target_player": "A",
        "target_odds": 1.80, "camada": "C2",
        "lines": [{"line": "over25", "line_label": "Over 2.5", "target_odds": 1.80,
                   "predicted_tp": 0.7, "qualified": True}],
    }
    stats = MagicMock()
    stats.predict_watch_candidate = AsyncMock(return_value=candidate)

    repo = MagicMock()
    repo.exists_for_match = AsyncMock(return_value=False)
    repo.create = AsyncMock(return_value=MagicMock(id=7))
    repo.update_telegram_message_id = AsyncMock()

    engine = MagicMock(stats=stats, alerts=repo)
    notifier = MagicMock()
    notifier.send_watch = AsyncMock(return_value=888)

    m = OddsMonitor.__new__(OddsMonitor)
    m.alert_engine_v2 = engine
    m.notifier = notifier
    m._predictive_sent = set()
    m._WATCH_AUTO_DELETE_SECONDS = 900
    m.blocked_repo_v2 = MagicMock(is_suppressed=AsyncMock(return_value=False))

    rm = MagicMock(id=8)
    rm.game1_id = 8
    g1 = MagicMock(id=8, player_home="A", player_away="B", score_home=0, score_away=1)

    await m._emit_watch_m2(rm, g1, "A", "B", 0)

    repo.create.assert_awaited_once()
    kw = repo.create.await_args.kwargs
    assert kw["match_id"] == 8
    assert kw["best_line"] == "over25"
    assert kw["camada"] == "C2"
    assert kw.get("over25_odds") is None
    repo.update_telegram_message_id.assert_awaited_once_with(7, 888)
```

> Nota: adequar o stub aos atributos que `_emit_watch_m2` realmente lê (o mapa cita `blocked_lines_v2`/`compute_h2h_tier_v2`). Ler o método antes; mockar via monkeypatch o que for necessário pra alcançar a persistência. O objetivo é provar `create` sem odd + `camada` preenchida + message_id.

- [ ] **Step 2: Rodar e ver falhar**

Run: `pytest tests/test_m2_no_odd.py -v`
Expected: FAIL — `_emit_watch_m2` não persiste.

- [ ] **Step 3: `exists_for_match` no `AlertV2Repository`**

Em `src/db/repositories.py`, perto de `AlertV2Repository.create` (1498-1503):

```python
    async def exists_for_match(self, match_id: int) -> bool:
        async with self._session() as session:
            result = await session.execute(
                select(AlertV2.id).where(AlertV2.match_id == match_id).limit(1)
            )
            return result.first() is not None
```

- [ ] **Step 4: Persistir AlertV2 no `_emit_watch_m2`**

Em `src/core/odds_monitor.py`, `_emit_watch_m2` (920-1040). Ler o método. Após `msg_id = await notifier.send_watch(watch_data, ..., to_admin=True)` (~1033), no modo sem odd:

```python
            from src.config import settings
            if settings.bet365_live_odds_enabled is False and msg_id:
                repo = self.alert_engine_v2.alerts
                if not await repo.exists_for_match(return_match.id):
                    g1_score = (
                        f"{game1_match.score_home}-{game1_match.score_away}"
                        if game1_match.player_home == loser
                        else f"{game1_match.score_away}-{game1_match.score_home}"
                    )
                    alert = await repo.create(
                        match_id=return_match.id,
                        losing_player=loser,
                        opponent_player=winner,
                        game1_score=g1_score,
                        camada=candidate.get("camada") or "C2",
                        best_line=candidate["line"],
                    )
                    await repo.update_telegram_message_id(alert.id, msg_id)
```

(`camada` é NOT NULL no AlertV2 — sempre preencher, default `"C2"` se ausente. Confirmar `AlertV2Repository.update_telegram_message_id`; se não existir, adicionar espelhando o do AlertFree.)

- [ ] **Step 5: `ValidatorV2._send_result_notification` sem odd**

Em `src/core/validator_v2.py`, `_send_result_notification` (200-241). Ler o método. No modo sem odd, editar via `edit_stat_result` (canal `admin`) e retornar antes do caminho com odd:

```python
        from src.config import settings
        if settings.bet365_live_odds_enabled is False:
            if alert.suppressed or not alert.telegram_message_id:
                return
            from src.core.free_status import LINE_LABELS
            data = {
                "target_player": alert.losing_player,
                "line_label": LINE_LABELS.get(alert.best_line or "over25", "Over 2.5"),
                "actual_goals": actual_goals,
                "method_tag": "M2",
            }
            await self.notifier.edit_stat_result(
                alert.telegram_message_id, "admin", data, bool(hit)
            )
            return
        # ... caminho legado (edit_alert_v2_result) permanece abaixo ...
```

(Adequar `actual_goals`/`hit` às variáveis reais do escopo — ler o método.)

- [ ] **Step 6: Rodar e ver passar**

Run: `pytest tests/test_m2_no_odd.py -v`
Expected: PASS

- [ ] **Step 7: Suíte completa + lint**

Run: `pytest -q && ruff check src/`
Expected: PASS (suíte inteira verde), lint limpo.

- [ ] **Step 8: Commit**

```bash
git add src/db/repositories.py src/core/odds_monitor.py src/core/validator_v2.py tests/test_m2_no_odd.py
git commit -m "feat: M2 sem odd — watch DM persiste AlertV2 + green/red pelo placar"
```

---

## Deploy (após Task 5, ou parcial após Task 2)

- Setar no Railway: `BET365_LIVE_ODDS_ENABLED=false`.
- Merge da branch + push; confirmar container subindo e logs sem chamadas a `/bet365/*`.
- Sanidade: confirmar pré-alerta + edição GREEN/RED chegando (grupo FREE após Task 2; VIP/DM após Task 5).

## Notas de escopo / reversibilidade

- Alertas **live de edge** (M1/M2 com odd) e o **tracking de odd do FREE** ficam desligados no modo sem odd — os pré-alertas estatísticos são o produto.
- Todo o código do bet365 premium fica dormente atrás da flag. `BET365_LIVE_ODDS_ENABLED=true` restaura o modo com odds.
- Relatórios diários: já contam GREEN/RED por método via os mesmos repos (o FREE ignora `void`, que deixa de existir no modo sem odd). Sem mudança necessária.
