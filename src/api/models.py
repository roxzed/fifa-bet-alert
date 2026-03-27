"""Pydantic models for BetsAPI responses.

These models normalize the raw BetsAPI JSON into a consistent internal
representation used throughout the application.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class APIOddsOutcome(BaseModel):
    """A single outcome within an odds market.

    Example: Over 2.5 at odds 1.85 with header '2.5'.
    """

    name: str  # e.g. 'Over 2.5', 'Under 2.5', 'Home', 'Draw', 'Away'
    odds: float
    header: str | None = None  # the line value like '2.5', '3.5' for o/u


class APIOdds(BaseModel):
    """Odds for a specific market on an event.

    Markets include over/under, 1x2 (match result), handicap, etc.
    """

    event_id: str
    market: str  # 'over_under', '1x2', 'handicap', 'both_to_score', etc.
    outcomes: list[APIOddsOutcome] = Field(default_factory=list)
    updated_at: datetime


class APIEvent(BaseModel):
    """A single match/event from BetsAPI.

    In eSoccer context:
    - home_name / away_name are the PLAYER names (gamertags)
    - home_team / away_team are the FIFA teams chosen (e.g. Real Madrid)
    """

    id: str
    league_id: str
    league_name: str = ""
    home_name: str  # player / gamertag
    away_name: str  # player / gamertag
    home_score: int | None = None
    away_score: int | None = None
    status: str = "upcoming"  # 'upcoming', 'inplay', 'ended'
    scheduled_time: datetime

    # Extra fields (may not always be present)
    home_team: str | None = None  # FIFA team chosen by home player
    away_team: str | None = None  # FIFA team chosen by away player
    stats: dict | None = None  # corners, shots, possession, cards, etc.


class APILeague(BaseModel):
    """A league / competition from BetsAPI."""

    id: str
    name: str
    sport_id: int = 1  # 1 = Soccer (includes eSoccer)


class PlayerGoalsOdds(BaseModel):
    """Odds for a player-specific goals market from bet365.

    Example: Chelsea (Kray) Goals - Over 3.5 @2.50
    """

    player_name: str       # "Kray"
    team_name: str         # "Chelsea"
    full_label: str        # "Chelsea (Kray) Goals"
    line: float            # 2.5, 3.5, 4.5
    over_odds: float       # decimal odds for Over
    under_odds: float      # decimal odds for Under
    bet365_fi: str = ""    # bet365 event FI for reference


class MatchResultOdds(BaseModel):
    """1X2 match result odds from bet365 for eSoccer."""

    home_odds: float       # odds para vitória do home
    draw_odds: float       # odds para empate
    away_odds: float       # odds para vitória do away
    bet365_fi: str = ""


class Bet365InplayEvent(BaseModel):
    """A live event from the bet365 inplay API."""

    fi: str                # bet365 FI identifier
    ev_id: str = ""        # bet365 event ID for URL (ex: 151316262272C1)
    our_event_id: str      # maps to BetsAPI event id
    home_name: str         # "Liverpool (GianniKid)"
    away_name: str         # "Chelsea (Kray)"
    home_player: str = ""  # "GianniKid"
    away_player: str = ""  # "Kray"
    home_team: str = ""    # "Liverpool"
    away_team: str = ""    # "Chelsea"
    score: str | None = "0-0"
    league_name: str = ""

    @property
    def bet365_url(self) -> str:
        """URL direta para o jogo na bet365."""
        if self.ev_id:
            return f"https://www.bet365.bet.br/#/IP/EV{self.ev_id}"
        return "https://www.bet365.bet.br/#/IP/B151/"


class APIResponse(BaseModel):
    """Raw top-level response from BetsAPI.

    Every BetsAPI endpoint returns JSON in this shape:
    {
        "success": 1,
        "results": [...],
        "pager": {"page": 1, "per_page": 50, "total": 120}
    }
    """

    success: bool = False
    results: list[dict] | None = None
    pager: dict | None = None  # {"page": int, "per_page": int, "total": int}
