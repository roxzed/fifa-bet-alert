"""Stats Engine V2: avalia oportunidades usando H2H puro (sem edge/EV).

Cascata de camadas (apos auditoria 25/04/2026 com Plinio):
  C1a: DESATIVADA — 3 alertas reais 0% WR (-3u). Sample insuficiente E direcao ruim.
  C1b: Cross-confirmation — 4elem >=3 (>=65%) AND 3elem (loser, opp, loser_team) >=5 (>=69%)
  C2:  H2H geral (loser, opp) min 15 games no historico, last 10, prob_shrunk >= 0.70
       Recalibrado 2026-05-23 (v2) com shrinkage bayesiano (mu=0.57, K=10)
       e janela 10 jogos (era 20). Backtest point-in-time real mostrou que
       janela 10 captura forma corrente — janela 20 diluia sinal recente.
       v2 deu +16.38u em maio (vs -0.61u da v1 com janela 20).

Linhas elegiveis: over25, over15 (era over45/35/25/15).
over35: 9 alertas reais -1.12u ROI -12%. Removido.
over45: nunca disparou.

Odds minima: 1.65.
Bloqueios por (player, line) hardcoded — drainers detectados em auditoria.
Respeita blacklist do M1 (jogadores com O2.5 consistentemente ruim).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from loguru import logger


# Linhas restritas a over25/over15 (apos auditoria 25/04: over35 era drenador,
# over45 nunca disparou). Linha mais alta tentada primeiro.
LINES_ORDER = [
    ("over25", 2),
    ("over15", 1),
]

MIN_ODDS = 1.65

MIN_GAMES_C2 = 15  # era 10. Auditoria 25/04: 6-14 jogos = -11.71u em 61 alertas.

# Recalibracao bayesiana C2.
# v1 (2026-05-22): window=20, K=20, thr=0.75 — implementado mas backtest
#   point-in-time depois revelou que estava avaliando incorretamente.
# v2 (2026-05-23): window=10, K=10, thr=0.70 — apos backtest point-in-time
#   real reconstruido a partir do historico de matches:
#
#   Janela 10 jogos captura forma corrente do jogador melhor que 20.
#   Janela 20 dilui sinal recente com jogos velhos onde o jogador estava
#   em forma diferente. Bucket prob 90-95% com janela 10 deu WR 62.3%
#   (calibracao boa). Mesma faixa com janela 20 deu WR 62.5% mas com
#   metade do volume.
#
#   Backtest M2 maio (point-in-time real, so C2 com nova config):
#     ATUAL (sem recalib):  398 tips, ROI -0.6%
#     v1 (win=20,K=20,t=0.75): 121 tips, -0.61u, ROI -0.5%
#     v2 (win=10,K=10,t=0.70): 293 tips, +16.38u, ROI +5.6%  <-- escolhida
#
#   Por periodo com v2:
#     A (01-06): 78 tips, +20.03u, ROI +25.7%
#     B (07-15): 120 tips, +3.87u, ROI +3.2%
#     C (16-22): 93 tips, -8.88u, ROI -9.5% (reduzido vs -17.70u atual)
C2_WINDOW = 10            # janela recente para calcular hit_rate
C2_SHRINK_MU = 0.57       # WR implicita media do mercado
C2_SHRINK_K = 10          # peso do prior (em "jogos equivalentes")
C2_THRESHOLD_SHRUNK = 0.70  # threshold de prob_shrunk para disparar alerta

# Bloqueio por (player, line) M2 — auditoria 25/04 identificou drains agudos.
# Stormi over15: -5.94u em 10 (20% WR). Jekunam over15: -4.22u em 11 (36%).
# nekishka over15: -4.00u em 4 (0%). nikkitta over25: -2.29u em 4 (25%).
# Simaponika over15: -2.26u em 61 (56%) — n grande sample, sinal real.
# A1ose: 3 alertas total 0% WR -3u.
# 2026-05-02: esvaziado por pedido do owner. Tudo via SHADOW V2.
BLOCKED_LINES_V2: dict[str, set[str]] = {}


@dataclass
class EvaluationV2:
    should_alert: bool
    camada: str = ""        # "C1a", "C1b", "C2"
    best_line: str = ""     # "over45", "over35", "over25", "over15"
    prob: float = 0.0       # probabilidade principal
    odds: float = 0.0       # odds da best_line
    sample_size: int = 0    # jogos na amostra usada
    reason: str = ""
    # C1b extra
    prob_4elem: float = 0.0
    prob_3elem: float = 0.0
    sample_4elem: int = 0
    sample_3elem: int = 0


class StatsEngineV2:
    """Motor estatistico do Metodo 2 — H2H puro sem edge/EV gates."""

    def __init__(self, match_repo, blacklist: set[str] | None = None) -> None:
        self.matches = match_repo
        self.blacklist = blacklist or set()

    async def evaluate_opportunity(
        self,
        loser: str,
        opponent: str,
        loser_team: str,
        opp_team: str,
        odds_dict: dict[str, float | None],
    ) -> EvaluationV2:
        """Avalia se deve alertar para este par G1→G2.

        Args:
            loser: jogador que perdeu G1
            opponent: adversario em G1
            loser_team: time do perdedor em G2
            opp_team: time do adversario em G2
            odds_dict: {"over15": 1.65, "over25": 1.80, ...} — odds atuais

        Returns:
            EvaluationV2 com should_alert e detalhes da camada que disparou.
        """
        if loser in self.blacklist:
            return EvaluationV2(
                should_alert=False,
                reason=f"Jogador {loser} na blacklist"
            )

        h2h_data = await self.matches.get_h2h_loser_goals(loser, opponent)

        if not h2h_data:
            return EvaluationV2(should_alert=False, reason="Sem historico H2H")

        # Filtrar odds_dict pelas linhas bloqueadas pra esse player (auditoria 25/04)
        blocked_lines = BLOCKED_LINES_V2.get(loser, set())
        if blocked_lines:
            odds_dict = {k: v for k, v in odds_dict.items() if k not in blocked_lines}

        # C1a DESATIVADA (auditoria 25/04: 3 alertas 0% WR, sample insuficiente)
        # C1b: cross-confirmation 4elem + 3elem
        result = self._try_c1b(h2h_data, loser_team, opp_team, odds_dict)
        if result:
            return result

        # C2: H2H geral
        result = self._try_c2(h2h_data, odds_dict)
        if result:
            return result

        return EvaluationV2(should_alert=False, reason="Nenhuma camada atingida")

    def _try_c1a(
        self,
        rows: Sequence,
        loser_team: str,
        opp_team: str,
        odds_dict: dict[str, float | None],
    ) -> EvaluationV2 | None:
        """C1a: 4-element H2H (loser, opp, loser_team, opp_team).
        Min 6 jogos, last 20, prob >= 69%.
        """
        filtered = [r for r in rows if r[1] == loser_team and r[2] == opp_team]
        if len(filtered) < 6:
            return None

        goals = [r[0] for r in filtered]
        recent = goals[-20:]

        for line, threshold in LINES_ORDER:
            odds_val = odds_dict.get(line)
            if not odds_val or odds_val < MIN_ODDS:
                continue
            prob = sum(1 for g in recent if g > threshold) / len(recent)
            if prob >= 0.69:
                logger.info(
                    f"M2 C1a: {line} prob={prob:.0%} (n={len(recent)}/{len(filtered)}) "
                    f"odds={odds_val:.2f}"
                )
                return EvaluationV2(
                    should_alert=True,
                    camada="C1a",
                    best_line=line,
                    prob=prob,
                    odds=odds_val,
                    sample_size=len(recent),
                    reason=f"C1a: 4elem {len(filtered)} jogos, {line} {prob:.0%}",
                )
        return None

    def _try_c1b(
        self,
        rows: Sequence,
        loser_team: str,
        opp_team: str,
        odds_dict: dict[str, float | None],
    ) -> EvaluationV2 | None:
        """C1b: Cross-confirmation.
        4elem (loser, opp, loser_team, opp_team) >= 3 games, prob >= 65%
        AND 3elem (loser, opp, loser_team) >= 5 games, prob >= 69%
        Ambos devem concordar na MESMA linha.
        """
        filtered_4 = [r for r in rows if r[1] == loser_team and r[2] == opp_team]
        filtered_3 = [r for r in rows if r[1] == loser_team]

        if len(filtered_4) < 3 or len(filtered_3) < 5:
            return None

        goals_4 = [r[0] for r in filtered_4]
        goals_3 = [r[0] for r in filtered_3]
        recent_4 = goals_4[-20:]
        recent_3 = goals_3[-20:]

        for line, threshold in LINES_ORDER:
            odds_val = odds_dict.get(line)
            if not odds_val or odds_val < MIN_ODDS:
                continue
            p4 = sum(1 for g in recent_4 if g > threshold) / len(recent_4)
            p3 = sum(1 for g in recent_3 if g > threshold) / len(recent_3)
            if p4 >= 0.65 and p3 >= 0.69:
                logger.info(
                    f"M2 C1b: {line} p4={p4:.0%} (n={len(recent_4)}) "
                    f"p3={p3:.0%} (n={len(recent_3)}) odds={odds_val:.2f}"
                )
                return EvaluationV2(
                    should_alert=True,
                    camada="C1b",
                    best_line=line,
                    prob=p3,
                    odds=odds_val,
                    sample_size=len(recent_3),
                    reason=f"C1b: 4e={len(filtered_4)}({p4:.0%}) 3e={len(filtered_3)}({p3:.0%}) {line}",
                    prob_4elem=p4,
                    prob_3elem=p3,
                    sample_4elem=len(recent_4),
                    sample_3elem=len(recent_3),
                )
        return None

    def _try_c2(
        self,
        rows: Sequence,
        odds_dict: dict[str, float | None],
    ) -> EvaluationV2 | None:
        """C2: H2H geral (loser, opp). Min 15 jogos no historico, last 10.

        Recalibrado 2026-05-23 (v2): janela reduzida de 20 -> 10 jogos para
        capturar forma corrente do jogador. Janela 20 diluia sinal recente.
        Shrinkage bayesiano mu=0.57 K=10, threshold prob_shrunk >= 0.70.
        """
        if len(rows) < MIN_GAMES_C2:
            return None

        goals = [r[0] for r in rows]
        recent = goals[-C2_WINDOW:]
        n_recent = len(recent)

        for line, threshold in LINES_ORDER:
            odds_val = odds_dict.get(line)
            if not odds_val or odds_val < MIN_ODDS:
                continue
            hit_rate = sum(1 for g in recent if g > threshold) / n_recent
            prob_shrunk = (
                (C2_SHRINK_K * C2_SHRINK_MU + n_recent * hit_rate)
                / (C2_SHRINK_K + n_recent)
            )
            if prob_shrunk >= C2_THRESHOLD_SHRUNK:
                logger.info(
                    f"M2 C2: {line} hit_rate={hit_rate:.0%} "
                    f"prob_shrunk={prob_shrunk:.0%} (n={n_recent}/{len(rows)}) "
                    f"odds={odds_val:.2f}"
                )
                return EvaluationV2(
                    should_alert=True,
                    camada="C2",
                    best_line=line,
                    prob=prob_shrunk,
                    odds=odds_val,
                    sample_size=n_recent,
                    reason=(
                        f"C2: H2H {len(rows)} jogos, {line} "
                        f"hit={hit_rate:.0%}(n={n_recent}) shrunk={prob_shrunk:.0%}"
                    ),
                )
        return None
