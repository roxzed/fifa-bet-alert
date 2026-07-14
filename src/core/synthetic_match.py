"""return_match sintetico pro watch preditivo (quando a API nao expoe a volta).

Nao eh persistido no DB — existe so pra alimentar predict_watch_candidate /
send_watch* com os campos que eles consomem. A volta troca o mando, entao os
jogadores/times sao invertidos em relacao ao G1.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class SyntheticReturnMatch:
    id: None
    game1_id: int
    player_home: str | None
    player_away: str | None
    team_home: str | None
    team_away: str | None
    started_at: datetime
    is_return_match: bool = True
    api_event_id: None = None
    score_home: None = None
    score_away: None = None


def build_synthetic_return(game1_match, started_at: datetime) -> SyntheticReturnMatch:
    return SyntheticReturnMatch(
        id=None,
        game1_id=game1_match.id,
        player_home=game1_match.player_away,   # invertido: volta troca mando
        player_away=game1_match.player_home,
        team_home=game1_match.team_away,
        team_away=game1_match.team_home,
        started_at=started_at,
    )
