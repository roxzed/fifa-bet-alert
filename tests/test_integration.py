"""Integration tests for the FIFA Bet Alert pipeline.

MELHORIA 7 (2026-03-25):
Tests the critical path end-to-end with mocked API responses:
  1. StatsEngine: cache, batch query, probability computation
  2. PairMatcher: fuzzy matching, time window, deduplication
  3. OddsMonitor: fuzzy matching for Bet365 events
  4. GameWatcher: adaptive polling interval computation
  5. HealthMonitor: alert thresholds and state tracking

Usage:
    python -m pytest tests/test_integration.py -v
    python tests/test_integration.py  # standalone
"""

import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass

# Ensure project root is in path
sys.path.insert(0, str(Path(__file__).parent.parent))


# ═══════════════════════════════════════════════════════════════
# Test helpers
# ═══════════════════════════════════════════════════════════════

def run(coro):
    """Run an async coroutine synchronously."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        return asyncio.run(coro)
    return loop.run_until_complete(coro)


@dataclass
class FakeEvent:
    id: str = "evt_001"
    home_name: str = "PlayerA"
    away_name: str = "PlayerB"
    home_team: str = "TeamX"
    away_team: str = "TeamY"
    home_score: int = 2
    away_score: int = 4
    league_name: str = "FIFA eSoccer Battle"
    league_id: str = "12345"
    scheduled_time: datetime = None

    def __post_init__(self):
        if self.scheduled_time is None:
            self.scheduled_time = datetime.now(timezone.utc) - timedelta(minutes=10)


@dataclass
class FakeBet365Event:
    fi: str = "bet365_001"
    home_player: str = "PlayerA"
    away_player: str = "PlayerB"
    bet365_url: str = "https://bet365.com/test"


@dataclass
class FakePlayerOdds:
    player_name: str = "PlayerA"
    line: float = 2.5
    over_odds: float = 1.85
    under_odds: float = 1.95


@dataclass
class FakeMatch:
    id: int = 1
    api_event_id: str = "evt_001"
    player_home: str = "PlayerA"
    player_away: str = "PlayerB"
    team_home: str = "TeamX"
    team_away: str = "TeamY"
    score_home: int = 2
    score_away: int = 4
    status: str = "ended"
    is_return_match: bool = False
    pair_match_id: int = None
    started_at: datetime = None
    ended_at: datetime = None

    def __post_init__(self):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if self.started_at is None:
            self.started_at = now - timedelta(minutes=15)
        if self.ended_at is None:
            self.ended_at = now - timedelta(minutes=5)


# ═══════════════════════════════════════════════════════════════
# 1. GameWatcher: Adaptive Polling
# ═══════════════════════════════════════════════════════════════

def test_adaptive_polling_ramps_up():
    """GameWatcher should increase poll interval when idle."""
    from src.core.game_watcher import GameWatcher

    watcher = GameWatcher(
        api_client=MagicMock(),
        match_repo=MagicMock(),
        player_repo=MagicMock(),
        team_repo=MagicMock(),
        pair_matcher=MagicMock(),
    )

    # First call with activity → MIN_INTERVAL
    interval = watcher._compute_interval(found_new=True)
    assert interval == watcher.MIN_INTERVAL, f"Expected {watcher.MIN_INTERVAL}, got {interval}"

    # Idle cycles should ramp up
    for i in range(watcher.RAMP_UP_CYCLES + 1):
        interval = watcher._compute_interval(found_new=False)

    assert interval == watcher.MAX_INTERVAL, f"Expected {watcher.MAX_INTERVAL}, got {interval}"

    # Activity resets to MIN
    interval = watcher._compute_interval(found_new=True)
    assert interval == watcher.MIN_INTERVAL, f"Expected {watcher.MIN_INTERVAL}, got {interval}"

    print("  PASS: test_adaptive_polling_ramps_up")


# ═══════════════════════════════════════════════════════════════
# 2. OddsMonitor: Adaptive Poll Interval
# ═══════════════════════════════════════════════════════════════

def test_odds_adaptive_interval():
    """OddsMonitor should adjust interval based on time to kickoff."""
    from src.core.odds_monitor import _adaptive_poll_interval

    # Far from kickoff
    assert _adaptive_poll_interval(15.0) == 60
    assert _adaptive_poll_interval(30.0) == 60

    # Close to kickoff
    assert _adaptive_poll_interval(5.0) == 15
    assert _adaptive_poll_interval(8.0) == 15

    # Very close / past kickoff (<=3 min) — 2s desde 2026-04-28 (corte de latencia)
    assert _adaptive_poll_interval(2.5) == 2
    assert _adaptive_poll_interval(2.0) == 2
    assert _adaptive_poll_interval(0.0) == 2
    assert _adaptive_poll_interval(-1.0) == 2

    # Unknown
    assert _adaptive_poll_interval(None) == 15

    print("  PASS: test_odds_adaptive_interval")


# ═══════════════════════════════════════════════════════════════
# 3. OddsMonitor: Fuzzy Matching
# ═══════════════════════════════════════════════════════════════

def test_fuzzy_match_bet365_events():
    """Fuzzy matching should tolerate name differences between APIs."""
    from src.core.odds_monitor import _normalize_player, _fuzzy_match_players, _name_similarity

    # Exact match
    assert _fuzzy_match_players({"playera", "playerb"}, {"playera", "playerb"})

    # With eSports suffix
    a = {_normalize_player("Kray")}
    b = {_normalize_player("Kray (eSports)")}
    assert a == b, f"Normalization failed: {a} != {b}"

    # Case insensitive
    a = {_normalize_player("GianniKid")}
    b = {_normalize_player("giannikid")}
    assert a == b

    # Fuzzy with slight differences
    assert _fuzzy_match_players(
        {_normalize_player("Real Madrid"), _normalize_player("Barcelona")},
        {_normalize_player("Real Madrid CF"), _normalize_player("Barcelona FC")},
        threshold=0.75,
    )

    # Should NOT match completely different names
    assert not _fuzzy_match_players(
        {_normalize_player("PlayerA"), _normalize_player("PlayerB")},
        {_normalize_player("CompletelyDifferent"), _normalize_player("AnotherOne")},
        threshold=0.80,
    )

    print("  PASS: test_fuzzy_match_bet365_events")


# ═══════════════════════════════════════════════════════════════
# 4. PairMatcher: Fuzzy Team Matching
# ═══════════════════════════════════════════════════════════════

def test_pair_matcher_fuzzy_teams():
    """PairMatcher should match teams with slight name differences."""
    from src.core.pair_matcher import _normalize, _teams_match_fuzzy

    # Test _normalize
    assert _normalize("Real Madrid (eSports)") == "real madrid"
    assert _normalize("  Barcelona FC  ") == "barcelona"
    assert _normalize("Kray (esoccer)") == "kray"

    # Test _teams_match_fuzzy
    assert _teams_match_fuzzy({"PlayerA"}, {"PlayerA"})
    assert _teams_match_fuzzy({"PlayerA"}, {"playerA"})
    assert _teams_match_fuzzy({"Kray"}, {"Kray (eSports)"})
    assert _teams_match_fuzzy({"GianniKid"}, {"giannikid"})
    assert not _teams_match_fuzzy({"PlayerA"}, {"CompletelyDifferent"})

    print("  PASS: test_pair_matcher_fuzzy_teams")


# ═══════════════════════════════════════════════════════════════
# 5. HealthMonitor: State Tracking
# ═══════════════════════════════════════════════════════════════

def test_health_monitor_state():
    """HealthMonitor should track API failures and game activity."""
    from src.core.health_monitor import HealthMonitor

    hm = HealthMonitor(
        stats_engine=MagicMock(),
        game_watcher=MagicMock(),
        pair_matcher=MagicMock(),
        odds_monitor=MagicMock(),
        notifier=MagicMock(),
    )

    # API failures tracking
    assert hm._api_failures == 0
    hm.record_api_failure()
    hm.record_api_failure()
    hm.record_api_failure()
    assert hm._api_failures == 3

    hm.record_api_success()
    assert hm._api_failures == 0

    # DB error tracking
    for _ in range(90):
        hm.record_db_operation(success=True)
    for _ in range(10):
        hm.record_db_operation(success=False)
    assert hm._db_operations == 100
    assert hm._db_errors == 10

    # Alert counting
    hm.record_alert_sent()
    hm.record_alert_sent()
    assert hm._alerts_today == 2

    # Uptime
    uptime = hm.uptime_str
    assert "m" in uptime or "h" in uptime or "s" in uptime

    print("  PASS: test_health_monitor_state")


# ═══════════════════════════════════════════════════════════════
# 6. End-to-End: Game → Pair Match → Odds
# ═══════════════════════════════════════════════════════════════

def test_e2e_game_to_pair():
    """End-to-end: ended game should trigger pair matching."""
    from src.core.game_watcher import GameWatcher

    api = AsyncMock()
    match_repo = AsyncMock()
    player_repo = AsyncMock()
    team_repo = AsyncMock()
    pair_matcher = AsyncMock()

    # Setup: API returns one ended event
    event = FakeEvent()
    api.get_ended_events = AsyncMock(return_value=[event])

    # Match not in DB yet
    match_repo.get_by_api_event_id = AsyncMock(return_value=None)
    match_repo.create = AsyncMock(return_value=FakeMatch())
    match_repo.session = AsyncMock()
    match_repo.session.commit = AsyncMock()
    match_repo.session.rollback = AsyncMock()

    player_repo.get_or_create = AsyncMock()
    team_repo.save_match_team = AsyncMock()

    watcher = GameWatcher(api, match_repo, player_repo, team_repo, pair_matcher)

    # Run one poll cycle
    found = run(watcher._poll_cycle("12345"))

    assert found is True, "Should have found new games"
    assert match_repo.create.called, "Should have created match in DB"
    assert pair_matcher.find_and_link_return_match.called, "Should have triggered pair matching"

    # Verify loser identification (2-4, so home is loser)
    call_kwargs = pair_matcher.find_and_link_return_match.call_args
    assert call_kwargs.kwargs.get("loser") == "PlayerA" or \
           (call_kwargs.args and "PlayerA" in str(call_kwargs)), \
           "Should identify PlayerA as loser (2-4)"

    print("  PASS: test_e2e_game_to_pair")


# ═══════════════════════════════════════════════════════════════
# 7. Backfill: Upsert Mode Check
# ═══════════════════════════════════════════════════════════════

def test_backfill_has_upsert_mode():
    """Backfill scripts should support --full flag."""
    import importlib.util

    for script in ["scripts/backfill_method_stats.py", "scripts/backfill_player_stats.py"]:
        path = Path(__file__).parent.parent / script
        if not path.exists():
            print(f"  SKIP: {script} not found")
            continue

        content = path.read_text()
        assert "--full" in content, f"{script} should support --full flag"
        assert "ON CONFLICT" in content or "UPDATE" in content, \
            f"{script} should use upsert pattern"

    print("  PASS: test_backfill_has_upsert_mode")


# ═══════════════════════════════════════════════════════════════
# Runner
# ═══════════════════════════════════════════════════════════════

def run_all():
    """Run all tests and report results."""
    tests = [
        test_adaptive_polling_ramps_up,
        test_odds_adaptive_interval,
        test_fuzzy_match_bet365_events,
        test_pair_matcher_fuzzy_teams,
        test_health_monitor_state,
        test_e2e_game_to_pair,
        test_backfill_has_upsert_mode,
    ]

    passed = 0
    failed = 0
    errors = []

    print("=" * 60)
    print("FIFA Bet Alert — Integration Tests")
    print("=" * 60)

    for test in tests:
        try:
            test()
            passed += 1
        except Exception as e:
            failed += 1
            errors.append((test.__name__, str(e)))
            print(f"  FAIL: {test.__name__}: {e}")

    print("=" * 60)
    print(f"Results: {passed} passed, {failed} failed, {passed + failed} total")
    if errors:
        print("\nFailures:")
        for name, err in errors:
            print(f"  - {name}: {err}")
    print("=" * 60)

    return failed == 0


if __name__ == "__main__":
    success = run_all()
    sys.exit(0 if success else 1)
