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
    entry_odd: float | None, loser_goals: int, line: str, min_odd: float, require_odd: bool = True
) -> tuple[str, bool | None]:
    """green/red pelo placar. Se require_odd e entry_odd is None -> void.

    require_odd=False (modo sem odd): ignora a odd, sempre green/red pelo placar.
    """
    if require_odd and entry_odd is None:
        return ("void", None)
    hit = loser_goals > LINE_THRESH[line]
    return ("green", True) if hit else ("red", False)
