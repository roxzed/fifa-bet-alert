"""Async client for BetsAPI (betsapi.com) - v1.

Handles authentication, rate limiting, retries with exponential backoff,
pagination, and maps all responses to Pydantic models.

Real v1 API response formats (discovered via live testing):
  - /league:            results = list of {"id": "42648", "name": "..."}
  - /events/ended:      results = list of event dicts with ss="1-2", home.name="Team (Player)"
  - /event/odds:        results = {"1_1": [...snapshots], "1_2": [...], "1_3": [...]}
  - /event/odds/summary results = {"Bet365": {"start": {"1_1": {...}, "1_3": {...}}, "kickoff": {...}}}

Market IDs (v1):
  1_1 = 1x2 (home_od, draw_od, away_od)
  1_2 = Asian Handicap
  1_3 = Over/Under (over_od, under_od, handicap="3.5")

Player name format: "Team (PlayerName)" e.g. "Man City (Baba)"
  → team  = "Man City"
  → player = "Baba"

Usage:
    async with BetsAPIClient(token="xxx", base_url="https://api.betsapi.com/v1") as client:
        leagues = await client.get_leagues(sport_id=1)
        events = await client.get_ended_events(league_id="40166", day="20241201")
"""

from __future__ import annotations

import asyncio
import math
import re
import time
from datetime import datetime, timezone

import httpx
from loguru import logger

from src.api.exceptions import (
    BetsAPIAuthError,
    BetsAPIError,
    BetsAPINotFoundError,
    BetsAPIRateLimitError,
    BetsAPITimeoutError,
)
from src.api.models import (
    APIEvent,
    APILeague,
    APIOdds,
    APIOddsOutcome,
    APIResponse,
    Bet365InplayEvent,
    MatchResultOdds,
    PlayerGoalsOdds,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_TIMEOUT = 30.0  # seconds
_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 2.0  # exponential backoff base in seconds
_RATE_LIMIT_WAIT_DEFAULT = 5.0  # seconds to wait on 429 if no Retry-After

# BetsAPI v1 time_status codes
_STATUS_MAP: dict[str, str] = {
    "0": "upcoming",
    "1": "inplay",
    "2": "inplay",  # some eSoccer events use 2 for in-progress
    "3": "ended",
}

# BetsAPI v1 market ID mapping
_MARKET_ID_MAP: dict[str, str] = {
    "1_1": "1x2",
    "1_2": "asian_handicap",
    "1_3": "over_under",
}

# Regex to parse "Team (PlayerName)" format
_PLAYER_TEAM_RE = re.compile(r'^(.*?)\s*\(([^)]+)\)\s*$')


class _RateLimiter:
    """Simple token-bucket rate limiter.

    Allows up to `max_tokens` requests per `refill_seconds` window.
    Conservative defaults for BetsAPI Soccer plan.
    """

    def __init__(self, max_tokens: int = 3, refill_seconds: float = 1.0):
        # BetsAPI Soccer plan: 3600 req/hour = 1 req/sec.
        # Burst de 3 para paginação (4 paginas de ended = burst natural).
        self._max_tokens = max_tokens
        self._refill_seconds = refill_seconds
        self._tokens = float(max_tokens)
        self._last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait until a token is available, then consume one."""
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
            await asyncio.sleep(0.1)

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        new_tokens = elapsed / self._refill_seconds * self._max_tokens
        self._tokens = min(self._max_tokens, self._tokens + new_tokens)
        self._last_refill = now


class BetsAPIClient:
    """Async client for BetsAPI v1.

    Args:
        token: BetsAPI authentication token.
        base_url: API base URL (default: https://api.betsapi.com/v1).
    """

    def __init__(
        self,
        token: str,
        base_url: str = "https://api.betsapi.com/v1",
        v2_url: str = "https://api.betsapi.com/v2",
    ):
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._v2_base_url = v2_url.rstrip("/")
        # Rate limiters separados: v1 e v2 têm cotas independentes
        self._rate_limiter_v1 = _RateLimiter(max_tokens=3, refill_seconds=1.0)
        self._rate_limiter_v2 = _RateLimiter(max_tokens=3, refill_seconds=1.0)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(_DEFAULT_TIMEOUT),
            headers={"Accept": "application/json"},
            follow_redirects=True,
        )

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> BetsAPIClient:
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()

    async def close(self) -> None:
        """Close the underlying httpx client."""
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Public API - Leagues
    # ------------------------------------------------------------------

    async def get_leagues(self, sport_id: int = 1) -> list[APILeague]:
        """List all leagues for a sport.

        BetsAPI v1 /league returns flat objects: {"id": "42648", "name": "..."}

        Args:
            sport_id: BetsAPI sport identifier. 1 = Soccer (includes eSoccer).

        Returns:
            List of APILeague objects.
        """
        data = await self._request_all_pages(
            "/league", params={"sport_id": sport_id}
        )
        leagues: list[APILeague] = []
        for item in data:
            league_id = str(item.get("id", "")).strip()
            league_name = item.get("name", "").strip()
            if league_id and league_name:
                leagues.append(
                    APILeague(
                        id=league_id,
                        name=league_name,
                        sport_id=sport_id,
                    )
                )
        return leagues

    async def find_league(
        self, name_contains: str, sport_id: int = 1
    ) -> APILeague | None:
        """Find a league by partial name match (case-insensitive).

        Searches page-by-page and stops as soon as a match is found,
        avoiding fetching all 30 pages unnecessarily.

        Args:
            name_contains: Substring to search for in league names.
            sport_id: BetsAPI sport identifier.

        Returns:
            First matching APILeague or None.
        """
        needle = name_contains.lower()
        page = 1
        request_params: dict = {"sport_id": sport_id}

        while True:
            request_params["page"] = page
            data = await self._request("/league", params=request_params)
            results = data.get("results") or []

            for item in results:
                league_id = str(item.get("id", "")).strip()
                league_name = item.get("name", "").strip()
                if needle in league_name.lower():
                    league = APILeague(id=league_id, name=league_name, sport_id=sport_id)
                    logger.info("Found league: {} (id={})", league.name, league.id)
                    return league

            # Check if there are more pages
            pager = data.get("pager")
            if not pager:
                break
            total = int(pager.get("total", 0))
            per_page = int(pager.get("per_page", 50))
            total_pages = math.ceil(total / per_page) if per_page > 0 else 1
            if page >= total_pages:
                break
            page += 1

        logger.warning("No league found matching '{}'", name_contains)
        return None

    # ------------------------------------------------------------------
    # Public API - Events
    # ------------------------------------------------------------------

    async def get_upcoming_events(self, league_id: str) -> list[APIEvent]:
        """Get upcoming/scheduled events for a league.

        Args:
            league_id: BetsAPI league identifier.

        Returns:
            List of upcoming APIEvent objects.
        """
        data = await self._request_all_pages(
            "/events/upcoming", params={"sport_id": 1, "league_id": league_id}
        )
        return [self._parse_event(item, default_status="upcoming") for item in data]

    async def get_inplay_events(self, league_id: str) -> list[APIEvent]:
        """Get currently live events for a league.

        Args:
            league_id: BetsAPI league identifier.

        Returns:
            List of in-play APIEvent objects.
        """
        data = await self._request_all_pages(
            "/events/inplay", params={"sport_id": 1, "league_id": league_id}
        )
        return [self._parse_event(item, default_status="inplay") for item in data]

    async def get_ended_events(
        self, league_id: str, day: str | None = None, use_v2: bool = False
    ) -> list[APIEvent]:
        """Get ended events for a league.

        Args:
            league_id: BetsAPI league identifier.
            day: Date in YYYYMMDD format. If None, defaults to today.
            use_v2: Use v2 API endpoint (required for 2026+ leagues like 22614).

        Returns:
            List of ended APIEvent objects.
        """
        params: dict = {"sport_id": 1, "league_id": league_id}
        if day is not None:
            params["day"] = day
        base_url = self._v2_base_url if use_v2 else None
        data = await self._request_all_pages("/events/ended", params=params, base_url_override=base_url)
        return [self._parse_event(item, default_status="ended") for item in data]

    async def get_event_details(self, event_id: str) -> APIEvent:
        """Get full details of a single event.

        Args:
            event_id: BetsAPI event identifier.

        Returns:
            A fully populated APIEvent.

        Raises:
            BetsAPINotFoundError: If the event does not exist.
        """
        raw = await self._request("/event/view", params={"event_id": event_id})
        results = raw.get("results", [])
        if not results:
            raise BetsAPINotFoundError(
                f"Event {event_id} not found",
                status_code=404,
            )
        item = results[0] if isinstance(results, list) else results
        return self._parse_event(item)

    # ------------------------------------------------------------------
    # Public API - Odds
    # ------------------------------------------------------------------

    async def get_event_odds(self, event_id: str) -> list[APIOdds]:
        """Get odds history snapshots for an event.

        BetsAPI v1 /event/odds returns:
            {"success": 1, "results": {"1_1": [...], "1_2": [...], "1_3": [...]}}

        Each snapshot in "1_3" (Over/Under) has:
            {"over_od": "1.825", "under_od": "1.875", "handicap": "3.5", "ss": "0-0", ...}

        Args:
            event_id: BetsAPI event identifier.

        Returns:
            List of APIOdds (one per snapshot per market).
        """
        raw = await self._request("/event/odds", params={"event_id": event_id})
        return self._parse_odds(event_id, raw)

    async def get_event_odds_summary(self, event_id: str) -> list[APIOdds]:
        """Get current odds summary for an event (pre-match + live).

        BetsAPI v1 /event/odds/summary returns:
            {"success": 1, "results": {
                "Bet365": {
                    "start": {"1_1": {...}, "1_3": {...}},
                    "kickoff": {"1_1": {...}, "1_3": {...}}
                }
            }}

        Args:
            event_id: BetsAPI event identifier.

        Returns:
            List of APIOdds (one per market per time point: start/kickoff).
        """
        raw = await self._request(
            "/event/odds/summary", params={"event_id": event_id}
        )
        return self._parse_odds_summary(event_id, raw)

    async def get_event_odds_history(self, event_id: str) -> list[APIOdds]:
        """Get historical odds movement for an event (alias for get_event_odds).

        Args:
            event_id: BetsAPI event identifier.

        Returns:
            List of APIOdds snapshots over time.
        """
        return await self.get_event_odds(event_id)

    # ------------------------------------------------------------------
    # Public API - Bet365 specific (player goals markets)
    # ------------------------------------------------------------------

    async def bet365_get_inplay_esoccer(self, league_filter: str = "Battle - 8 mins") -> list[Bet365InplayEvent]:
        """Get live eSoccer events from bet365 inplay API.

        Returns list of Bet365InplayEvent with FI identifiers needed
        to fetch player-specific goals odds.
        """
        raw = await self._request("/bet365/inplay_filter", params={"sport_id": 1})
        results = raw.get("results") or []

        events: list[Bet365InplayEvent] = []
        for item in results:
            league_name = (item.get("league") or {}).get("name", "")
            if league_filter not in league_name:
                continue

            home_full = (item.get("home") or {}).get("name", "")
            away_full = (item.get("away") or {}).get("name", "")
            home_player, home_team = self._extract_player_and_team(home_full)
            away_player, away_team = self._extract_player_and_team(away_full)

            events.append(Bet365InplayEvent(
                fi=str(item.get("id", "")),
                ev_id=str(item.get("ev_id", "")),
                our_event_id=str(item.get("our_event_id", "")),
                home_name=home_full,
                away_name=away_full,
                home_player=home_player,
                away_player=away_player,
                home_team=home_team or "",
                away_team=away_team or "",
                score=item.get("ss", "0-0"),
                league_name=league_name,
            ))
        return events

    async def bet365_get_player_goals_odds(self, fi: str) -> list[PlayerGoalsOdds]:
        """Get player-specific goals odds from a bet365 live event.

        Fetches the bet365 event data and extracts "Team (Player) Goals"
        markets with Over/Under lines and odds.

        Args:
            fi: Bet365 FI identifier from bet365_get_inplay_esoccer().

        Returns:
            List of PlayerGoalsOdds (one per player with active goals line).
        """
        raw = await self._request("/bet365/event", params={"FI": fi})
        results = raw.get("results") or []

        # Flatten: results can be [[items]] or [items]
        items = results[0] if len(results) == 1 and isinstance(results[0], list) else results

        # Parse: MG → market group, PA → line/odds, MA → over/under label
        player_odds: list[PlayerGoalsOdds] = []
        current_mg = ""
        current_line: float | None = None
        over_odds: float | None = None
        under_odds: float | None = None
        collecting = False

        for item in items:
            if not isinstance(item, dict):
                continue
            t = item.get("type", "")
            na = item.get("NA", "").strip()

            if t == "MG":
                # Save previous market if complete
                if collecting and current_line and over_odds:
                    player_name, team_name = self._extract_player_and_team(
                        current_mg.replace(" Goals", "")
                    )
                    player_odds.append(PlayerGoalsOdds(
                        player_name=player_name,
                        team_name=team_name or "",
                        full_label=current_mg,
                        line=current_line,
                        over_odds=over_odds,
                        under_odds=under_odds or 0.0,
                        bet365_fi=fi,
                    ))

                # Check if new MG is a player goals market
                current_mg = na
                collecting = na.endswith("Goals") and na not in (
                    "Match Goals", "1st Half Goals", "Goals Odd/Even",
                )
                current_line = None
                over_odds = None
                under_odds = None

            elif collecting and t == "PA":
                od = item.get("OD", "")
                ha = item.get("HA", "")

                if not od and na:
                    # This PA has the line value (e.g. "3.5")
                    try:
                        current_line = float(na)
                    except ValueError:
                        pass
                elif od and ha:
                    # This PA has odds — determine if Over or Under
                    decimal_odds = _fractional_to_decimal(od)
                    line_val = float(ha)
                    if current_line is None:
                        current_line = line_val
                    # Over comes before Under in the response
                    if over_odds is None:
                        over_odds = decimal_odds
                    else:
                        under_odds = decimal_odds

        # Don't forget the last market
        if collecting and current_line and over_odds:
            player_name, team_name = self._extract_player_and_team(
                current_mg.replace(" Goals", "")
            )
            player_odds.append(PlayerGoalsOdds(
                player_name=player_name,
                team_name=team_name or "",
                full_label=current_mg,
                line=current_line,
                over_odds=over_odds,
                under_odds=under_odds or 0.0,
                bet365_fi=fi,
            ))

        return player_odds

    async def bet365_get_match_result_odds(self, fi: str) -> MatchResultOdds | None:
        """Get 1X2 (match result) odds from a bet365 live event.

        Parses the bet365 event data to find the "Match Result" or "Full Time Result"
        market group and extracts Home/Draw/Away odds.

        Returns MatchResultOdds or None if market not found.
        """
        raw = await self._request("/bet365/event", params={"FI": fi})
        results = raw.get("results") or []
        items = results[0] if len(results) == 1 and isinstance(results[0], list) else results

        home_odds = None
        draw_odds = None
        away_odds = None
        collecting = False

        for item in items:
            if not isinstance(item, dict):
                continue
            t = item.get("type", "")
            na = item.get("NA", "").strip()

            if t == "MG":
                # Match Result / Full Time Result market
                collecting = na in (
                    "Match Result", "Full Time Result", "1X2",
                    "Game Lines", "Game Result",
                )
                if not collecting:
                    # Stop if we already found odds and moved to next MG
                    if home_odds is not None:
                        break

            elif collecting and t == "PA":
                od = item.get("OD", "")
                if not od:
                    continue
                decimal_odds = _fractional_to_decimal(od)
                # Determine outcome: Home(1) / Draw(X) / Away(2)
                header = item.get("HD", "") or item.get("HA", "")
                if na in ("1", "Home") or header == "1":
                    home_odds = decimal_odds
                elif na in ("X", "Draw") or header == "X":
                    draw_odds = decimal_odds
                elif na in ("2", "Away") or header == "2":
                    away_odds = decimal_odds

        if home_odds and away_odds:
            return MatchResultOdds(
                home_odds=home_odds,
                draw_odds=draw_odds or 0.0,
                away_odds=away_odds,
                bet365_fi=fi,
            )
        return None

    # ------------------------------------------------------------------
    # Internal - HTTP request with retry / rate-limit / error handling
    # ------------------------------------------------------------------

    async def _request(
        self, endpoint: str, params: dict | None = None, base_url_override: str | None = None
    ) -> dict:
        """Make a single authenticated request with retry logic.

        Args:
            endpoint: API path (e.g. '/events/upcoming').
            params: Extra query parameters.
            base_url_override: Override the base URL (e.g. for v2 endpoints).

        Returns:
            Parsed JSON dict from the API.
        """
        base = base_url_override if base_url_override else self._base_url
        url = f"{base}{endpoint}"
        request_params = dict(params or {})
        request_params["token"] = self._token

        last_exception: Exception | None = None

        for attempt in range(1, _MAX_RETRIES + 1):
            # Rate limiter separado por API (v1 vs v2)
            limiter = self._rate_limiter_v2 if base == self._v2_base_url else self._rate_limiter_v1
            await limiter.acquire()

            logger.debug(
                "BetsAPI request attempt {}/{}: {} params={}",
                attempt,
                _MAX_RETRIES,
                endpoint,
                {k: v for k, v in request_params.items() if k != "token"},
            )

            try:
                response = await self._client.get(url, params=request_params)
            except httpx.TimeoutException as exc:
                last_exception = BetsAPITimeoutError(
                    f"Timeout on {endpoint} (attempt {attempt}/{_MAX_RETRIES})"
                )
                logger.warning(
                    "Timeout on {} (attempt {}/{}): {}",
                    endpoint,
                    attempt,
                    _MAX_RETRIES,
                    exc,
                )
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_RETRY_BACKOFF_BASE ** attempt)
                continue
            except httpx.HTTPError as exc:
                last_exception = BetsAPIError(
                    f"HTTP error on {endpoint}: {exc}",
                )
                logger.warning(
                    "HTTP error on {} (attempt {}/{}): {}",
                    endpoint,
                    attempt,
                    _MAX_RETRIES,
                    exc,
                )
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_RETRY_BACKOFF_BASE ** attempt)
                continue

            status = response.status_code

            # --- Rate limit (429) ---
            if status == 429:
                # Use x-ratelimit-reset (absolute Unix timestamp) if available
                reset_ts = response.headers.get("x-ratelimit-reset")
                if reset_ts:
                    wait = max(0.0, float(reset_ts) - time.time()) + 2.0
                else:
                    wait = float(response.headers.get("Retry-After", 60.0))

                logger.warning(
                    "Rate limited on {} - waiting {:.0f}s until reset (attempt {}/{})",
                    endpoint,
                    wait,
                    attempt,
                    _MAX_RETRIES,
                )
                last_exception = BetsAPIRateLimitError(
                    retry_after=wait,
                    response_body=response.text,
                )
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(wait)
                continue

            # --- Auth errors (401, 403) ---
            if status in (401, 403):
                raise BetsAPIAuthError(
                    message=f"Auth error on {endpoint} (HTTP {status})",
                    status_code=status,
                    response_body=response.text,
                )

            # --- Not found (404) ---
            if status == 404:
                raise BetsAPINotFoundError(
                    message=f"Not found: {endpoint}",
                    status_code=status,
                    response_body=response.text,
                )

            # --- Other server errors (5xx) ---
            if status >= 500:
                last_exception = BetsAPIError(
                    message=f"Server error on {endpoint} (HTTP {status})",
                    status_code=status,
                    response_body=response.text,
                )
                logger.warning(
                    "Server error {} on {} (attempt {}/{})",
                    status,
                    endpoint,
                    attempt,
                    _MAX_RETRIES,
                )
                if attempt < _MAX_RETRIES:
                    await asyncio.sleep(_RETRY_BACKOFF_BASE ** attempt)
                continue

            # --- Other client errors (4xx) ---
            if status >= 400:
                raise BetsAPIError(
                    message=f"Client error on {endpoint} (HTTP {status})",
                    status_code=status,
                    response_body=response.text,
                )

            # --- Success ---
            data: dict = response.json()
            success_flag = data.get("success")
            if success_flag not in (1, True, "1"):
                logger.warning(
                    "BetsAPI returned success={} for {}: {}",
                    success_flag,
                    endpoint,
                    data,
                )
                return {"success": 0, "results": []}
            logger.debug(
                "BetsAPI response for {}: {}",
                endpoint,
                str(data)[:200],
            )
            return data

        if last_exception is not None:
            raise last_exception
        raise BetsAPIError(f"All {_MAX_RETRIES} retries failed for {endpoint}")

    async def _request_all_pages(
        self, endpoint: str, params: dict | None = None, base_url_override: str | None = None
    ) -> list[dict]:
        """Fetch all pages for a paginated endpoint.

        BetsAPI pagination: {"page": 1, "per_page": 50, "total": 120}

        Args:
            endpoint: API path.
            params: Query parameters (page will be injected).
            base_url_override: Override the base URL (e.g. for v2 endpoints).

        Returns:
            Aggregated list of result dicts across all pages.
        """
        all_results: list[dict] = []
        page = 1
        request_params = dict(params or {})

        while True:
            request_params["page"] = page
            data = await self._request(endpoint, params=request_params, base_url_override=base_url_override)
            results = data.get("results") or []

            # Some endpoints return a dict instead of a list (single result)
            if isinstance(results, dict):
                results = [results]

            all_results.extend(results)

            pager = data.get("pager")
            if not pager:
                break

            total = int(pager.get("total", 0))
            per_page = int(pager.get("per_page", 50))
            total_pages = math.ceil(total / per_page) if per_page > 0 else 1

            if page >= total_pages:
                break

            page += 1
            logger.debug("Fetching page {}/{} for {}", page, total_pages, endpoint)

        return all_results

    # ------------------------------------------------------------------
    # Internal - Response parsing
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_player_and_team(full_name: str) -> tuple[str, str | None]:
        """Parse player name and team from BetsAPI 'Team (PlayerName)' format.

        Examples:
            "Man City (Baba)"    → ("Baba", "Man City")
            "Barcelona (Carlos)" → ("Carlos", "Barcelona")
            "JohnDoe"            → ("JohnDoe", None)

        Args:
            full_name: Raw name string from the API.

        Returns:
            Tuple of (player_name, team_name_or_None).
        """
        match = _PLAYER_TEAM_RE.match(full_name.strip())
        if match:
            team = match.group(1).strip()
            player = match.group(2).strip()
            return player, team if team else None
        return full_name.strip(), None

    @staticmethod
    def _parse_event(item: dict, default_status: str = "upcoming") -> APIEvent:
        """Parse a raw event dict into an APIEvent model.

        BetsAPI v1 event structure:
        {
            "id": "12345",
            "sport_id": "1",
            "league": {"id": "40166", "name": "Esoccer Battle..."},
            "home": {"name": "Man City (Baba)"},
            "away": {"name": "Barcelona (Carlos)"},
            "ss": "2-1",           # score: "home-away"
            "time_status": "3",    # 0=upcoming, 1/2=inplay, 3=ended
            "time": "1711300000",  # unix timestamp
        }
        """
        # League info
        league_raw = item.get("league") or {}
        league_id = str(league_raw.get("id", ""))
        league_name = league_raw.get("name", "")

        # Player + team names (format: "Team (PlayerName)")
        home_raw = item.get("home") or {}
        away_raw = item.get("away") or {}
        home_full = home_raw.get("name", "")
        away_full = away_raw.get("name", "")

        home_name, home_team = BetsAPIClient._extract_player_and_team(home_full)
        away_name, away_team = BetsAPIClient._extract_player_and_team(away_full)

        # Score from "ss" field: "2-1"
        home_score: int | None = None
        away_score: int | None = None
        score_str = item.get("ss", "")
        if score_str and "-" in str(score_str):
            parts = str(score_str).split("-")
            try:
                home_score = int(parts[0].strip())
                away_score = int(parts[1].strip())
            except (ValueError, IndexError):
                pass

        # Fallback to separate score fields
        if home_score is None:
            raw_hs = item.get("home_score") or (item.get("scores") or {}).get("home")
            if raw_hs is not None:
                try:
                    home_score = int(raw_hs)
                except (ValueError, TypeError):
                    pass
        if away_score is None:
            raw_as = item.get("away_score") or (item.get("scores") or {}).get("away")
            if raw_as is not None:
                try:
                    away_score = int(raw_as)
                except (ValueError, TypeError):
                    pass

        # Status
        time_status = str(item.get("time_status", ""))
        status = _STATUS_MAP.get(time_status, default_status)

        # Scheduled time (unix timestamp)
        raw_time = item.get("time", 0)
        try:
            scheduled_time = datetime.fromtimestamp(int(raw_time), tz=timezone.utc)
        except (ValueError, TypeError, OSError):
            scheduled_time = datetime.now(tz=timezone.utc)

        # Extra - override team names if provided
        extra = item.get("extra") or {}
        if extra.get("home_team") or extra.get("homeTeam"):
            home_team = extra.get("home_team") or extra.get("homeTeam")
        if extra.get("away_team") or extra.get("awayTeam"):
            away_team = extra.get("away_team") or extra.get("awayTeam")

        # Stats
        stats = item.get("stats") or item.get("statistics")

        return APIEvent(
            id=str(item.get("id", "")),
            league_id=league_id,
            league_name=league_name,
            home_name=home_name,
            away_name=away_name,
            home_score=home_score,
            away_score=away_score,
            status=status,
            scheduled_time=scheduled_time,
            home_team=home_team,
            away_team=away_team,
            stats=stats,
        )

    @staticmethod
    def _parse_odds(event_id: str, raw: dict) -> list[APIOdds]:
        """Parse raw /event/odds response.

        BetsAPI v1 /event/odds structure:
        {
            "success": 1,
            "results": {
                "1_1": [{"home_od": "2.45", "draw_od": "3.70", "away_od": "2.30",
                         "ss": "0-0", "time_str": "0", "add_time": "1700000000"}, ...],
                "1_2": [...],
                "1_3": [{"over_od": "1.825", "under_od": "1.875", "handicap": "3.5",
                         "ss": "0-0", "time_str": "0", "add_time": "1700000000"}, ...]
            }
        }

        Args:
            event_id: BetsAPI event identifier.
            raw: Raw API response dict.

        Returns:
            List of APIOdds (one per market per snapshot).
        """
        results_block = raw.get("results") or {}

        if not isinstance(results_block, dict):
            return []

        all_odds: list[APIOdds] = []

        for market_key, entries in results_block.items():
            market_name = _MARKET_ID_MAP.get(market_key, market_key)

            if not isinstance(entries, list):
                continue

            for snapshot in entries:
                if not isinstance(snapshot, dict):
                    continue

                outcomes = _parse_snapshot_outcomes(snapshot, market_name)
                if not outcomes:
                    continue

                raw_ts = snapshot.get("add_time") or snapshot.get("time") or 0
                try:
                    updated_at = datetime.fromtimestamp(int(raw_ts), tz=timezone.utc)
                except (ValueError, TypeError, OSError):
                    updated_at = datetime.now(tz=timezone.utc)

                all_odds.append(
                    APIOdds(
                        event_id=event_id,
                        market=market_name,
                        outcomes=outcomes,
                        updated_at=updated_at,
                    )
                )

        return all_odds

    @staticmethod
    def _parse_odds_summary(event_id: str, raw: dict) -> list[APIOdds]:
        """Parse raw /event/odds/summary response.

        BetsAPI v1 /event/odds/summary structure:
        {
            "success": 1,
            "results": {
                "Bet365": {
                    "start": {
                        "1_1": {"home_od": "2.45", "draw_od": "3.70", "away_od": "2.30",
                                "id": "...", "handicap": "0"},
                        "1_3": {"over_od": "1.825", "under_od": "1.875", "handicap": "3.5",
                                "id": "..."}
                    },
                    "kickoff": {
                        "1_1": {...},
                        "1_3": {...}
                    }
                }
            }
        }

        Args:
            event_id: BetsAPI event identifier.
            raw: Raw API response dict.

        Returns:
            List of APIOdds (start and kickoff odds per market).
        """
        results_block = raw.get("results") or {}

        if not isinstance(results_block, dict):
            return []

        all_odds: list[APIOdds] = []
        now = datetime.now(tz=timezone.utc)

        for bookmaker, time_points in results_block.items():
            if not isinstance(time_points, dict):
                continue

            for time_label, markets in time_points.items():
                # time_label = "start", "kickoff", "live", etc.
                if not isinstance(markets, dict):
                    continue

                for market_key, snapshot in markets.items():
                    if not isinstance(snapshot, dict):
                        continue

                    market_name = _MARKET_ID_MAP.get(market_key, market_key)
                    # Tag market with time point: "over_under_start", "over_under_kickoff"
                    tagged_market = f"{market_name}_{time_label}"

                    outcomes = _parse_snapshot_outcomes(snapshot, market_name)
                    if not outcomes:
                        continue

                    all_odds.append(
                        APIOdds(
                            event_id=event_id,
                            market=tagged_market,
                            outcomes=outcomes,
                            updated_at=now,
                        )
                    )

        return all_odds


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _parse_snapshot_outcomes(
    snapshot: dict, market_name: str
) -> list[APIOddsOutcome]:
    """Extract APIOddsOutcome list from an odds snapshot dict.

    Handles 1x2 (home/draw/away) and Over/Under (over/under + handicap line).

    Args:
        snapshot: Single odds snapshot dict.
        market_name: Resolved market name (e.g. "over_under", "1x2").

    Returns:
        List of APIOddsOutcome objects.
    """
    outcomes: list[APIOddsOutcome] = []
    header = snapshot.get("handicap") or snapshot.get("ss") or None

    # 1x2 market
    if snapshot.get("home_od"):
        outcomes.append(
            APIOddsOutcome(
                name="Home",
                odds=_safe_float(snapshot["home_od"]),
                header=header,
            )
        )
    if snapshot.get("draw_od"):
        outcomes.append(
            APIOddsOutcome(
                name="Draw",
                odds=_safe_float(snapshot["draw_od"]),
                header=header,
            )
        )
    if snapshot.get("away_od"):
        outcomes.append(
            APIOddsOutcome(
                name="Away",
                odds=_safe_float(snapshot["away_od"]),
                header=header,
            )
        )

    # Over/Under market - header is the handicap line (e.g. "3.5")
    if snapshot.get("over_od"):
        line = snapshot.get("handicap", "")
        outcomes.append(
            APIOddsOutcome(
                name=f"Over {line}" if line else "Over",
                odds=_safe_float(snapshot["over_od"]),
                header=str(line) if line else None,
            )
        )
    if snapshot.get("under_od"):
        line = snapshot.get("handicap", "")
        outcomes.append(
            APIOddsOutcome(
                name=f"Under {line}" if line else "Under",
                odds=_safe_float(snapshot["under_od"]),
                header=str(line) if line else None,
            )
        )

    return outcomes


def _safe_float(value: object) -> float:
    """Convert a value to float safely, returning 0.0 on failure."""
    try:
        return float(value)  # type: ignore[arg-type]
    except (ValueError, TypeError):
        return 0.0


def _fractional_to_decimal(frac: str) -> float:
    """Convert fractional odds (e.g. '6/4') to decimal (e.g. 2.50).

    Bet365 API returns odds in fractional format like '20/21', '6/4', etc.
    Decimal = (numerator / denominator) + 1
    """
    try:
        if "/" in str(frac):
            parts = str(frac).split("/")
            return (float(parts[0]) / float(parts[1])) + 1.0
        return float(frac)
    except (ValueError, TypeError, ZeroDivisionError):
        return 0.0
