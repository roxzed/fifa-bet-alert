# Watch preditivo (fallback) — pré-alerta imune à oscilação do upcoming

**Data:** 2026-07-14
**Status:** Aprovado pelo owner

## Problema

Os pré-alertas (watch) M1/M2/M3 dependem de a volta (G2) ser casada pelo
pair_matcher **antes** do kickoff. O casamento depende de a volta aparecer no
`get_upcoming_events` da BetsAPI. Foi provado (chamadas diretas à API, mesmo
código) que a BetsAPI **oscila** a publicação dos jogos futuros da liga
Esoccer Battle 22614: ora retorna 0 eventos upcoming, ora retorna vários.
Quando o upcoming está vazio, a volta só é casável quando já entrou in-play
(depois do kickoff), e o watch T-30s aborta com "kickoff ja passou".

Resultado observável: os pré-alertas somem de forma intermitente ("começou e
parou do nada"), fora do nosso controle.

## Objetivo

Tornar o pré-alerta **imune à oscilação do upcoming**: um caminho de
**fallback preditivo** que dispara o pré-alerta baseado no horário PREVISTO da
volta quando a API não a expõe a tempo. O caminho normal (via API) continua
sendo o primário; o preditivo só entra quando o normal falha.

## Escopo

Os três métodos ganham o fallback, cada um no destino atual:
- **M1 preditivo → grupo VIP** (`TELEGRAM_CHAT_ID`, formato atual)
- **M2 preditivo → DM do owner** (`TELEGRAM_ADMIN_CHAT_ID`, `to_admin=True`)
- **M3 preditivo → DM do owner** (`TELEGRAM_M3_CHAT_ID`)

Fora de escopo: o **alerta live com odds** continua dependendo do in-play
(precisa das odds reais da bet365). O fallback cobre apenas o **pré-alerta**.

## Mecanismo

Quando o G1 termina e a volta NÃO é casada de imediato (vai pro `_pending` do
pair_matcher, como hoje), agenda-se em paralelo um **watch preditivo** por
método:

```
G1 termina → pair_matcher tenta casar a volta
    ├── casou via API (upcoming disponível): fluxo normal — start_monitoring
    │   agenda os watch loops reais (M1/M2/M3) com o return_match real.
    └── NÃO casou (foi pro _pending): agenda watch preditivo por método.
        Cada preditivo:
          1. dorme até T-30s do horario_previsto
             (horario_previsto = g1.started_at + mediana_offset)
          2. ao acordar, checa: a volta ja foi casada pela API?
             - SIM  → aborta (o watch real ja cuidou / vai cuidar)
             - NAO  → monta return_match SINTETICO e envia o pre-alerta
                      no destino do metodo, com auto-delete de 5 min.
```

### Estimador do offset (mediana real)
- `mediana_offset` = mediana de `Match.time_between_games` (ou
  `G2.started_at - G1.started_at`) sobre os pares históricos válidos
  (intervalo entre 20 e 120 min pra descartar outliers/erros).
- Computado no **startup** e cacheado. Fallback pra 58 min se não houver
  dados suficientes (< 20 pares).
- Consulta eficiente (agregação SQL / campo já populado) — nunca N+1.

### Return match sintético
Objeto leve com os campos que `predict_watch_candidate` /
`send_watch*` consomem, derivados do G1:
- `id = None` (sintético; nunca persistido)
- `player_home`, `player_away` = jogadores do G1 **invertidos** (a volta troca
  mando; mas os watch loops usam target_player = perdedor do G1, então a
  inversão só afeta o rótulo home/away exibido)
- `started_at` = `horario_previsto`
- `team_home`, `team_away` = times do G1 invertidos
- `is_return_match = True`

### Coordenação anti-duplicata
Trava por `(game1_id, metodo)`:
- Se a volta é casada via API antes do disparo preditivo → o preditivo daquele
  método é cancelado (verifica no pair_matcher/monitor se já há
  monitoramento/return_match real pro par).
- Se o preditivo dispara → marca `_watch_predito_sent[(game1_id, metodo)]` pra
  que o watch loop real (quando a volta casar depois, in-play) **não repita** o
  pré-alerta daquele método.
- O alerta **live** (não-watch) não é afetado pela trava — segue normal.

## Arquitetura (unidades)

| Unidade | Responsabilidade |
|---|---|
| `src/core/return_offset.py` (novo) | `estimate_return_offset(match_repo) -> float` (minutos, mediana). Cache no processo. Função isolada e testável. |
| `src/core/synthetic_match.py` (novo) | `build_synthetic_return(game1_match, started_at)` → objeto com os campos consumidos pelos watch loops. Puro, testável. |
| `src/core/odds_monitor.py` (modif.) | Agendar os watch preditivos quando a volta não casa; trava anti-duplicata; reusar `_watch_loop*` com o return_match sintético OU um `_predictive_watch_loop` dedicado por método. |
| `src/core/pair_matcher.py` (modif.) | No ponto onde adiciona ao `_pending`, sinalizar o odds_monitor pra agendar os preditivos (ou expor o horário previsto). |
| `src/main.py` (modif.) | Computar `mediana_offset` no startup e injetar no odds_monitor. |
| `src/config.py` (modif.) | `WATCH_PREDICTIVE_ENABLED: bool = True`; `WATCH_RETURN_OFFSET_FALLBACK_MIN: float = 58`. |

Decisão de design a resolver no plano: reusar os `_watch_loop*` existentes
passando o return_match sintético (menos código, mas os loops têm lógica de
SHADOW/tier acoplada) vs. um `_predictive_watch_loop` enxuto por método. O
plano escolhe a de menor acoplamento após ler os loops reais.

## Configuração (.env)
```
WATCH_PREDICTIVE_ENABLED=true          # liga/desliga o fallback preditivo
WATCH_RETURN_OFFSET_FALLBACK_MIN=58    # offset default se sem dados historicos
```
Destinos reusam as env vars existentes (TELEGRAM_CHAT_ID / ADMIN / M3).

## Tratamento de erros
- Falha no preditivo (qualquer método) nunca derruba o monitor nem o fluxo
  normal: try/except com log, como os watch loops atuais.
- `predict_watch_candidate` retornando None (nenhuma linha qualifica) → não
  envia, loga o motivo (consistente com hoje).
- Se `WATCH_PREDICTIVE_ENABLED=false`, comportamento idêntico ao atual.

## Testes
- `return_offset`: mediana correta; fallback com < 20 pares; ignora outliers.
- `synthetic_match`: campos derivados corretos (jogadores/times invertidos,
  started_at = previsto).
- Coordenação anti-duplicata: preditivo cancela se volta casou; watch real não
  repete se preditivo já enviou (testável com flags mockadas).
- Destinos: M1 preditivo → chat_id VIP; M2/M3 preditivo → DM
  (reusa o padrão de mock de `tests/test_send_watch.py` / `test_telegram_v3.py`).
- Regressão: suíte completa verde; `WATCH_PREDICTIVE_ENABLED=false` = sem mudança.

## Fora de escopo (YAGNI)
- Alerta live preditivo (impossível sem odds da API).
- Reagendar/corrigir o horário do preditivo se a volta atrasar (dispara uma vez
  no T-30s previsto; a imprecisão é aceita).
- Cobrir ligas além da 22614.
