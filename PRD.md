# PRD - FIFA Bet Alert System

## 1. Visao Geral

**Nome do Projeto:** FIFA Bet Alert
**Autor:** Plini
**Data:** 2026-03-24
**Status:** Em Planejamento

### Resumo Executivo

Sistema automatizado de monitoramento e alerta para apostas no mercado de FIFA eSports (bet365). O sistema identifica oportunidades de aposta baseadas no metodo do "jogador perdedor" - quando um jogador perde o primeiro jogo, existe uma tendencia estatistica de que ele faca mais gols no jogo de volta (55-60 min depois), favorecido pelo handicap.

O sistema monitora jogos da liga **Esoccer Battle - 8 mins play** em tempo real, rastreia resultados do primeiro jogo, identifica o jogador perdedor, monitora as odds do over 2.5 gols individuais no jogo de volta, e envia alertas via Telegram quando os criterios sao atendidos.

---

## 2. Problema

### Situacao Atual
- O usuario monitora manualmente dezenas de jogos por dia, 24h
- Precisa lembrar quem perdeu o primeiro jogo e ficar esperando 55-60 min pelo jogo de volta
- Precisa acompanhar as linhas de odds manualmente na bet365
- Perde oportunidades por nao estar disponivel no momento exato
- Nao tem dados historicos organizados para validar/refinar o metodo

### Oportunidade
- Automatizar 100% do monitoramento
- Nunca perder uma oportunidade de entrada
- Coletar dados para validacao estatistica continua do metodo
- Escalar para multiplas ligas simultaneamente

---

## 3. Objetivos e Metricas de Sucesso

### Objetivos Primarios
1. **Automatizar deteccao** de jogadores perdedores no primeiro jogo
2. **Monitorar odds** do over 2.5 gols do jogador perdedor no jogo de volta
3. **Alertar via Telegram** com todas as infos necessarias para tomada de decisao
4. **Operar 24/7** sem intervencao manual

### Objetivos Secundarios
5. **Coletar e armazenar** todos os dados para analise historica
6. **Dashboard de validacao** do metodo com estatisticas de acerto
7. **Score de confianca** para cada alerta baseado em padroes historicos
8. **Expansao** para outras ligas e mercados

### Metricas de Sucesso
| Metrica | Alvo |
|---------|------|
| Uptime do sistema | > 99% |
| Latencia do alerta (antes do jogo) | >= 1 min antes do kickoff |
| Taxa de oportunidades capturadas | > 95% |
| Falsos positivos | < 5% |
| Tempo de setup por liga | < 5 min |

---

## 4. Publico-Alvo

**Usuario unico:** Plini - apostador profissional que opera manualmente na bet365 no mercado FIFA eSports.

**Necessidades:**
- Receber alertas precisos e a tempo no Telegram
- Ter contexto suficiente no alerta para decidir rapidamente
- Confiar nos dados e na logica do sistema
- Acompanhar performance do metodo ao longo do tempo

---

## 5. Escopo do Produto

### 5.1 Funcionalidades Core (MVP)

#### F1 - Monitoramento de Jogos
- Conectar na API do BetsAPI (ou alternativa)
- Monitorar todos os jogos da liga Esoccer Battle - 8 mins play
- Identificar pares de jogos (ida e volta) automaticamente
- Detectar quando um jogo termina e registrar o resultado
- Mapear o jogador perdedor

#### F2 - Monitoramento de Odds
- Acompanhar as odds do over do jogador perdedor no jogo de volta
- Detectar quando a linha de over 2.5 gols individuais esta disponivel
- Detectar quando a linha cai de 3.5 para 2.5 (indicador de mercado favoravel)
- Alertar tambem no 3.5 se score de confianca for alto

#### F3 - Sistema de Alertas (Telegram)
- Bot Telegram dedicado
- Alerta enviado >= 1 minuto antes do jogo de volta comecar
- Informacoes no alerta:
  - Nome dos jogadores (Jogador A vs Jogador B)
  - Resultado do jogo 1 (ex: 1-3)
  - Quem perdeu o jogo 1
  - Odd atual do over 2.5 do jogador perdedor
  - Odd do over 3.5 (se disponivel)
  - Horario do jogo de volta
  - Score de confianca (baseado em historico)
  - Link direto para o evento (se possivel)

#### F4 - Armazenamento de Dados
- Salvar todos os jogos monitorados (resultado jogo 1, resultado jogo 2)
- Salvar odds no momento do alerta
- Salvar se o over bateu ou nao (resultado real)
- Base para validacao do metodo

### 5.2 Funcionalidades Avancadas (Pos-MVP)

#### F5 - Dashboard de Validacao
- Taxa de acerto do metodo (over 2.5 bateu? over 3.5 bateu?)
- Filtros por: periodo, jogador, placar do jogo 1, faixa de odds
- Graficos de evolucao temporal
- ROI simulado por faixa de odd

#### F6 - Score de Confianca Inteligente
- Analisar historico do jogador perdedor especifico
- Considerar margem de derrota no jogo 1 (perdeu de 1 vs perdeu de 4)
- Considerar odds de abertura vs odds atuais
- Considerar horario do dia (padroes de jogadores em diferentes turnos)
- Peso por volume de jogos do jogador

#### F7 - Alertas Inteligentes com Niveis
- **ALERTA VERDE** (confianca alta): over 2.5 disponivel, historico forte, odd boa
- **ALERTA AMARELO** (confianca media): over 3.5 disponivel, tendencia de queda
- **ALERTA VERMELHO** (oportunidade rara): todos os indicadores alinhados
- Customizar quais niveis receber

#### F8 - Multi-Liga
- Expandir para outras ligas Battle (10 min, 12 min)
- Expandir para Liga Pro
- Configuracao facil de novas ligas

#### F9 - Analise de Jogadores
- Perfil de cada jogador com historico
- Jogadores que mais "reagem" apos perder
- Jogadores que tendem a manter o padrao de derrota
- Blacklist/whitelist de jogadores

#### F10 - Relatorios Automaticos
- Relatorio diario no Telegram com resumo:
  - Quantos alertas enviados
  - Quantos bateram (over confirmado)
  - Taxa de acerto do dia
  - Melhor e pior resultado
- Relatorio semanal/mensal com tendencias

---

## 6. Arquitetura Tecnica

### 6.1 Stack Tecnologica

| Componente | Tecnologia | Justificativa |
|------------|-----------|---------------|
| Linguagem | Python 3.11+ | Melhor ecossistema para automacao, APIs, data analysis |
| Framework Async | asyncio + aiohttp | Operacao concorrente eficiente para multiplos jogos |
| Banco de Dados | SQLite (MVP) → PostgreSQL (escala) | Simples para MVP, robusto para escala |
| ORM | SQLAlchemy | Flexibilidade e migracao facil entre DBs |
| Telegram Bot | python-telegram-bot | Lib madura e bem documentada |
| Scheduler | APScheduler | Agendamento robusto de tarefas periodicas |
| API Client | httpx | HTTP client async moderno |
| Logging | loguru | Logging simplificado e poderoso |
| Config | pydantic-settings | Validacao de configuracoes com type safety |
| Dashboard (pos-MVP) | Streamlit ou Grafana | Visualizacao rapida sem frontend complexo |

### 6.2 Arquitetura do Sistema

```
┌─────────────────────────────────────────────────────────────┐
│                    FIFA BET ALERT SYSTEM                     │
├─────────────────────────────────────────────────────────────┤
│                                                              │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐   │
│  │  Game Watcher │    │ Odds Monitor │    │Alert Engine  │   │
│  │              │    │              │    │              │   │
│  │ - Poll API   │───>│ - Track odds │───>│ - Evaluate   │   │
│  │ - Detect end │    │ - Detect 2.5 │    │ - Score calc │   │
│  │ - Map pairs  │    │ - Track drops│    │ - Send TG    │   │
│  └──────┬───────┘    └──────┬───────┘    └──────┬───────┘   │
│         │                   │                   │           │
│         v                   v                   v           │
│  ┌─────────────────────────────────────────────────────┐    │
│  │                   Data Store (SQLite)                │    │
│  │  - matches  - odds_history  - alerts  - players     │    │
│  └─────────────────────────────────────────────────────┘    │
│         │                                                    │
│         v                                                    │
│  ┌──────────────┐    ┌──────────────┐                       │
│  │  Validator   │    │  Reporter    │                       │
│  │              │    │              │                       │
│  │ - Track hits │    │ - Daily sum  │                       │
│  │ - Update DB  │    │ - Weekly RPT │                       │
│  │ - Stats calc │    │ - TG reports │                       │
│  └──────────────┘    └──────────────┘                       │
│                                                              │
└─────────────────────────────────────────────────────────────┘
         │                                    │
         v                                    v
  ┌──────────────┐                   ┌──────────────┐
  │  BetsAPI     │                   │  Telegram    │
  │  (Data Feed) │                   │  (Alerts)    │
  └──────────────┘                   └──────────────┘
```

### 6.3 Fluxo Principal

```
1. POLL: A cada 30s, busca jogos em andamento/finalizados na liga Battle 8 min
2. DETECT: Quando um jogo termina, registra o resultado e identifica o perdedor
3. MAP: Associa o par de jogadores ao proximo jogo (volta) ~55-60 min depois
4. WATCH: Monitora odds do proximo jogo, focando no over do jogador perdedor
5. EVALUATE: Quando over 2.5 esta disponivel (ou 3.5 caindo), avalia criterios
6. ALERT: Se criterios atendidos, envia alerta no Telegram >= 1 min antes do jogo
7. VALIDATE: Apos jogo de volta terminar, registra se over bateu ou nao
8. REPORT: Gera relatorios periodicos com performance do metodo
```

### 6.4 Modelo de Dados

```sql
-- Jogadores conhecidos e seus perfis (com estatisticas detalhadas)
CREATE TABLE players (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    -- Contadores gerais
    total_games INTEGER DEFAULT 0,
    total_losses INTEGER DEFAULT 0,
    total_return_matches INTEGER DEFAULT 0,  -- jogos de volta apos derrota
    -- Over stats apos derrota
    over25_after_loss INTEGER DEFAULT 0,
    over35_after_loss INTEGER DEFAULT 0,
    hit_rate_25 REAL DEFAULT 0,              -- over25_after_loss / total_return_matches
    hit_rate_35 REAL DEFAULT 0,
    avg_goals_after_loss REAL DEFAULT 0,
    -- Stats por tipo de derrota
    tight_loss_count INTEGER DEFAULT 0,      -- derrotas por 1 gol
    tight_loss_over25 INTEGER DEFAULT 0,
    medium_loss_count INTEGER DEFAULT 0,     -- derrotas por 2 gols
    medium_loss_over25 INTEGER DEFAULT 0,
    blowout_loss_count INTEGER DEFAULT 0,    -- derrotas por 3+ gols
    blowout_loss_over25 INTEGER DEFAULT 0,
    -- Confiabilidade
    is_reliable BOOLEAN DEFAULT FALSE,       -- tem >= 10 jogos apos derrota
    reliability_score REAL DEFAULT 0,        -- 0-100 baseado em volume e consistencia
    -- Metadata
    last_seen TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Cada jogo monitorado (dados detalhados)
CREATE TABLE matches (
    id INTEGER PRIMARY KEY,
    api_event_id TEXT UNIQUE,          -- ID do evento na API
    league TEXT NOT NULL,
    player_home TEXT NOT NULL,
    player_away TEXT NOT NULL,
    team_home TEXT,                     -- time/equipe escolhida (ex: Real Madrid)
    team_away TEXT,                     -- time/equipe escolhida (ex: Barcelona)
    score_home INTEGER,
    score_away INTEGER,
    score_home_ht INTEGER,             -- placar no intervalo (se disponivel)
    score_away_ht INTEGER,
    -- Stats extras (se disponivel da API)
    corners_home INTEGER,
    corners_away INTEGER,
    shots_home INTEGER,
    shots_away INTEGER,
    possession_home INTEGER,           -- % posse
    possession_away INTEGER,
    cards_home INTEGER,
    cards_away INTEGER,
    -- Contexto
    status TEXT DEFAULT 'scheduled',   -- scheduled, live, ended
    started_at TIMESTAMP,
    ended_at TIMESTAMP,
    day_of_week INTEGER,               -- 0=segunda, 6=domingo
    hour_of_day INTEGER,               -- 0-23
    -- Vinculo ida/volta
    pair_match_id INTEGER REFERENCES matches(id),
    is_return_match BOOLEAN DEFAULT FALSE,
    time_between_games INTEGER,        -- minutos entre fim do jogo 1 e inicio da volta
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Historico de odds monitoradas
CREATE TABLE odds_history (
    id INTEGER PRIMARY KEY,
    match_id INTEGER REFERENCES matches(id),
    player TEXT NOT NULL,              -- jogador que a odd se refere
    market TEXT NOT NULL,              -- 'over_2.5', 'over_3.5', etc
    odds_value REAL NOT NULL,
    captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Alertas enviados (com motor estatistico)
CREATE TABLE alerts (
    id INTEGER PRIMARY KEY,
    match_id INTEGER REFERENCES matches(id),
    losing_player TEXT NOT NULL,
    game1_score TEXT NOT NULL,          -- ex: '1-3'
    loss_margin INTEGER,               -- diferenca de gols no jogo 1
    loss_type TEXT,                     -- 'tight', 'medium', 'blowout', 'open'
    -- Odds no momento do alerta
    over25_odds REAL,
    over35_odds REAL,
    -- Motor estatistico
    implied_prob REAL,                 -- probabilidade implicita da odd
    true_prob REAL,                    -- probabilidade real calculada
    true_prob_conservative REAL,       -- limite inferior do IC 95%
    edge REAL,                         -- true_prob - implied_prob
    expected_value REAL,               -- EV da aposta
    kelly_fraction REAL,               -- % da banca sugerido (Kelly 25%)
    star_rating INTEGER,               -- 1 a 5 estrelas
    -- Camadas de probabilidade
    p_base REAL,                       -- prob global
    p_loss_type REAL,                  -- prob por tipo de derrota
    p_player REAL,                     -- prob do jogador especifico
    p_time_slot REAL,                  -- prob por faixa horaria
    p_market_adj REAL,                 -- ajuste de mercado
    player_sample_size INTEGER,        -- n jogos do jogador
    global_sample_size INTEGER,        -- n jogos globais
    confidence_interval_low REAL,      -- IC 95% inferior
    confidence_interval_high REAL,     -- IC 95% superior
    -- Metadata
    alert_level TEXT,                  -- green, yellow, red
    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    -- Resultado pos-jogo
    actual_goals INTEGER,
    over25_hit BOOLEAN,
    over35_hit BOOLEAN,
    validated_at TIMESTAMP
);

-- Estatisticas globais do metodo (atualizado a cada validacao)
CREATE TABLE method_stats (
    id INTEGER PRIMARY KEY,
    stat_key TEXT NOT NULL UNIQUE,      -- ex: 'global', 'loss_tight', 'hour_18_00'
    stat_type TEXT NOT NULL,            -- 'global', 'loss_type', 'time_slot'
    total_samples INTEGER DEFAULT 0,
    over25_hits INTEGER DEFAULT 0,
    over35_hits INTEGER DEFAULT 0,
    hit_rate_25 REAL DEFAULT 0,
    hit_rate_35 REAL DEFAULT 0,
    avg_goals REAL DEFAULT 0,
    last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Regime de performance (detecta se metodo esta degradando)
CREATE TABLE regime_checks (
    id INTEGER PRIMARY KEY,
    checked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    window_size INTEGER,               -- ultimos N jogos analisados
    recent_rate REAL,                  -- taxa recente
    historical_rate REAL,              -- taxa historica
    z_score REAL,                      -- z-score do teste
    status TEXT,                       -- 'HEALTHY', 'WARNING', 'DEGRADED'
    action_taken TEXT                  -- o que o sistema fez
);

-- Simulacoes de ROI por estrategia
CREATE TABLE roi_simulations (
    id INTEGER PRIMARY KEY,
    strategy_name TEXT NOT NULL,        -- ex: 'all_alerts', '3star_plus', 'kelly'
    simulated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    total_bets INTEGER,
    wins INTEGER,
    losses INTEGER,
    win_rate REAL,
    roi REAL,
    profit_units REAL,
    max_drawdown REAL
);

-- Configuracoes por liga
CREATE TABLE leagues (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    api_league_id TEXT,
    is_active BOOLEAN DEFAULT TRUE,
    poll_interval_seconds INTEGER DEFAULT 30,
    return_match_delay_minutes INTEGER DEFAULT 55,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

---

## 7. Fonte de Dados

### Opcao Primaria: BetsAPI

- **Site:** https://betsapi.com
- **API Base:** `https://api.b365api.com/v3/` (ou v1 como fallback)
- **Autenticacao:** Token via header `X-API-TOKEN` ou query param `token=`
- **Liga confirmada no catalogo:** "Esoccer Battle - 8 mins play" (ID varia por ano)
- **Endpoints necessarios:**
  - `GET /events/upcoming` - jogos agendados (filtro por league_id)
  - `GET /events/inplay` - jogos ao vivo
  - `GET /events/ended` - jogos finalizados
  - `GET /event/view/{id}` - detalhes de um evento
  - `GET /event/odds` - odds de um evento
- **Rate limit:** ~3.600 req/hora (v1) ou ~200.000 req/hora (v3)
- **Preco:** Planos a partir de ~$20/mes (verificar site para valores atuais)

### Opcoes Alternativas

| API | URL | Pros | Contras |
|-----|-----|------|---------|
| 365OddsAPI | 365oddsapi.com | Foco em FIFA, real-time | Menos documentacao |
| The Odds API | the-odds-api.com | Boa documentacao, free tier | Pode nao cobrir Battle 8 min |
| OddsMatrix | oddsmatrix.com | 36+ mercados FIFA | Enterprise, pode ser caro |

### Recomendacao
Comecar com **BetsAPI** por ja ser familiar ao usuario e ter a liga Battle 8 min confirmada no catalogo. Avaliar 365OddsAPI como alternativa se BetsAPI nao fornecer odds individuais de jogador.

---

## 8. Integracao Telegram

### Setup do Bot
1. Criar bot via @BotFather no Telegram
2. Obter token do bot
3. Criar canal/grupo privado para alertas
4. Bot envia mensagens no canal

### Formato do Alerta (com Analise Estatistica Completa)

```
🔔 ALERTA FIFA BET ⭐⭐⭐⭐ (4/5) 🟢

📊 Jogo 1 (Finalizado):
   PlayerA 1 - 3 PlayerB
   Times: Real Madrid vs Barcelona
   Tipo: Derrota por 2 gols (padrao favoravel)

🎯 Jogo de Volta em: 14:32 (em 2 min)
   PlayerA vs PlayerB

📉 Over 2.5 gols PlayerA: @1.85
   • Prob. casa (implicita): 54.1%
   • Prob. real (nossa):     68.3%
   • Edge: +14.2% ✅
   • EV: +25.8% ✅
   • Kelly 25%: 7.6% da banca

📊 Historico:
   • Global apos derrota: 64.3% (n=2150)
   • PlayerA apos derrota: 75.0% (n=12)
   • Derrota por 2 gols: 68.0% (n=580)
   • Com Real Madrid: 71.2% (n=45)
   • IC 95%: [62.1% - 74.5%]

📈 Mercado: Odd caiu 2.10 → 1.85 ✅
🕐 Horario: 14h (faixa 12-18h: 69.8%)
⏰ Kickoff: 14:32 UTC-3
```

### Formato do Relatorio Diario

```
📋 RELATORIO DIARIO - 2026-03-24

📊 Resumo:
   • Alertas enviados: 12
   • Over 2.5 bateu: 8/12 (66.7%)
   • Over 3.5 bateu: 5/12 (41.7%)
   • ROI simulado (flat @1.80): +15.6%

🏆 Melhor resultado:
   PlayerX: 4 gols apos perder de 0-3

❌ Pior resultado:
   PlayerY: 1 gol apos perder de 1-2

📈 Acumulado do mes: 67.2% acerto over 2.5
```

---

## 9. Requisitos Nao-Funcionais

| Requisito | Especificacao |
|-----------|--------------|
| Disponibilidade | 24/7, restart automatico em caso de falha |
| Latencia | Alerta enviado >= 1 min antes do kickoff |
| Resiliencia | Retry automatico em falhas de API, queue de alertas |
| Logging | Log completo de todas as operacoes para debug |
| Configuracao | Todas as configs via .env ou arquivo YAML |
| Deploy | Docker container para facilitar deploy em VPS/cloud |
| Monitoramento | Health check endpoint + alerta se sistema cair |
| Seguranca | Tokens em variaveis de ambiente, nunca em codigo |

---

## 10. Riscos e Mitigacoes

| Risco | Impacto | Probabilidade | Mitigacao |
|-------|---------|---------------|-----------|
| BetsAPI nao fornece odds individuais de jogador | Alto | Media | Testar API antes de codar; ter 365OddsAPI como backup |
| API fora do ar | Alto | Baixa | Retry com backoff; monitorar uptime; fallback API |
| Mudanca na estrutura da liga | Medio | Baixa | Config flexivel de league_id; monitorar mudancas |
| Rate limit excedido | Medio | Media | Cache inteligente; ajustar polling interval |
| Identificacao incorreta de pares ida/volta | Alto | Media | Usar nome dos jogadores + janela de tempo para match |
| Odds mudam rapido demais | Medio | Alta | Polling frequente (15-30s); alertar range de odds |
| Bot Telegram bloqueado | Baixo | Baixa | Monitorar health do bot; fallback via email |

---

## 11. Cronograma Proposto

### Fase 1 - Fundacao (Semana 1)
- [ ] Setup do projeto (repo, estrutura, dependencias)
- [ ] Configuracao do bot Telegram
- [ ] Integracao basica com BetsAPI (autenticacao, listagem de ligas)
- [ ] Confirmar que a API retorna dados necessarios (odds, times, etc)

### Fase 2 - Core de Coleta (Semana 2-3)
- [ ] Database: modelos completos com todos os campos detalhados
- [ ] BetsAPI client: todos os endpoints necessarios
- [ ] Game Watcher: monitoramento de jogos em tempo real
- [ ] Pair Matcher: identificacao de jogos ida/volta
- [ ] Odds Monitor: rastreamento de odds
- [ ] Coleta de dados extras: times, stats, contexto
- [ ] Data Store: persistencia de todos os dados detalhados

### Fase 3 - Coleta Historica + Cold Start (Semana 3-4)
- [ ] Script de coleta historica: puxar 90-180 dias de dados
- [ ] Rodar coleta historica completa
- [ ] Sistema de coleta ao vivo rodando 24/7 em paralelo
- [ ] Relatorios semanais de progresso da coleta no Telegram
- [ ] Bot Telegram com comandos basicos (/status, /progress)

### Fase 4 - Motor Estatistico (Semana 4-5)
- [ ] probability.py: funcoes matematicas puras + testes
- [ ] stats_engine.py: motor com estado, integracao com DB
- [ ] alert_classifier.py: estrelas e niveis
- [ ] Backtesting completo com dados historicos coletados
- [ ] Calibracao de pesos via regressao logistica
- [ ] Relatorio de validacao do metodo

### Fase 5 - Sistema de Alertas (Semana 5-6)
- [ ] Alert Engine: integra stats_engine + Telegram
- [ ] Validator: validacao pos-jogo + atualizacao de stats
- [ ] Reporter: relatorios diarios/semanais
- [ ] Modo shadow (2 semanas de alertas sem apostar)

### Fase 6 - Go Live (Semana 8+)
- [ ] Ativar alertas reais apos validacao do modo shadow
- [ ] Monitoramento continuo de regime (degradacao?)
- [ ] Ajuste fino de thresholds com dados ao vivo
- [ ] Deteccao de regime funcionando

### Fase 7 - Expansao (Semana 10+)
- [ ] Dashboard de validacao (Streamlit)
- [ ] Multi-liga (Battle 10 min, Liga Pro)
- [ ] Analise avancada de jogadores e times
- [ ] Re-calibracao periodica de pesos (mensal)

---

## 12. Motor Estatistico e de Probabilidade (CORE)

> **Esta e a secao mais critica do sistema.** Sem ela, o sistema e apenas um notificador.
> Com ela, o sistema e uma ferramenta de geracao de lucro consistente.

### 12.1 Principio Fundamental: So Alertar Quando Ha EDGE

O sistema **NUNCA** deve alertar apenas porque a linha de over 2.5 abriu.
Deve alertar apenas quando:

```
Probabilidade Real (calculada por nos) > Probabilidade Implicita (da odd)
```

Se a odd do over 2.5 e @1.85, a casa esta dizendo que a probabilidade e ~54%.
Se nossos dados historicos dizem que a probabilidade real e 68%, entao temos um **edge de +14%**.
So ai faz sentido apostar.

### 12.2 Conversao de Odds em Probabilidade Implicita

```python
def implied_probability(decimal_odds: float) -> float:
    """Converte odds decimais em probabilidade implicita."""
    return 1 / decimal_odds

# Exemplos:
# @1.50 → 66.7% (casa acha que bate com frequencia)
# @1.85 → 54.1% (casa acha que e ~moeda)
# @2.20 → 45.5% (casa acha que nao bate na maioria)
# @3.00 → 33.3% (casa acha que raramente bate)
```

### 12.3 Calculo da Probabilidade Real (True Probability)

A probabilidade real e calculada em **camadas**, do mais geral ao mais especifico:

#### Camada 1 - Base Global (peso quando sem dados especificos)
```
P_base = total_over25_hits / total_return_matches_after_loss
```
- Calculado sobre TODOS os jogos de volta apos derrota na liga
- Precisa de minimo 100 amostras para ser confiavel
- Exemplo: 450 acertos em 700 jogos = 64.3%

#### Camada 2 - Por Tipo de Derrota (peso 25%)
```
P_derrota = over25_hits_por_tipo / total_por_tipo
```
- **Derrota apertada (1 gol diff):** ex: 55% over 2.5
- **Derrota media (2 gols diff):** ex: 68% over 2.5
- **Goleada (3+ gols diff):** ex: 62% over 2.5
- **Derrota com jogo aberto (total gols > 5):** ex: 72% over 2.5

#### Camada 3 - Por Jogador Especifico (peso 30% se n >= 10 jogos)
```
P_jogador = over25_hits_jogador / total_jogos_jogador_apos_derrota
```
- So usar se jogador tem >= 10 jogos apos derrota (significancia estatistica)
- Se < 10 jogos, reduzir peso proporcionalmente (ex: 5 jogos = peso 15%)
- Regressao ao media: misturar com P_base para evitar overfitting

#### Camada 4 - Por Faixa Horaria (peso 10%)
```
P_horario = over25_hits_faixa / total_faixa
```
- Faixas: madrugada (00-06), manha (06-12), tarde (12-18), noite (18-00)
- Jogadores diferentes jogam em horarios diferentes

#### Camada 5 - Time/Equipe Escolhida (peso 10%)
```
P_time = over25_rate do time usado pelo jogador perdedor na volta
P_matchup = over25_rate do matchup entre os dois times
```
- Se jogador usa seu time principal: bonus (mais familiaridade = mais gols)
- Se jogador mudou de time apos derrota: sinal de adaptacao (neutro a positivo)
- Se o time tem avg_goals_scored alto: favoravel para over
- Se o matchup historico tem muitos gols: bonus

#### Camada 6 - Movimento de Mercado (peso 5%)
```
P_mercado = ajuste baseado na direcao das odds
```
- Odd caindo: mercado concorda → bonus +3%
- Odd estavel: neutro → 0%
- Odd subindo: mercado discorda → penalidade -3%

#### Composicao Final (Probabilidade Composta)
```python
def calculate_true_probability(
    p_base: float,          # Camada 1 - Global
    p_derrota: float,       # Camada 2 - Tipo de derrota
    p_jogador: float,       # Camada 3 - Jogador especifico
    p_horario: float,       # Camada 4 - Faixa horaria
    p_time: float,          # Camada 5 - Time/equipe
    p_mercado_adj: float,   # Camada 6 - Movimento de mercado
    n_jogador: int,         # amostra do jogador
    n_time: int,            # amostra do time
    min_sample: int = 10
) -> float:
    # Peso do jogador escala com amostra (max 25%)
    w_jogador = min(0.25, 0.25 * (n_jogador / min_sample))
    # Peso do time escala com amostra (max 10%)
    w_time = min(0.10, 0.10 * (n_time / min_sample))
    # Base absorve o peso que jogador/time nao usam
    w_base = 1.0 - 0.25 - w_jogador - 0.10 - w_time - 0.05

    p_true = (
        w_base * p_base +              # ~25% (flexivel)
        0.25 * p_derrota +             # 25% fixo
        w_jogador * p_jogador +         # 0-25% (escala com dados)
        0.10 * p_horario +             # 10% fixo
        w_time * p_time +              # 0-10% (escala com dados)
        0.05 * (p_base + p_mercado_adj) # 5% fixo
    )

    return clip(p_true, 0.0, 1.0)
```

> **NOTA:** Os pesos devem ser recalibrados apos os 90 dias de coleta
> usando regressao logistica nos dados reais. Os valores acima sao
> estimativas iniciais que serao substituidas por coeficientes otimizados.

### 12.4 Calculo do Expected Value (EV)

```python
def expected_value(true_prob: float, decimal_odds: float) -> float:
    """
    EV positivo = aposta lucrativa a longo prazo.

    EV = (prob_acerto * lucro) - (prob_erro * perda)
    Para aposta de 1 unidade:
    """
    profit = decimal_odds - 1  # lucro se ganhar
    ev = (true_prob * profit) - ((1 - true_prob) * 1)
    return ev

# Exemplos:
# true_prob=0.68, odds=@1.85 → EV = (0.68 * 0.85) - (0.32 * 1) = +0.258 (+25.8%) ✅ EXCELENTE
# true_prob=0.55, odds=@1.85 → EV = (0.55 * 0.85) - (0.45 * 1) = +0.0175 (+1.75%) ⚠️ MARGINAL
# true_prob=0.50, odds=@1.85 → EV = (0.50 * 0.85) - (0.50 * 1) = -0.075 (-7.5%) ❌ NAO APOSTAR
```

### 12.5 Calculo do Edge (Vantagem)

```python
def edge(true_prob: float, decimal_odds: float) -> float:
    """
    Edge = nossa probabilidade - probabilidade implicita da casa.
    Edge > 0 = temos vantagem.
    """
    implied = 1 / decimal_odds
    return true_prob - implied

# Exemplos:
# true_prob=0.68, odds=@1.85 → edge = 0.68 - 0.54 = +14% ✅
# true_prob=0.55, odds=@1.85 → edge = 0.55 - 0.54 = +1%  ⚠️
# true_prob=0.50, odds=@1.85 → edge = 0.50 - 0.54 = -4%  ❌
```

### 12.6 Kelly Criterion (Gestao de Banca)

```python
def kelly_fraction(true_prob: float, decimal_odds: float) -> float:
    """
    Fracao ideal da banca para apostar.
    Kelly = (bp - q) / b
    onde b = odds-1, p = prob acerto, q = prob erro
    """
    b = decimal_odds - 1
    p = true_prob
    q = 1 - p
    kelly = (b * p - q) / b
    return max(0, kelly)  # nunca negativo

def fractional_kelly(true_prob: float, decimal_odds: float, fraction: float = 0.25) -> float:
    """
    Kelly fracionado (mais conservador).
    Usar 25% do Kelly para proteger a banca de variancia.
    """
    return kelly_fraction(true_prob, decimal_odds) * fraction

# Exemplo: true_prob=0.68, odds=@1.85
# Kelly completo: (0.85*0.68 - 0.32) / 0.85 = 30.4% da banca
# Kelly 25%: 7.6% da banca por aposta (RECOMENDADO)
```

### 12.7 Thresholds (Limites Minimos para Alertar)

| Parametro | Valor Minimo | Justificativa |
|-----------|-------------|---------------|
| Edge minimo | >= 5% | Abaixo disso nao compensa o risco |
| EV minimo | >= +3% | Margem minima de lucro esperado |
| Probabilidade real minima | >= 55% | Abaixo disso a variancia e muito alta |
| Historico minimo antes de operar | >= 90 dias de dados | Base estatistica solida antes de alertar |
| Amostra minima (global) | >= 500 jogos | Para P_base ser minimamente confiavel (90 dias gera ~2000+) |
| Amostra minima (jogador) | >= 5 jogos | Abaixo disso, ignorar camada do jogador |
| Odd minima | >= 1.40 | Abaixo disso, retorno nao compensa |
| Odd maxima | <= 4.00 | Acima disso, probabilidade real e muito baixa |
| Kelly fraction | 25% | Protecao contra variancia |

### 12.8 Sistema de Rating de Oportunidade

Cada alerta recebe um rating de 1 a 5 estrelas:

```
⭐         Edge 5-8%,   EV 3-5%    → "Vale se nao tiver nada melhor"
⭐⭐       Edge 8-12%,  EV 5-10%   → "Oportunidade solida"
⭐⭐⭐     Edge 12-18%, EV 10-15%  → "Boa oportunidade"
⭐⭐⭐⭐   Edge 18-25%, EV 15-25%  → "Oportunidade forte"
⭐⭐⭐⭐⭐ Edge >25%,   EV >25%    → "Oportunidade rara - maxima confianca"
```

### 12.9 Intervalo de Confianca

Nao basta calcular a probabilidade - precisamos saber o quanto confiamos nela.

```python
import math

def confidence_interval(successes: int, total: int, z: float = 1.96) -> tuple:
    """
    Intervalo de confianca de 95% (Wilson score interval).
    Melhor que o intervalo normal para amostras pequenas.
    """
    if total == 0:
        return (0.0, 1.0)

    p_hat = successes / total
    denominator = 1 + z**2 / total
    center = (p_hat + z**2 / (2 * total)) / denominator
    margin = (z / denominator) * math.sqrt(
        (p_hat * (1 - p_hat) / total) + (z**2 / (4 * total**2))
    )

    return (max(0, center - margin), min(1, center + margin))

# Exemplo com 10 jogos, 7 acertos:
# P = 70%, IC 95% = [39.7%, 89.2%] → MUITA incerteza, amostra pequena!
#
# Exemplo com 100 jogos, 70 acertos:
# P = 70%, IC 95% = [60.5%, 78.2%] → Mais confiavel
#
# Exemplo com 500 jogos, 350 acertos:
# P = 70%, IC 95% = [65.9%, 73.9%] → Alta confianca
```

**Regra:** Usar o LIMITE INFERIOR do intervalo de confianca como probabilidade conservadora para calcular EV. Assim protegemos contra amostras pequenas.

### 12.10 Atualizacao Bayesiana

A medida que novos dados chegam, atualizamos nossas estimativas em tempo real:

```python
def bayesian_update(prior_successes: int, prior_total: int,
                    new_successes: int, new_total: int) -> float:
    """
    Atualizacao Bayesiana com prior Beta.
    Comecar com prior fraco (alpha=2, beta=2) = crenca inicial neutra.
    """
    alpha = prior_successes + new_successes + 2  # +2 = prior
    beta = (prior_total - prior_successes) + (new_total - new_successes) + 2
    return alpha / (alpha + beta)
```

Isso significa que:
- Com poucos dados, o sistema e conservador (perto de 50%)
- A medida que acumula dados, a probabilidade converge para o valor real
- Nunca fica "travado" em estimativas antigas - se adapta

### 12.11 Deteccao de Regime (O Metodo Parou de Funcionar?)

```python
def detect_regime_change(recent_hits: int, recent_total: int,
                         historical_rate: float,
                         window: int = 50) -> dict:
    """
    Detecta se o metodo esta performando significativamente
    abaixo do historico (possivel mudanca de regime).
    """
    if recent_total < window:
        return {"status": "insufficient_data"}

    recent_rate = recent_hits / recent_total
    # Teste Z para proporcoes
    se = math.sqrt(historical_rate * (1 - historical_rate) / recent_total)
    z_score = (recent_rate - historical_rate) / se

    if z_score < -2.0:  # 95% confianca de queda
        return {
            "status": "DEGRADED",
            "message": f"Metodo caiu de {historical_rate:.1%} para {recent_rate:.1%}",
            "action": "PAUSAR alertas e investigar"
        }
    elif z_score < -1.5:
        return {
            "status": "WARNING",
            "message": f"Tendencia de queda: {recent_rate:.1%} vs historico {historical_rate:.1%}",
            "action": "Monitorar de perto"
        }
    else:
        return {
            "status": "HEALTHY",
            "message": f"Metodo performando dentro do esperado: {recent_rate:.1%}"
        }
```

**O sistema deve:**
- Rodar essa checagem a cada 50 jogos
- Se status = DEGRADED, pausar alertas automaticamente e notificar no Telegram
- Se status = WARNING, reduzir rating de todas as oportunidades em 1 estrela
- Isso protege contra perdas quando o mercado muda

### 12.12 Simulacao de ROI por Estrategia

O sistema deve rodar simulacoes constantes para responder:

| Pergunta | Como responder |
|----------|---------------|
| "Se eu apostasse em TODOS os alertas, qual seria meu ROI?" | Simular flat bet em todos os alertas historicos |
| "Se eu apostasse so nos 3+ estrelas?" | Filtrar e simular |
| "Se eu apostasse so em jogadores com >10 jogos?" | Filtrar e simular |
| "Qual a melhor combinacao de filtros?" | Grid search nos parametros |
| "Quanto estou arriscando de drawdown maximo?" | Calcular max drawdown historico |

```python
def simulate_roi(alerts: list, strategy_filter: callable,
                 stake_method: str = "flat") -> dict:
    """
    Simula ROI para uma estrategia especifica.
    """
    bankroll = 1000  # unidades
    initial = bankroll
    max_bankroll = bankroll
    max_drawdown = 0
    wins = 0
    losses = 0

    for alert in alerts:
        if not strategy_filter(alert):
            continue

        if stake_method == "flat":
            stake = 10  # 1% da banca inicial
        elif stake_method == "kelly":
            stake = bankroll * fractional_kelly(alert.true_prob, alert.odds)

        if alert.over25_hit:
            bankroll += stake * (alert.odds - 1)
            wins += 1
        else:
            bankroll -= stake
            losses += 1

        max_bankroll = max(max_bankroll, bankroll)
        drawdown = (max_bankroll - bankroll) / max_bankroll
        max_drawdown = max(max_drawdown, drawdown)

    total = wins + losses
    return {
        "total_bets": total,
        "wins": wins,
        "losses": losses,
        "win_rate": wins / total if total > 0 else 0,
        "roi": (bankroll - initial) / initial,
        "profit_units": bankroll - initial,
        "max_drawdown": max_drawdown,
        "bankroll_final": bankroll
    }
```

### 12.13 Formato do Alerta Atualizado (com Matematica)

```
🔔 ALERTA FIFA BET ⭐⭐⭐⭐ (4/5)

📊 Jogo 1: PlayerA 1 - 3 PlayerB
   Tipo: Derrota por 2 gols (padrao favoravel)

🎯 Jogo de Volta: 14:32 (em 2 min)

📉 Over 2.5 gols PlayerA: @1.85
   • Prob. implicita (casa): 54.1%
   • Prob. real (nossa):     68.3%
   • Edge: +14.2% ✅
   • EV: +25.8% por aposta ✅
   • Kelly 25%: 7.6% da banca

📊 Historico:
   • Global apos derrota: 64.3% (n=700)
   • PlayerA apos derrota: 75.0% (n=12)
   • Derrota por 2 gols: 68.0% (n=200)
   • IC 95%: [62.1% - 74.5%]

📈 Mercado: Odd caiu 2.10 → 1.85 (mercado favoravel)
⏰ Kickoff: 14:32 UTC-3
```

### 12.14 Fase de Coleta Obrigatoria (Cold Start)

O sistema **NAO pode enviar alertas** ate acumular dados suficientes.

#### Requisitos para Sair do Cold Start

| Requisito | Valor Minimo | Justificativa |
|-----------|-------------|---------------|
| Dias de coleta | >= 90 dias | Base temporal minima para capturar sazonalidade |
| Total de jogos de volta monitorados | >= 500 | Volume minimo para P_base confiavel |
| Total de jogadores unicos com >= 10 jogos | >= 20 | Para ter camada de jogador funcional |
| Checagem de regime inicial | HEALTHY | Confirmar que o padrao existe nos dados |

#### Fluxo do Cold Start
```
1. COLETA: Sistema roda 90+ dias em modo silencioso (sem alertas)
   - Captura TODOS os jogos, resultados, odds
   - Registra TODOS os detalhes possiveis de cada jogo
   - Ja calcula probabilidades mas NAO envia alertas

2. VALIDACAO: Apos 90 dias, roda analise completa
   - O padrao "perdedor faz over no jogo de volta" existe nos dados?
   - Qual a taxa real? E lucrativa a longo prazo?
   - Quais filtros maximizam ROI?
   - Relatorio completo enviado no Telegram

3. ATIVACAO: Se validado, liga os alertas com os thresholds otimizados
   - Thresholds calibrados com dados reais (nao estimativas)
   - Score de confianca treinado com dados reais

4. MODO SHADOW (opcional): Primeiras 2 semanas de alertas
   - Envia alertas mas marcados como "SHADOW - nao apostar"
   - Permite validar que o sistema esta funcionando corretamente
   - Apos 2 semanas sem problemas, ativar modo real
```

### 12.15 Dados Detalhados a Capturar (Cada Variavel Conta)

Cada detalhe pode ser uma variavel que melhora a probabilidade. Capturar tudo.

#### Dados do Jogo
| Campo | Exemplo | Porque importa |
|-------|---------|----------------|
| Placar final | 1-3 | Core do metodo |
| Gols por tempo (1T/2T) | 0-2, 1-1 | Jogador que toma gols no final pode estar tiltado |
| Time/equipe escolhida (home) | Real Madrid | Times com ataque forte podem favorecer over |
| Time/equipe escolhida (away) | Barcelona | Matchup entre times pode influenciar |
| Posse de bola (se disponivel) | 45%-55% | Jogador dominado pode mudar estrategia na volta |
| Chutes a gol (se disponivel) | 3-8 | Indica superioridade real vs sorte |
| Escanteios (se disponivel) | 2-6 | Indica pressao ofensiva |
| Cartoes (se disponivel) | 1-0 | Jogador que toma cartao pode estar mais agressivo |

#### Dados do Jogador
| Campo | Exemplo | Porque importa |
|-------|---------|----------------|
| Nome | "PlayerX" | Identificacao unica |
| Times que costuma usar | Real Madrid, PSG | Preferencia de time pode indicar estilo |
| Horarios que costuma jogar | 14:00-22:00 | Performance varia com horario |
| Media de gols geral | 2.3 | Jogador que ja faz muitos gols = mais provavel over |
| Media de gols apos derrota | 2.8 | Especifico do metodo |
| Streak atual | 3 derrotas seguidas | Tilt ou recuperacao? |
| Win rate geral | 48% | Jogador fraco perde mais = mais oportunidades |
| Adversarios frequentes | "PlayerY" (15x) | Matchup especifico pode ter padrao proprio |

#### Dados do Mercado/Odds
| Campo | Exemplo | Porque importa |
|-------|---------|----------------|
| Odd de abertura over 2.5 | @2.10 | Referencia de onde mercado comecou |
| Odd atual over 2.5 | @1.85 | Valor no momento do alerta |
| Odd de abertura over 3.5 | @3.50 | Mercado mais agressivo |
| Odd atual over 3.5 | @3.20 | Movimento |
| Handicap do jogo | -0.5 / +0.5 | Casa favorece quem? |
| Odd 1x2 | 1.90 / 3.50 / 2.10 | Quem a casa acha que ganha a volta? |
| Timestamp de cada captura | 14:28:32 | Para calcular velocidade de movimento |

#### Dados Contextuais
| Campo | Exemplo | Porque importa |
|-------|---------|----------------|
| Dia da semana | Quarta | Perfil de jogadores pode mudar no fim de semana |
| Faixa horaria | 14:00-18:00 | Diferentes pools de jogadores |
| Tempo entre jogo 1 e 2 | 58 min | Variacao no padrao de intervalo |
| Liga/torneio | Battle 8 min | Para multi-liga futuro |
| Quantidade de jogos simultaneos | 8 | Liga cheia vs vazia pode impactar |

### 12.16 Analise de Times/Equipes

Os times escolhidos pelos jogadores podem ser uma variavel significativa:

```python
# Hipoteses a testar com dados de 90 dias:

# 1. Time do perdedor no jogo 1 vs time no jogo de volta
#    - Jogador muda de time apos derrota? (sinal de tilt ou adaptacao)
#    - Certos times geram mais gols? (ataque forte = mais over)

# 2. Matchup de times
#    - Real Madrid vs Barcelona tem mais gols que Juventus vs Inter?
#    - Existem matchups que favorecem over?

# 3. Familiaridade com o time
#    - Jogador que SEMPRE usa Real Madrid vs jogador que troca toda hora
#    - Consistencia de time pode indicar maior dominio = mais gols

# Tabela adicional para rastrear:
```

```sql
-- Times/equipes usadas em cada jogo
CREATE TABLE match_teams (
    id INTEGER PRIMARY KEY,
    match_id INTEGER REFERENCES matches(id),
    player_name TEXT NOT NULL,
    team_name TEXT,                     -- ex: 'Real Madrid', 'PSG'
    side TEXT,                          -- 'home' ou 'away'
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Estatisticas por time
CREATE TABLE team_stats (
    id INTEGER PRIMARY KEY,
    team_name TEXT NOT NULL UNIQUE,
    total_games INTEGER DEFAULT 0,
    total_goals_scored INTEGER DEFAULT 0,
    total_goals_conceded INTEGER DEFAULT 0,
    avg_goals_scored REAL DEFAULT 0,
    avg_goals_conceded REAL DEFAULT 0,
    over25_rate REAL DEFAULT 0,         -- % de jogos com over 2.5 total
    last_updated TIMESTAMP
);

-- Estatisticas por matchup de times
CREATE TABLE matchup_stats (
    id INTEGER PRIMARY KEY,
    team_a TEXT NOT NULL,
    team_b TEXT NOT NULL,
    total_games INTEGER DEFAULT 0,
    avg_total_goals REAL DEFAULT 0,
    over25_rate REAL DEFAULT 0,
    last_updated TIMESTAMP,
    UNIQUE(team_a, team_b)
);

-- Jogador + time preferido
CREATE TABLE player_team_preferences (
    id INTEGER PRIMARY KEY,
    player_name TEXT NOT NULL,
    team_name TEXT NOT NULL,
    times_used INTEGER DEFAULT 0,
    goals_scored_with INTEGER DEFAULT 0,
    avg_goals_with REAL DEFAULT 0,
    is_main_team BOOLEAN DEFAULT FALSE, -- usa >50% das vezes
    UNIQUE(player_name, team_name)
);
```

### 12.17 Tabela de Decisao Final

```
EDGE >= 5%  AND  EV >= 3%  AND  P_real >= 55%  → ALERTAR
EDGE >= 12% AND  EV >= 10% AND  P_real >= 60%  → ALERTAR COM DESTAQUE
EDGE < 5%   OR   EV < 3%   OR   P_real < 55%   → NAO ALERTAR (silencioso)
EDGE < 0%                                       → REGISTRAR COMO "SEM VALOR"
```

O sistema **nunca** manda um alerta que nao tem edge positivo. Isso e o que separa um notificador de um sistema lucrativo.

---

## 13. Features Adicionais Sugeridas

### 13.1 Detector de Padroes de Derrota
Nem toda derrota e igual. O sistema pode classificar:
- **Derrota apertada** (1-2, 2-3): jogador competitivo, pode reagir forte
- **Goleada** (0-4, 1-5): jogador pode estar tiltado OU pode reagir agressivamente
- **Derrota com muitos gols** (3-4, 2-5): jogo aberto, indica tendencia a jogos com muitos gols
- Correlacionar cada tipo com taxa de over no jogo de volta

### 13.2 Detector de "Momentum Shift"
Monitorar se o mercado esta se movendo a favor:
- Odd do over caindo = mercado concorda que tera gols
- Volume de apostas subindo (se disponivel)
- Linha de handicap mudando

### 13.3 Filtro Anti-Tilt
Alguns jogadores depois de uma goleada jogam pessimamente no jogo de volta tambem. O sistema pode detectar jogadores que "tiltam" e excluir dos alertas.

### 13.4 Horarios de Ouro
Analisar em quais horarios do dia o metodo funciona melhor. Jogos de madrugada vs dia vs noite podem ter perfis diferentes de jogadores.

### 13.5 Correlacao de Odds de Abertura
Se a odd de abertura do over 2.5 ja esta baixa (ex: @1.50), o mercado ja precifica muitos gols. Pode ser bom sinal. Se esta alta (@2.50+), ha mais valor mas mais risco.

### 13.6 Alerta de "Odd Caindo Rapido"
Se a odd do over 2.5 cai bruscamente em poucos minutos, pode indicar informacao privilegiada ou consenso do mercado. Alerta especial para essas situacoes.

### 13.7 Backtesting com Dados Historicos
Antes de operar ao vivo, rodar o metodo contra dados historicos do BetsAPI para validar estatisticamente:
- Taxa de acerto por tipo de derrota
- Taxa de acerto por jogador
- Taxa de acerto por faixa de odd
- ROI por estrategia

---

## 14. Estrutura do Projeto

```
fifa-bet-alert/
├── README.md
├── PRD.md
├── .env.example
├── .gitignore
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── pyproject.toml
│
├── src/
│   ├── __init__.py
│   ├── main.py                  # Entry point
│   ├── config.py                # Pydantic settings
│   │
│   ├── api/                     # Integracao com APIs externas
│   │   ├── __init__.py
│   │   ├── betsapi_client.py    # Client BetsAPI
│   │   ├── models.py            # Modelos de resposta da API
│   │   └── exceptions.py
│   │
│   ├── core/                    # Logica de negocio
│   │   ├── __init__.py
│   │   ├── game_watcher.py      # Monitoramento de jogos
│   │   ├── pair_matcher.py      # Matching ida/volta
│   │   ├── odds_monitor.py      # Monitoramento de odds
│   │   ├── alert_engine.py      # Motor de alertas
│   │   ├── stats_engine.py      # Motor estatistico (probabilidades, EV, edge)
│   │   ├── probability.py       # Funcoes matematicas puras
│   │   ├── alert_classifier.py  # Classificacao por estrelas/niveis
│   │   ├── validator.py         # Validacao pos-jogo
│   │   └── reporter.py          # Relatorios automaticos
│   │
│   ├── telegram/                # Bot Telegram
│   │   ├── __init__.py
│   │   ├── bot.py               # Setup do bot
│   │   ├── messages.py          # Templates de mensagens
│   │   └── commands.py          # Comandos do bot (/status, /stats, etc)
│   │
│   ├── db/                      # Persistencia
│   │   ├── __init__.py
│   │   ├── database.py          # Conexao e setup
│   │   ├── models.py            # SQLAlchemy models
│   │   └── repositories.py     # Queries e operacoes
│   │
│   └── utils/                   # Utilitarios
│       ├── __init__.py
│       ├── logger.py
│       └── scheduler.py
│
├── tests/
│   ├── __init__.py
│   ├── test_pair_matcher.py
│   ├── test_odds_monitor.py
│   ├── test_alert_engine.py
│   └── test_confidence.py
│
├── scripts/
│   ├── collect_history.py       # Coleta 90+ dias de dados historicos
│   ├── backtest.py              # Analise completa + calibracao de pesos
│   ├── validate_api.py          # Validar acesso a API
│   └── setup_telegram.py        # Helper para setup do bot
│
└── data/
    └── .gitkeep                 # Pasta para DB local
```

---

## 15. Definicao de Pronto (DoD)

O MVP esta pronto quando:
1. Sistema roda 24h sem crash
2. Detecta corretamente pares de jogos ida/volta
3. Monitora odds do over 2.5 gols do jogador perdedor
4. Envia alerta no Telegram >= 1 min antes do jogo de volta
5. Alerta contem todas as infos definidas na secao F3
6. Dados sao persistidos para validacao futura
7. Logs completos de todas as operacoes
8. Deploy via Docker funcional
