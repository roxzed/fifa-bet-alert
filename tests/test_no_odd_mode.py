"""Modo sem odd: com BET365_LIVE_ODDS_ENABLED=false, zero chamada ao bet365 premium."""

from unittest.mock import AsyncMock, MagicMock

from src.core.odds_monitor import OddsMonitor


def _monitor_with_api():
    """OddsMonitor com dependencias mockadas; api espionavel."""
    api = MagicMock()
    api.bet365_get_inplay_esoccer = AsyncMock(return_value=[])
    api.bet365_get_player_goals_odds = AsyncMock(return_value=[])
    # OddsMonitor exige varios repos/engines; passamos MagicMock nos que nao usamos aqui.
    m = OddsMonitor.__new__(OddsMonitor)
    m.api = api
    return m, api


async def test_fetch_loser_odds_noop_quando_flag_off(monkeypatch):
    from src.config import settings
    monkeypatch.setattr(settings, "bet365_live_odds_enabled", False)
    m, api = _monitor_with_api()
    rm = MagicMock()
    rm.id = 1
    rm.player_home = "A"
    rm.player_away = "B"

    result = await m._fetch_loser_odds(rm, "A")

    assert result == (None, None, None)
    api.bet365_get_inplay_esoccer.assert_not_awaited()
    api.bet365_get_player_goals_odds.assert_not_awaited()
