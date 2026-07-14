"""Hook: pair_matcher agenda preditivo no pending e cancela ao casar."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.core.pair_matcher import PairMatcher


def _pm():
    om = MagicMock()
    pm = PairMatcher(MagicMock(), MagicMock(), om)
    return pm, om


def _g1():
    g1 = MagicMock()
    g1.id = 111
    g1.player_home = "Sena"
    g1.player_away = "Bosko"
    return g1


def test_add_pending_agenda_preditivo():
    pm, om = _pm()
    pm._add_pending(_g1(), "Sena", "Bosko", {"sena", "bosko"}, "22614", 2)
    om.schedule_predictive_watch.assert_called_once()


def test_add_pending_agenda_preditivo_falha_nao_derruba_fluxo():
    """Se schedule_predictive_watch lançar exceção, _add_pending não deve propagar."""
    pm, om = _pm()
    om.schedule_predictive_watch.side_effect = RuntimeError("boom")
    pm._add_pending(_g1(), "Sena", "Bosko", {"sena", "bosko"}, "22614", 2)
    assert 111 in pm._pending


@pytest.mark.asyncio
async def test_match_from_candidates_cancela_preditivo_ao_casar():
    """Quando a volta casa via candidatos pre-buscados, cancela o watch preditivo pendente."""
    om = MagicMock()
    om.start_monitoring = AsyncMock()
    match_repo = MagicMock()
    match_repo.get_by_api_event_id = AsyncMock(return_value=None)
    return_match = MagicMock()
    return_match.id = 222
    match_repo.create = AsyncMock(return_value=return_match)
    match_repo.link_pair = AsyncMock()

    pm = PairMatcher(MagicMock(), MagicMock(), om)

    now = datetime.now(timezone.utc)
    g1 = MagicMock()
    g1.id = 111
    g1.api_event_id = "g1-api"
    g1.team_home = "TeamA"
    g1.team_away = "TeamB"
    g1.ended_at = now - timedelta(minutes=60)
    g1.started_at = now - timedelta(minutes=68)

    event = MagicMock()
    event.id = "g2-api"
    event.home_name = "Sena"
    event.away_name = "Bosko"
    event.home_team = "TeamA"
    event.away_team = "TeamB"
    event.scheduled_time = now
    event.league_name = "22614"
    event.status = "scheduled"
    event.home_score = None
    event.away_score = None

    found = await pm._match_from_candidates(
        game1_match=g1,
        loser="Sena",
        winner="Bosko",
        players={"sena", "bosko"},
        loser_goals_g1=2,
        candidates=[event],
        match_repo=match_repo,
    )

    assert found is True
    om.cancel_predictive_watch.assert_called_once_with(111)
    om.start_monitoring.assert_awaited_once()
