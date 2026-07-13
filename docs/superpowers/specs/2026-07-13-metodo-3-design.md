# Método 3 (M3) — Alertas por frequência H2H

**Data:** 2026-07-13
**Status:** Aprovado pelo owner

## Objetivo

Terceiro método de alerta do fifa-bet-alert, rodando em paralelo ao M1 e M2 sem
tocá-los. Alertas vão para o **privado do owner** (não há grupo novo): método em
fase de validação pessoal, sem exposição a assinantes.

## Hipótese / Regra do método

Para o **perdedor do G1** no jogo de volta (G2), uma linha de over de gols
individuais (1.5 / 2.5 / 3.5 / 4.5) qualifica quando **ambos** os critérios passam:

1. **Frequência histórica:** nos **últimos 20 jogos H2H** entre os dois jogadores
   (qualquer jogo — ida ou volta, ganhando ou perdendo), o perdedor do G1 bateu a
   linha em **≥ 60%** dos jogos.
2. **Recência:** nos **últimos 7 jogos H2H** (mesmo recorte, os 7 mais recentes),
   a linha bateu em **≥ 5**.

- Amostra mínima: **10 H2H**. Entre 10 e 20 jogos, o percentual é calculado sobre
  os jogos existentes. Abaixo de 10, o confronto não é avaliado pelo M3.
- Gols são sempre **individuais do jogador** (mercado "Time (Jogador) — Gols"),
  nunca gols totais da partida.
- **Todas** as linhas que qualificarem aparecem no alerta (não só a melhor).

Nota consciente do owner: o M3 é filtro de frequência pura, **sem cálculo de
edge/EV**. Por isso o destino é o privado — validar ROI com dados reais antes de
qualquer exposição a assinantes (mínimo ~90 dias).

## Fluxo

```
G1 termina → perdedor identificado → volta encontrada (PairMatcher)
    ├── T-90s antes do kickoff: PRÉ-AVISO no privado (linhas aprovadas + taxas,
    │   sem odd — mercado fechado). Auto-delete em 5 min.
    └── Live (mercado de gols do jogador abre na bet365):
        ALERTA FINAL com cada linha aprovada + odd real.
        Gate de odds: 1.60 ≤ odd ≤ 4.00 (mesmo do sistema).
            └── Pós-jogo: mensagem editada com ✅ GREEN / ❌ RED por linha.
```

## Arquitetura (Abordagem A — espelho do M2)

O M2 já estabelece o padrão de método paralelo; o M3 replica:

| Unidade | Responsabilidade |
|---|---|
| `src/core/stats_engine_v3.py` | Avaliação pura do critério: busca últimos 20 H2H no DB, calcula taxa por linha + hits nos últimos 7, retorna linhas qualificadas. Sem I/O de Telegram. |
| `src/core/alert_engine_v3.py` | Orquestra: recebe oportunidade do odds_monitor, chama stats v3, aplica gate de odds, persiste alerta (tag `method=3`) e envia via notifier. Deduplica (1 alerta por match). |
| `src/core/validator_v3.py` | Pós-jogo: resultado real por linha, edita mensagem GREEN/RED, atualiza stats. |
| `src/telegram/bot.py` | `send_watch_v3`, `send_alert_v3`, `edit_alert_v3_result` — destino `telegram_m3_chat_id`, circuit breaker próprio (`_breaker_v3`). |
| `src/core/odds_monitor.py` | Hooks nos pontos onde o M2 já se pendura: task de watch v3 (T-90s) e avaliação v3 no loop de odds live. `alert_engine_v3` opcional (None = M3 desligado). |
| `src/main.py` | Instancia e liga os módulos v3 quando `TELEGRAM_M3_CHAT_ID` configurado. |

**Princípio:** nenhuma mudança de comportamento em M1/M2. Os hooks no
odds_monitor são aditivos e protegidos (`if self.alert_engine_v3:`).

## Configuração (.env)

```
TELEGRAM_M3_CHAT_ID=6034412176   # privado do owner; vazio = M3 desligado
M3_MIN_PROB=0.60                 # taxa mínima na janela de 20
M3_H2H_WINDOW=20                 # janela principal
M3_RECENT_WINDOW=7               # janela de recência
M3_RECENT_MIN_HITS=5             # hits mínimos na janela de recência
M3_MIN_H2H=10                    # amostra mínima de H2H
```

Thresholds calibráveis sem deploy de código, via pydantic-settings como o resto.

## Dados

- A tabela `Match` já registra todos os jogos encerrados da liga (não só G2 de
  perdedores) — a consulta "últimos N H2H entre A e B" usa dados existentes.
- Consulta H2H: jogos onde {A, B} são os dois jogadores, ordenados por data desc,
  limit 20, extraindo os gols do jogador-alvo em cada jogo.
- Alertas M3 persistidos com identificação de método para ROI isolado no
  dashboard/relatórios desde o primeiro dia.

## Mensagens (privado do owner)

- **Pré-aviso (T-90s):** jogadores, kickoff, e por linha aprovada: taxa nos 20
  (ex: `O2.5 70% (14/20) | últimos 7: 5/7`). Auto-delete 5 min.
- **Alerta live:** mesmo conteúdo + odd real de cada linha aprovada. Sem
  auto-delete (recebe edição de resultado).
- **Resultado:** edição com GREEN/RED por linha e placar de gols do jogador.
- Formato pode ser técnico (é o privado do owner — jargão liberado, diferente
  da regra de copy pública do LENDA).

## Tratamento de erros

- Circuit breaker dedicado (5 falhas → 60s cooldown), como M1/M2/FREE.
- Falha no M3 nunca derruba M1/M2: hooks embrulhados em try/except com log.
- H2H insuficiente ou critérios reprovados: log em nível INFO com os números
  (diagnóstico de "por que não alertou" — lição do bug do watch de 2026-07-13).

## Testes

- `stats_engine_v3`: unidade pura — casos: 20+ H2H passa/reprova, janela 10–19,
  <10 skip, recência 5/7 exata, 4/7 reprova, múltiplas linhas qualificadas.
- `bot.py` v3: mock do Bot (padrão de `tests/test_send_watch.py`) — envio pro
  chat certo, NO-OP sem chat_id, breaker.
- `alert_engine_v3`: gate de odds e deduplicação.

## Fora de escopo (YAGNI)

- Grupo Telegram próprio pro M3 (decisão: privado do owner).
- Cálculo de edge/EV no M3 (decisão consciente do owner).
- Backtest histórico automatizado do M3 (pode ser script posterior).
- Refatoração de M1/M2 para abstração comum de métodos.
