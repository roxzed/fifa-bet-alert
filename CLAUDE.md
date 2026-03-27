# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Sobre o Projeto

Sistema automatizado de monitoramento e alerta para apostas FIFA eSports na bet365.
Monitora jogos da liga **Esoccer Battle - 8 mins play** (league ID `22614`, v2 API), identifica o perdedor do jogo 1,
e alerta via Telegram APENAS quando há edge matemático comprovado (probabilidade real > implícita).

**Hipótese core:** O jogador que perde o jogo 1 tende a marcar mais gols na volta (55-60 min depois).
**Mercado alvo:** Over 2.5/3.5/4.5 gols do *jogador específico* (não gols totais).
**Gate de alerta:** `Edge >= 5%` E `EV >= 3%` E `true_prob >= 68%`.

## Comandos

```bash
# Instalar dependências
pip install -e ".[dev]"

# Rodar o sistema principal
python -m src.main

# Testes (pytest-asyncio em modo auto)
pytest
pytest tests/test_probability.py          # módulo específico
pytest --cov=src/core/probability         # com coverage

# Lint e formatação
ruff check src/
ruff format src/

# Type checking
mypy src/

# Scripts utilitários
python scripts/validate_api.py            # testar conexão BetsAPI
python scripts/collect_history.py         # coleta histórica
python scripts/backtest.py                # análise + calibração
python scripts/check_recent.py            # checar dados recentes
python scripts/scrape_bet365.py           # scraping odds
```

## Configuração (.env)

Todas as configurações via `src/config.py` (pydantic-settings), lidas de `.env`:

```
BETSAPI_TOKEN=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
DATABASE_URL=postgresql+asyncpg://...     # SQLite também suportado
```

Thresholds críticos configuráveis: `MIN_EDGE=0.05`, `MIN_EV=0.03`, `MIN_TRUE_PROB=0.65`, `COLD_START_DAYS=83`.

## Arquitetura — Fluxo Principal

```
BetsAPI (v2) → GameWatcher → PairMatcher → OddsMonitor → AlertEngine → Telegram
                    ↓               ↓              ↓            ↓
                 DB (matches)   DB (pairs)    DB (odds)    StatsEngine
                                                               ↓
                                                          probability.py
```

1. **`GameWatcher`** (`src/core/game_watcher.py`) — loop principal, poll a cada 180s nos jogos encerrados da liga. Detecta fim de jogo, registra resultado, identifica o perdedor, dispara `PairMatcher`.

2. **`PairMatcher`** (`src/core/pair_matcher.py`) — encontra o jogo de volta (mesmos dois jogadores, ~55-60 min após G1) buscando em upcoming events. Jogos não encontrados ficam em fila `_pending` com retentativa a cada 2 min.

3. **`OddsMonitor`** (`src/core/odds_monitor.py`) — para cada volta identificada, cria uma task asyncio que poll odds a cada 15s. Avalia linhas over 1.5/2.5/3.5/4.5 do jogador específico. Dispara `AlertEngine` quando uma linha tem edge.

4. **`StatsEngine`** (`src/core/stats_engine.py`) — orquestra 13 camadas de probabilidade (veja docstring do módulo). Retorna `OpportunityEvaluation` com a melhor linha. Implementa lógica de cold start e regime change.

5. **`AlertEngine`** (`src/core/alert_engine.py`) — recebe oportunidade, chama `StatsEngine`, persiste alerta no DB e envia Telegram se `should_alert=True`.

6. **`Validator`** (`src/core/validator.py`) — poll pós-jogo, registra resultado real dos alertas, atualiza stats do jogador.

7. **`Reporter`** (`src/core/reporter.py`) — relatórios diário (23:55) e semanal (dom 23:50) via Telegram.

## Módulo Estatístico (CORE)

**`src/core/probability.py`** — funções PURAS (sem I/O). Contém:
- `implied_probability`, `calculate_true_probability` (13 camadas com pesos dinâmicos)
- `edge`, `expected_value`, `fractional_kelly`
- `bayesian_update`, `wilson_confidence_interval`
- `should_alert`, `star_rating`, `classify_loss`, `detect_regime_change`, `simulate_roi`

**`src/core/stats_engine.py`** — orquestra as funções puras com dados do DB. Avalia as 3 linhas (over25/35/45) e retorna a de melhor EV via `OpportunityEvaluation`. Implementa cold start (83 dias por padrão) e regime degradation (z-score).

## Database

SQLAlchemy 2.0 async. Modelos em `src/db/models.py`:
- `Player` — perfil do jogador com hit rates por tipo de derrota (tight/medium/blowout)
- `Match` — jogos G1 e G2 com placar, times, odds abertura/fechamento
- `Odds` — histórico completo de movimentação
- `Alert` — alertas enviados com resultado pós-validação
- `PlayerTeamPreference`, `TeamStats`, `MethodStats` — dados para layers avançadas
- `LeagueConfig` — configuração da liga monitorada

Repositórios em `src/db/repositories.py` — todos os métodos são `async`.

## Fases do Sistema

1. **Cold Start** (83 dias): coleta silenciosa, sem alertas enviados
2. **Backtest**: `scripts/backtest.py` — análise completa + calibração de pesos
3. **Shadow Mode** (2 semanas): alertas marcados "não apostar"
4. **Live**: alertas reais com edge comprovado

## APIs Externas

- **BetsAPI v1** (`api.betsapi.com/v1`) — dados históricos, liga 2025 (ID `42648`)
- **BetsAPI v2** (`api.betsapi.com/v2`) — jogos ao vivo/upcoming, liga atual (ID `22614`)
- Liga atual **não aparece no `/league` do v1** — busca direta pelo ID no v2

## Convenções

- Código em inglês, comentários em português quando necessário
- Todas as funções com I/O devem ser `async`
- `probability.py`: funções PURAS, **100% de coverage obrigatório**
- `loguru` para todos os logs (não `logging`)
- `pydantic-settings` + `.env` para configurações (nunca hardcoded)
- Type hints em todas as funções, docstrings em funções públicas
- `ruff` line-length 100, mypy strict
