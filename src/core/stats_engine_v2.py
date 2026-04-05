"""Stats Engine V2: avalia oportunidades usando H2H puro (sem edge/EV).

Cascata de camadas:
  C1a: 4-element (loser, opp, loser_team, opp_team) >=6 games, last 20, prob >=69%
  C1b: Cross-confirmation — 4elem >=3 (>=65%) AND 3elem (loser, opp, loser_team) >=5 (>=69%)
  C2:  H2H geral (loser, opp) min 10 games, last 20, prob >=85%

Linha prioritaria: O4.5 → O3.5 → O2.5 → O1.5 (mais agressiva primeiro).
Odds minima: 1.60.
Respeita blacklist do M1 (jogadores com O2.5 consistentemente ruim).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from loguru import logger


LINES_ORDER = [
    ("over45", 4),
    ("over35", 3),
    ("over25", 2),
    ("over15", 1),
]

MIN_ODDS = 1.65


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

        # C1a: 4-element exact match
        result = self._try_c1a(h2h_data, loser_team, opp_team, odds_dict)
        if result:
            return result

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
        """C2: H2H geral (loser, opp). Min 10 jogos, last 20, prob >= 85%."""
        if len(rows) < 10:
            return None

        goals = [r[0] for r in rows]
        recent = goals[-20:]

        for line, threshold in LINES_ORDER:
            odds_val = odds_dict.get(line)
            if not odds_val or odds_val < MIN_ODDS:
                continue
            prob = sum(1 for g in recent if g > threshold) / len(recent)
            if prob >= 0.85:
                logger.info(
                    f"M2 C2: {line} prob={prob:.0%} (n={len(recent)}/{len(rows)}) "
                    f"odds={odds_val:.2f}"
                )
                return EvaluationV2(
                    should_alert=True,
                    camada="C2",
                    best_line=line,
                    prob=prob,
                    odds=odds_val,
                    sample_size=len(recent),
                    reason=f"C2: H2H {len(rows)} jogos, {line} {prob:.0%}",
                )
        return None
