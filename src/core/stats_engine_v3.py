"""Method 3 (M3) — critério de frequência H2H pura.

Regra (spec 2026-07-13): pro perdedor do G1, uma linha de over de gols
individuais qualifica quando, nos últimos 20 H2H contra o adversário
(QUALQUER jogo, ida ou volta):
  1. taxa da linha >= 60%; E
  2. >= 5 hits nos últimos 7 H2H.
Amostra mínima: 10 H2H. Sem cálculo de edge/EV (decisão consciente do
owner — método em validação no privado dele).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from loguru import logger

from src.config import settings

M3_LINES: list[tuple[str, float]] = [
    ("over15", 1.5),
    ("over25", 2.5),
    ("over35", 3.5),
    ("over45", 4.5),
]
M3_LINE_LABELS = {
    "over15": "Over 1.5",
    "over25": "Over 2.5",
    "over35": "Over 3.5",
    "over45": "Over 4.5",
}


@dataclass
class LineEvalV3:
    line: str
    threshold: float
    hits: int
    n: int
    rate: float
    recent_hits: int
    recent_n: int
    qualified: bool


@dataclass
class EvaluationV3:
    should_alert: bool
    lines: list[LineEvalV3] = field(default_factory=list)
    n_h2h: int = 0
    reason: str = ""


def evaluate_h2h_lines(
    goals_recent_first: list[int],
    *,
    min_h2h: int,
    min_prob: float,
    recent_window: int,
    recent_min_hits: int,
) -> EvaluationV3:
    """Função PURA: avalia todas as linhas M3 sobre a lista de gols.

    `goals_recent_first`: gols do jogador-alvo por jogo H2H, mais recente
    primeiro (máximo M3_H2H_WINDOW itens, garantido pelo caller).
    """
    n = len(goals_recent_first)
    if n < min_h2h:
        return EvaluationV3(
            should_alert=False,
            n_h2h=n,
            reason=f"amostra insuficiente: {n} H2H < {min_h2h}",
        )

    recent = goals_recent_first[:recent_window]
    qualified: list[LineEvalV3] = []
    detalhes: list[str] = []  # diagnóstico por linha ("por que não alertou")
    for line, threshold in M3_LINES:
        hits = sum(1 for g in goals_recent_first if g > threshold)
        rate = hits / n
        recent_hits = sum(1 for g in recent if g > threshold)
        detalhes.append(f"{line} {rate:.0%} {hits}/{n} rec{recent_hits}/{len(recent)}")
        ok = rate >= min_prob and recent_hits >= recent_min_hits
        if ok:
            qualified.append(
                LineEvalV3(
                    line=line,
                    threshold=threshold,
                    hits=hits,
                    n=n,
                    rate=rate,
                    recent_hits=recent_hits,
                    recent_n=len(recent),
                    qualified=True,
                )
            )

    if not qualified:
        return EvaluationV3(
            should_alert=False,
            n_h2h=n,
            reason=f"nenhuma linha passou (n={n}): " + "; ".join(detalhes),
        )
    return EvaluationV3(should_alert=True, lines=qualified, n_h2h=n)


class StatsEngineV3:
    """Orquestra a avaliação M3 com dados do DB (I/O fino, lógica na pura)."""

    def __init__(self, match_repo) -> None:
        self.matches = match_repo

    async def evaluate(self, loser: str, opponent: str) -> EvaluationV3:
        goals = await self.matches.get_h2h_player_goals(
            loser, opponent, limit=settings.m3_h2h_window
        )
        ev = evaluate_h2h_lines(
            goals,
            min_h2h=settings.m3_min_h2h,
            min_prob=settings.m3_min_prob,
            recent_window=settings.m3_recent_window,
            recent_min_hits=settings.m3_recent_min_hits,
        )
        if not ev.should_alert:
            # Diagnóstico "por que não alertou" (lição do bug do watch)
            logger.info(f"M3 {loser} vs {opponent}: sem alerta — {ev.reason}")
        return ev
