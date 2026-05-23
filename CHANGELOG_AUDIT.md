# CHANGELOG — Auditoria de mudanças do sistema (Maio 2026)

Este documento registra todas as mudanças significativas feitas no sistema entre 18-23/05/2026 para permitir rollback rápido se algo der errado.

---

## 📍 Pontos de rollback definidos

| Rollback alvo | Commit | Data | Estado |
|---|---|---|---|
| **Antes de TUDO** (estado 18/05 manhã) | `985f004` | 11/05/2026 | Sistema pré-auditoria. SHADOW v3, GIRANDO ativo. |
| **Estado final 18/05** (só dashboard fix) | `e577114` | 18/05/2026 | Dashboard corrigido. Sistema operacional sem mudanças no engine. |
| **Estado final 22/05** (M2 recalibrado v1) | `e2f287f` | 22/05/2026 | M2 recalibração v1 aplicada (janela 20). M1 ainda com GIRANDO. |
| **Estado final 23/05** (com mudanças M1) | `f0646f7` | 23/05/2026 | Estado atual completo. |

---

## Histórico cronológico das mudanças

### 18/05/2026 — Dashboard e Cancelamento

#### `1433a15` — fix(dashboard): excluir alertas suprimidos e corrigir stats_all
- **Arquivo:** `web/app.py`
- **Mudança:** Query do dashboard agora filtra `suppressed IS NOT TRUE`. Stats_all separado de stats.
- **Motivo:** Dashboard mostrava 1.488 tips quando real eram ~243 (incluía suprimidos).
- **Impacto:** Apenas visualização, não afeta envio de alertas.

#### `6a60fa4` — feat(m2): mostrar tier H2H nas mensagens M2 igual ao M1
- **Arquivo:** `src/core/alert_engine_v2.py`, `src/telegram/messages.py`
- **Mudança:** Tier H2H sempre computado e prefixo [S/A/B/C/D] aparece nas mensagens M2.
- **Impacto:** Cosmético + alimenta GIRANDO check que já existia.

#### `20f463c` — feat(shadow): D threshold 5->2%, remover PERMANENT, fix dedup por match_id
- **Arquivos:** `src/core/h2h_tier.py`, `src/core/blocked_lines.py`, `src/core/blocked_lines_v2.py`
- **Mudança 1:** C threshold de tier H2H baixou de 5.0 → 2.0 (D agora é 0-2%).
- **Mudança 2:** Removido estado PERMANENT do SHADOW (M1 e M2).
- **Mudança 3:** Dedup por (match_id, best_line) em `_fetch_all_alerts` — corrige bug de polls duplicados.
- **DB:** PERMANENT cells existentes foram desbloqueados (mko1919/over25/V1nn).
- **Impacto:** SHADOW pode bloquear/desbloquear N vezes. Bug crítico de polls corrigido.

#### `e577114` — chore: cancela alerta 2228 (Sena over25 vs Bosko)
- **Arquivo:** `src/core/cancelled_alerts.py`, `web/app.py`
- **Mudança:** ID 2228 adicionado ao set de cancelados.
- **Impacto:** Apenas exibição, não conta no PL.

---

### 22/05/2026 — M2 Recalibração

#### `3e92c50` — fix(m2): /results agora filtra alertas suprimidos no M2 tambem
- **Arquivo:** `src/telegram/commands.py`
- **Mudança:** Comando `/results` no grupo M2 filtra alertas suppressed=true.
- **Motivo:** /results mostrava alertas que nunca foram enviados ao grupo.
- **Impacto:** Apenas comando /results.

#### `6fb6102` — feat(m2): filtros calibrados pos-drawdown maio 2026 [⚠️ REVERTIDO]
- **Arquivo:** `src/core/alert_engine_v2.py`
- **Mudança:** Adicionou filtro de 03h BRT e blocklist tohi4.
- **STATUS:** REVERTIDO em `47e1e61` no mesmo dia.

#### `47e1e61` — revert(m2): remover filtros 03h e hard-blocked combos do tohi4
- **Arquivo:** `src/core/alert_engine_v2.py`
- **Mudança:** Reverteu o commit anterior. Volta a deixar SHADOW automatizado lidar.
- **Impacto:** Estado igual antes do `6fb6102`.

#### `e2f287f` — feat(m2): recalibracao bayesiana C2 — shrinkage para mu=0.57 K=20
- **Arquivo:** `src/core/stats_engine_v2.py`
- **Mudança:** _try_c2 aplica shrinkage bayesiano para mu=0.57 com K=20, threshold prob_shrunk >= 0.75.
- **Janela:** mantida em 20 jogos.
- **Motivo:** Calibração quebrada — bucket prob ≥85% tinha WR real ~55%.
- **Impacto:** M2 volume cai, ROI esperado sobe.

---

### 23/05/2026 — M2 v2 e M1 Refactor

#### `9103e74` — feat(m2): recalibracao v2 — janela 10 (era 20) + K=10 thr=0.70
- **Arquivo:** `src/core/stats_engine_v2.py`
- **Mudança:** Janela reduzida 20 → 10 jogos. K=20 → 10. Threshold 0.75 → 0.70.
- **Motivo:** Backtest point-in-time real mostrou janela 10 captura melhor forma corrente.
- **Impacto:** M2 mais responsivo a tendências recentes.

#### `5ca3d17` — feat(m1): remover filtro GIRANDO (tier D suprimir) — backtest mostrou estar cortando lucro
- **Arquivo:** `src/core/alert_engine.py`
- **Mudança:** Removida supressão de alertas tier D no M1.
- **Motivo:** Backtest abril+maio: 45 jogos suprimidos com WR 78%, ROI hipotético +50% (Z=2.82, p=0.0024).
- **Impacto:** M1 envia alertas tier D agora. Volume sobe.

#### `a0ea961` — feat(m1): suprimir alertas tier '?' — manter so D-S no Telegram
- **Arquivo:** `src/core/alert_engine.py`
- **Mudança:** Filtro tier '?' (n<3 OU n>=3 com ROI<0) suprimido no Telegram.
- **Motivo:** Backtest hipotético sugeria que tier '?' drenava — depois descobriu-se que cálculo estava enganoso.

#### `94b4e44` — fix(m1): tier filter — suprimir apenas combos sem amostra (n<3) [⚠️ REVERTIDO]
- **Arquivo:** `src/core/alert_engine.py`
- **Mudança:** Refinou para suprimir só ?_n<3 (n<3), mantendo ?_neg (n>=3 com ROI<0).
- **Motivo:** Análise mostrou ?_neg deu ROI +40% em maio.
- **STATUS:** REVERTIDO em `f0646f7`.

#### `f0646f7` — feat(m1): suprimir TODOS alertas tier '?' (cobre n<3 e ?_neg) [ATUAL]
- **Arquivo:** `src/core/alert_engine.py`
- **Mudança:** Volta a suprimir TODOS os tier '?'. Owner: "garantia de só enviar tier classificado".
- **Garantia:** Alert ainda é criado no DB, validado normalmente, profit_flat preenchido. Histórico H2H acumula. Quando combo vira D-S, próximos chegam.
- **Estado atual do sistema.**

---

## 🔄 COMANDOS DE ROLLBACK

### Rollback completo até o estado de 22/05 (manter só M2 recalibração v1)
```bash
git reset --hard e2f287f
git push origin master --force
```
**Estado:** Dashboard fixes + SHADOW v3 + M2 recalib v1 (janela 20). M1 com GIRANDO ainda.

### Rollback total — voltar antes de TUDO (estado 18/05 antes do trabalho)
```bash
git reset --hard e577114
git push origin master --force
```
**Estado:** Apenas dashboard fixes. Nenhuma mudança no engine.

### Rollback antes da semana inteira (estado pré-tudo)
```bash
git reset --hard 985f004
git push origin master --force
```
**Estado:** Sistema como estava em 11/05. SHADOW v3 ativo, GIRANDO ativo.

### Reverter mudança específica sem afetar outras
```bash
# Exemplo: reverter só a remoção do GIRANDO (commit 5ca3d17)
git revert 5ca3d17
git push origin master

# Exemplo: reverter só o filtro tier '?' do M1
git revert f0646f7
git push origin master
```

### Voltar mko1919 over25 vs V1nn para PERMANENT (banco)
Esse foi alterado no banco no dia 18/05. Se rollback for necessário:
```sql
UPDATE blocked_lines
SET state = 'PERMANENT', block_count = 2
WHERE player = 'mko1919' AND line = 'over25' AND opponent = 'V1nn';
```

---

## ⚠️ Cuidados ao fazer rollback

1. **--force push** sobrescreve o histórico remoto. Avise outros desenvolvedores.
2. **Railway redeploy** acontece automaticamente após push (3-5 min).
3. **Banco de dados** não é versionado — algumas mudanças (PERMANENT removido) precisam SQL manual.
4. **Após rollback**, observe alertas chegando no Telegram para confirmar o estado.

---

## Como saber se está funcionando

Após qualquer rollback, em até 10 minutos deve aparecer no Telegram um alerta novo. Verifique:
- M1: alerta com prefixo `[S]`, `[A]`, etc.
- M2: alerta com formato `📊 M2 | C2 — [tier] Over X.X...`
- Se nada chegar em 30 min, verificar Railway deployment status.

---

**Última atualização:** 2026-05-23
**Mantido por:** Plini + Claude (assistente)
