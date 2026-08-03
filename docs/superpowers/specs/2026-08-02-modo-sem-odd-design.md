# Modo "sem odd" — bot sem a API cara (bet365 ao vivo)

**Data:** 2026-08-02
**Status:** Aprovado pelo owner

## Contexto / problema

O bot depende de **duas famílias de endpoint da BetsAPI**:
- **Padrão (barata):** `/events/upcoming|inplay|ended`, `/event/odds` — detecta jogos e traz o placar/resultado. Alimenta game_watcher, pair_matcher e a validação green/red.
- **bet365 premium (cara):** `/bet365/inplay_filter` + `/bet365/event` — única fonte das **odds ao vivo dos gols individuais do jogador** (`_fetch_loser_odds` no `odds_monitor`). Cobrada por request; o poll a cada ~3s dispara milhares de chamadas = a conta que o owner vai cortar.

O owner **vai parar de pagar o bet365 premium**. O bot (atualmente parado — container derrubado por sinal) precisa **voltar ao ar sem chamar o premium**, mantendo o mesmo padrão de produto: **pré-alerta T-30s com a linha + resultado GREEN/RED editando a mensagem**, agora **sem odd**.

## Objetivo

Um **modo "sem odd"** (flag `BET365_LIVE_ODDS_ENABLED=false`) em que o bot:
- **Não faz nenhuma chamada ao bet365 premium** (zero gasto na API cara).
- Manda as **tips pelo critério estatístico** (histórico no DB) nos 4 destinos: **M1→VIP, M2→DM, M3→DM, FREE→grupo grátis**.
- **Persiste cada tip** e a valida **GREEN/RED pelo placar real** (`/events/ended`, API barata), editando a mensagem.
- É **reversível** (`=true` volta ao modo com odds se o premium voltar).

## Fato-chave que viabiliza (baixo risco)

- Os **watch loops (pré-alertas)** já decidem a linha pelo **critério estatístico** (`predict_watch_candidate` do M1/M2, `evaluate` do M3, `prealert` do FREE) usando dados históricos e odds-alvo **hipotéticas** — **não** a odd ao vivo. Logo, os pré-alertas **já funcionam sem o premium**.
- Cada método já tem **modelo + validator** que validam **pelo placar** (`actual_goals` vs threshold da linha), não pela odd: `Alert`/`Validator` (M1), `AlertV2`/`ValidatorV2` (M2), `AlertV3`/`ValidatorV3` (M3), `AlertFree`/`ValidatorFree` (FREE). Todos rodam sob `_supervised_task`.
- O que hoje **cria** o registro é o *alerta live* (que usa odd). No modo sem-odd, quem cria passa a ser o **watch (pré-alerta)**.

## Regras

1. `BET365_LIVE_ODDS_ENABLED: bool = False` (novo default — modo sem odd ligado).
2. **Corte do premium:** com a flag off, `_fetch_loser_odds` retorna vazio **sem chamar** `bet365_get_inplay_esoccer`/`bet365_get_player_goals_odds`. O `_monitor_loop` não obtém odds → **nenhum alerta live de edge** (M1/M2/M3 live) e **nenhum tracking de odd** (FREE) rodam. Não há chamada ao `/bet365/*`.
3. **Watches persistem a tip:** cada watch loop (M1/M2/M3/FREE), ao enviar o pré-alerta, **cria o registro do próprio método** (odd/campos de odd nulos) guardando o `telegram_message_id` da mensagem do pré-alerta. Dedup: 1 tip por (match, método).
   - M1/M2/M3: persiste a(s) linha(s) previstas pelo `predict_watch_candidate`/`evaluate` (a de maior probabilidade quando o método escolhe uma; as previstas quando lista várias).
   - FREE: já persiste (`AlertEngineFree.prealert`) — só remove a dependência de odd/void.
4. **Validação GREEN/RED pelo placar:** os validators existentes validam cada tip pelos gols reais e **editam a mensagem do pré-alerta** (via o `telegram_message_id` guardado) com o resultado. **Sem odd, sem VOID** (FREE deixa de anular por odd — vira GREEN/RED puro).
5. **Mensagens sem odd:** pré-alerta e resultado **não exibem odd** nem gate de edge. Regra pública do FREE mantida (sem revelar método). Auto-delete de void do FREE deixa de existir (não há mais void).
6. **Nenhuma mudança** no game_watcher/pair_matcher (usam a API barata) nem na detecção. O `_supervised_task` dos validators segue igual.

## Arquitetura (reuso máximo)

Decisão de design: **reusar os modelos e validators de cada método** (não reescrever), plugando a criação do registro no watch e formatters de resultado sem-odd.

| Unidade | Mudança |
|---|---|
| `src/config.py` | `bet365_live_odds_enabled: bool = False`. |
| `src/core/odds_monitor.py` — `_fetch_loser_odds` | Se `not settings.bet365_live_odds_enabled`: retorna vazio imediatamente (sem tocar o premium). Guard no topo. |
| `src/core/odds_monitor.py` — watch loops | Cada `_watch_loop*` passa a persistir a tip (chamando o engine/repo do método) com odd nula + o `message_id` do pré-alerta. FREE já persiste. |
| `AlertEngine*` (M1/M2/M3) | Um caminho "persistir pré-alerta sem odd" (cria o registro com odds nulas, `suppressed=False`, sem gate de edge) — reusa `create` do repositório de cada método. |
| Validators (M1/M2/M3/FREE) | Já validam por placar. Ajuste: usar formatter de **resultado sem odd** ao editar; FREE sem VOID. |
| `messages.py` | Formatters de pré-alerta e resultado **sem odd** por método (ou reuso dos do FREE/watch que já não mostram odd real). |
| `src/main.py` | Nenhuma nova wiring obrigatória (flag lida via settings); confirmar que os validators sobem normalmente. |

**Ponto de decisão pro plano:** avaliar se os pré-alertas M1/M2/M3 hoje já não exibem a odd real (usam odd-alvo) — se exibem odd-alvo, remover da mensagem no modo sem-odd. O plano lê os `send_watch*`/`format_watch*` reais e ajusta.

## O que se mantém / se perde

- **Mantém:** detecção de jogos, 4 grupos, pré-alerta T-30s, GREEN/RED editando, relatório diário, mesmo padrão, reversibilidade.
- **Perde (por não ter odd):** odd nas mensagens, gate de edge/EV (alerta live M1/M2), regra de odd 1.70 do FREE (vira GREEN/RED puro).

## Tratamento de erros

- Flag off: `_fetch_loser_odds` no-op **nunca** chama o premium (garante zero gasto). Teste cobre isso.
- Persistência do watch em try/except: falha não derruba o pré-alerta nem o monitor.
- `BET365_LIVE_ODDS_ENABLED=true` restaura 100% o comportamento atual (com odds) — modo sem-odd é aditivo/reversível.

## Testes

- `_fetch_loser_odds` com flag off: retorna vazio e **não chama** `bet365_get_inplay_esoccer`/`bet365_get_player_goals_odds` (mock; assert não-chamado).
- Watch persiste a tip com `telegram_message_id` e odd nula (por método).
- Validator valida GREEN/RED por placar sem odd; FREE sem VOID.
- Mensagens sem odd (nenhum valor de odd no texto; FREE sem revelar método).
- Regressão: suíte completa; `BET365_LIVE_ODDS_ENABLED=true` = comportamento atual intacto.

## Fora de escopo (YAGNI)

- Buscar odds de outra fonte (não há substituto barato pros gols do jogador).
- Reescrever os 4 pipelines num só (reuso dos existentes é menor risco pra voltar rápido).
- Remover o código do bet365 premium (fica dormante atrás da flag, pra reativar se o premium voltar).
