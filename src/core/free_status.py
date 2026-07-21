"""Regra de resultado do Modelo FREE (pura, sem I/O)."""

from __future__ import annotations

LINE_LABELS = {
    "over15": "Over 1.5",
    "over25": "Over 2.5",
    "over35": "Over 3.5",
    "over45": "Over 4.5",
}
LINE_THRESH = {"over15": 1.5, "over25": 2.5, "over35": 3.5, "over45": 4.5}


def decide_status(
    entry_odd: float | None, loser_goals: int, line: str, min_odd: float
) -> tuple[str, bool | None]:
    """VOID se a odd nunca atingiu min_odd (entry_odd None). Senao GREEN/RED
    conforme os gols do jogador batem a linha."""
    if entry_odd is None:
        return ("void", None)
    hit = loser_goals > LINE_THRESH[line]
    return ("green", True) if hit else ("red", False)
