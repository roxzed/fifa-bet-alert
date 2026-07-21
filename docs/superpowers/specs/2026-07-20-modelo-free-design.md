# Modelo FREE — nova metodologia pública (baseada no M3)

**Data:** 2026-07-20
**Status:** Aprovado pelo owner

## Objetivo

Rodar o critério do M3 (frequência H2H) como um **novo modelo público no grupo
FREE** (`TELEGRAM_FREE_GROUP_ID`), com pré-alerta antes do jogo, confirmação de
odd ao vivo e documentação de resultado (green/red/anulado) editando a mensagem.
Fluxo isolado, em paralelo ao M3/VIP, ligável por env var.

## Regras (definidas pelo owner)

1. **Pré-alerta T-30s** antes do kickoff, no grupo FREE, com **uma linha** — a de
   **maior taxa** do critério M3 (mais provável).
2. **Odd mínima 1.70**, monitorada AO VIVO:
   - O sistema monitora a odd da linha alertada durante **todo o jogo** (até
     `ended` — não para após gol; a odd pode voltar a subir).
   - Assim que a odd atinge **≥ 1.70 pela primeira vez**, marca a **entrada
     válida** (registra a odd de entrada e o momento).
   - Se o jogo termina e a odd **nunca** atingiu 1.70 → **ANULADO** (no-bet).
3. **Resultado pós-jogo** (edita a mensagem):
   - Entrada válida (odd atingiu ≥1.70) **e** linha bateu → ✅ **GREEN**
   - Entrada válida **e** não bateu → ❌ **RED**
   - Odd nunca atingiu 1.70 → ⚪ **ANULADO** (não entra no placar green/red)
4. **A mensagem sempre exibe "odd mínima 1.70"**.
5. **Sem revelar o método** (regra pública LENDA): NUNCA mencionar "jogo de
   volta", G1/G2, "perdedor do 1º jogo", edge/EV/matemática. Só: jogador, linha
   de over, horário, odd mínima 1.70.
6. **Sem limite de volume** (todos os sinais que qualificam vão pro FREE).

## Fluxo

```
Volta casada via upcoming (pré-kickoff)
   └── T-30s: PRÉ-ALERTA no FREE (linha de maior taxa, "odd mínima 1.70")
        └── cria AlertFree (status=pendente)
   in-play (odds_monitor, até o jogo acabar):
        └── rastreia a odd da linha; grava entry_odd na 1ª vez que odd >= 1.70
   fim do jogo (ValidatorFree):
        ├── odd atingiu 1.70 + linha bateu   -> GREEN  (edita msg)
        ├── odd atingiu 1.70 + nao bateu      -> RED    (edita msg)
        └── odd nunca atingiu 1.70            -> VOID   (edita msg)
```

**Limitação herdada (explícita):** o pré-alerta T-30s depende de a volta ser
casada via `upcoming` ANTES do kickoff — igual ao watcher atual. Nos gaps de
transição de rodada da liga (quando a volta só aparece in-play), NÃO haverá
pré-alerta FREE pra aquele jogo. Isso é aceito (mesmo comportamento do watch).

## Arquitetura (reusa o M3)

| Unidade | Responsabilidade |
|---|---|
| `StatsEngineV3` (reuso) | Fornece as linhas qualificadas; o modelo FREE pega a de **maior taxa** (`rate`). |
| `src/db/models.py` — `AlertFree` (novo) | Persistência isolada do modelo FREE. Campos: `match_id`, `losing_player`, `opponent_player`, `game1_score`, `line`, `rate`, `hits`, `n_h2h`, `recent_hits`, `entry_odd` (1ª odd ≥1.70; null se nunca), `max_odd` (maior odd vista), `status` ("pending"/"green"/"red"/"void"), `telegram_message_id`, `sent_at`, `actual_goals`, `hit`, `validated_at`. |
| `AlertFreeRepository` (novo) | create / exists_for_match / update_odds / validate / get_unvalidated / get_validated_since (espelha `AlertV3Repository`). |
| `src/telegram/bot.py` | `send_watch_free(data)` (pré-alerta pro `_free_group_id`, com "odd mínima 1.70"); `edit_free_result(message_id, data, status)`; breaker próprio ou reuso do `_breaker_free`. |
| `src/telegram/messages.py` | `format_free_prealert(d)` e `format_free_result(d, status)` — copy pública, sem método. |
| `src/core/odds_monitor.py` | `_watch_loop_free` (T-30s via `_WATCH_FREE_LEAD_SECONDS=30`); no `_monitor_loop`, rastrear a odd da linha FREE alertada e atualizar `entry_odd`/`max_odd` (via dict em memória `_free_tracking[match_id]`, persistido no fim). Aditivo, protegido por `if free_engine`. |
| `src/core/alert_engine_free.py` (novo) | Orquestra: no pré-alerta escolhe a linha de maior taxa, cria `AlertFree`, dispara `send_watch_free`. |
| `src/core/validator_free.py` (novo) | Pós-jogo: aplica a regra green/red/void, edita a mensagem, persiste. |
| `src/main.py` | Liga o modelo FREE quando `FREE_MODEL_ENABLED` + `TELEGRAM_FREE_GROUP_ID`. |
| `src/config.py` | `free_model_enabled: bool = False`; `free_min_odd: float = 1.70`; `_WATCH_FREE_LEAD_SECONDS` em odds_monitor. |

**Rastreamento da odd (in-play):** quando o pré-alerta FREE é enviado, guarda-se
`_free_tracking[match_id] = {line, message_id, entry_odd=None, max_odd=0}`. A cada
poll do `_monitor_loop`, se `match_id` está em `_free_tracking`, lê a odd da
`line` alertada; atualiza `max_odd`; na 1ª vez que odd ≥ `free_min_odd`, grava
`entry_odd`. No fim do jogo, persiste em `AlertFree` e o `ValidatorFree` decide.

## Mensagens (públicas — sem método)

**Pré-alerta (T-30s):**
```
🔥 ENTRADA FIFA eSports
🎮 <jogador>  —  Over X.5 gols
⏰ Jogo às HH:MM
💰 Odd mínima: 1.70
Fique atento e entre quando a odd chegar em 1.70+
```

**Resultado (edita a mesma mensagem):**
- GREEN: `✅ GREEN — <jogador> Over X.5 (fez N gols) | entrada @ odd Y.YY`
- RED: `❌ RED — <jogador> Over X.5 (fez N gols) | entrada @ odd Y.YY`
- VOID: `⚪ ANULADO — a odd não atingiu 1.70 (sem entrada)`

## Documentação / relatório
- `AlertFree` registra tudo pra medir o modelo isolado (green/red/void, entry_odd).
- Placar considera só green+red (void fora). ROI calculável pela `entry_odd` real.
- (Opcional, fora do escopo inicial) relatório diário do FREE.

## Tratamento de erros
- Falha no fluxo FREE nunca derruba M1/M2/M3: hooks em try/except com log.
- `FREE_MODEL_ENABLED=false` ⇒ nenhum pré-alerta FREE, comportamento atual intacto.
- Nenhuma mudança no comportamento live de M1/M2/M3.

## Testes
- `AlertFreeRepository`: create/update_odds/validate/get_unvalidated.
- Regra de status (unidade pura): odd≥1.70+bateu→green; ≥1.70+não→red; nunca 1.70→void.
- Rastreamento de odd: entry_odd grava na 1ª vez ≥1.70; max_odd acumula; nunca ≥1.70 → entry_odd None.
- Telegram: `send_watch_free` vai pro FREE group; mensagem contém "odd mínima 1.70" e NÃO contém termos do método (volta/G1/G2/perdedor).
- Regressão: suíte completa; `FREE_MODEL_ENABLED=false` = sem mudança.

## Fora de escopo (YAGNI)
- Cap de volume (owner quer todos).
- Odd de entrada por "pico" (é a 1ª vez que cruza 1.70).
- Relatório diário FREE (pode vir depois).
- Alterar o fluxo FREE atual do M1 (`send_alert_free`) — o modelo novo é separado.
