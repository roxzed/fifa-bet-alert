"""
Async repository classes for FIFA Bet Alert.

All database access goes through these repositories.
Uses SQLAlchemy 2.0 select() style with async sessions.

Session isolation (2026-03-27):
Each repository method creates its own short-lived session via the
session_factory passed at construction time. This eliminates shared-session
corruption (InFailedSQLTransactionError, concurrent operations not permitted)
that previously cascaded across components.
"""

from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import AsyncGenerator, Dict, List, Optional, Sequence

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload
from loguru import logger

from src.db.models import (
    Alert,
    League,
    Match,
    MatchTeam,
    MatchupStats,
    MethodStats,
    OddsHistory,
    Player,
    PlayerTeamPreference,
    TeamStats,
)


class _BaseRepository:
    """Base class providing session-per-method isolation.

    Every public method acquires its own session from the factory,
    commits on success, rolls back on error, and always closes.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sf = session_factory

    @asynccontextmanager
    async def _session(self) -> AsyncGenerator[AsyncSession, None]:
        session = self._sf()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()

    async def execute_query(self, stmt):
        """Run an ad-hoc SQLAlchemy statement in an isolated session.

        Returns the raw Result object. Useful for complex queries that
        don't fit neatly into standard CRUD methods.
        """
        async with self._session() as session:
            return await session.execute(stmt)

    async def save_model(self, instance):
        """Persist an arbitrary ORM model instance in its own session."""
        async with self._session() as session:
            session.add(instance)
            await session.flush()


# ---------------------------------------------------------------------------
# PlayerRepository
# ---------------------------------------------------------------------------
class PlayerRepository(_BaseRepository):
    """CRUD and stats operations for the players table."""

    async def get_or_create(self, name: str) -> Player:
        """Return existing player by name or create a new one."""
        async with self._session() as session:
            stmt = select(Player).where(Player.name == name)
            result = await session.execute(stmt)
            player = result.scalar_one_or_none()
            if player is None:
                try:
                    player = Player(name=name)
                    session.add(player)
                    await session.flush()
                    logger.debug("Created new player: {}", name)
                except IntegrityError:
                    await session.rollback()
                    result = await session.execute(stmt)
                    player = result.scalar_one_or_none()
            return player

    async def get_profile(self, name: str) -> Optional[Player]:
        """Return full player profile or None."""
        async with self._session() as session:
            stmt = select(Player).where(Player.name == name)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def get_profile_by_name(self, name: str) -> Optional[Player]:
        return await self.get_profile(name)

    async def count_unique(self) -> int:
        """Return total number of unique players."""
        async with self._session() as session:
            stmt = select(func.count(Player.id))
            result = await session.execute(stmt)
            return result.scalar() or 0

    async def get_reliable_players(self, min_games: int = 10) -> Sequence[Player]:
        """Return players with at least min_games return matches."""
        async with self._session() as session:
            stmt = (
                select(Player)
                .where(Player.total_return_matches >= min_games)
                .order_by(Player.reliability_score.desc())
            )
            result = await session.execute(stmt)
            return result.scalars().all()

    async def update_stats_after_validation(
        self,
        name: str,
        *,
        over25_hit: bool,
        over35_hit: bool,
        actual_goals: int,
        loss_type: str,
    ) -> None:
        """Update player aggregate stats after a return match result is validated."""
        async with self._session() as session:
            stmt = select(Player).where(Player.name == name)
            result = await session.execute(stmt)
            player = result.scalar_one_or_none()
            if player is None:
                try:
                    player = Player(name=name)
                    session.add(player)
                    await session.flush()
                except IntegrityError:
                    await session.rollback()
                    result = await session.execute(stmt)
                    player = result.scalar_one_or_none()

            player.total_return_matches += 1

            if over25_hit:
                player.over25_after_loss += 1
            if over35_hit:
                player.over35_after_loss += 1

            if player.total_return_matches > 0:
                player.hit_rate_25 = player.over25_after_loss / player.total_return_matches
                player.hit_rate_35 = player.over35_after_loss / player.total_return_matches

            prev_total = player.avg_goals_after_loss * (player.total_return_matches - 1)
            player.avg_goals_after_loss = (prev_total + actual_goals) / player.total_return_matches

            margin = 1 if "tight" in loss_type else (2 if "medium" in loss_type else 3)
            if margin == 1:
                player.tight_loss_count += 1
                if over25_hit:
                    player.tight_loss_over25 += 1
            elif margin == 2:
                player.medium_loss_count += 1
                if over25_hit:
                    player.medium_loss_over25 += 1
            else:
                player.blowout_loss_count += 1
                if over25_hit:
                    player.blowout_loss_over25 += 1

            player.is_reliable = player.total_return_matches >= 10
            if player.total_return_matches >= 10:
                volume_score = min(player.total_return_matches / 50.0, 1.0) * 50
                consistency_score = player.hit_rate_25 * 50
                player.reliability_score = volume_score + consistency_score

            player.last_seen = datetime.now(timezone.utc).replace(tzinfo=None)
            await session.flush()


# ---------------------------------------------------------------------------
# MatchRepository
# ---------------------------------------------------------------------------
class MatchRepository(_BaseRepository):
    """CRUD and querying for the matches table."""

    async def create(self, **kwargs) -> Match:
        """Create a new match record."""
        async with self._session() as session:
            match = Match(**kwargs)
            session.add(match)
            await session.flush()
            logger.debug(
                "Created match id={} {} vs {}",
                match.id,
                match.player_home,
                match.player_away,
            )
            return match

    async def get_by_api_event_id(self, api_event_id: str) -> Optional[Match]:
        """Return a match by its external API event ID."""
        async with self._session() as session:
            stmt = select(Match).where(Match.api_event_id == api_event_id)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def get_by_id(self, match_id: int) -> Optional[Match]:
        """Return a match by primary key."""
        async with self._session() as session:
            stmt = select(Match).where(Match.id == match_id)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def count_total(self) -> int:
        """Return total number of matches in the database."""
        async with self._session() as session:
            stmt = select(func.count(Match.id))
            result = await session.execute(stmt)
            return result.scalar() or 0

    async def count_pairs(self) -> int:
        """Return number of matched ida/volta pairs."""
        async with self._session() as session:
            stmt = select(func.count(Match.id)).where(
                Match.pair_match_id.is_not(None),
                Match.is_return_match == False,  # noqa: E712
            )
            result = await session.execute(stmt)
            return result.scalar() or 0

    async def get_oldest_match_date(self) -> Optional[datetime]:
        """Return the earliest started_at across all matches."""
        async with self._session() as session:
            stmt = select(func.min(Match.started_at))
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def get_all_pairs(self) -> List[tuple]:
        """Return all (game1, game2) pairs where game1 is the original match."""
        async with self._session() as session:
            stmt = (
                select(Match)
                .options(selectinload(Match.pair_match))
                .where(
                    Match.pair_match_id.is_not(None),
                    Match.is_return_match == False,  # noqa: E712
                )
                .order_by(Match.started_at.asc())
            )
            result = await session.execute(stmt)
            game1_matches = result.scalars().all()
            return [(m, m.pair_match) for m in game1_matches if m.pair_match is not None]

    async def get_unlinked_matches(self, days_back: int = 90) -> Sequence[Match]:
        """Return unlinked (no pair) non-return matches within the last days_back days."""
        async with self._session() as session:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
            stmt = (
                select(Match)
                .where(
                    Match.pair_match_id.is_(None),
                    Match.is_return_match == False,  # noqa: E712
                    Match.started_at >= cutoff,
                    Match.status == "ended",
                )
                .order_by(Match.started_at.asc())
            )
            result = await session.execute(stmt)
            return result.scalars().all()

    async def find_return_match(
        self,
        player1: str,
        player2: str,
        after_time: datetime,
        window_min: int = 45,
        window_max: int = 75,
    ) -> Optional[Match]:
        """Find a return match between player1 and player2 within a time window."""
        async with self._session() as session:
            earliest = after_time + timedelta(minutes=window_min)
            latest = after_time + timedelta(minutes=window_max)

            stmt = (
                select(Match)
                .where(
                    or_(
                        and_(Match.player_home == player1, Match.player_away == player2),
                        and_(Match.player_home == player2, Match.player_away == player1),
                    ),
                    Match.started_at >= earliest,
                    Match.started_at <= latest,
                    Match.pair_match_id.is_(None),
                    Match.status == "ended",
                )
                .order_by(Match.started_at.asc())
                .limit(1)
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def link_pair(
        self, match_id: int, return_match_id: int, time_between: Optional[int]
    ) -> None:
        """Link two matches as a ida/volta pair."""
        async with self._session() as session:
            stmt1 = select(Match).where(Match.id == match_id)
            stmt2 = select(Match).where(Match.id == return_match_id)
            m1 = (await session.execute(stmt1)).scalar_one_or_none()
            m2 = (await session.execute(stmt2)).scalar_one_or_none()
            if m1 and m2:
                m1.pair_match_id = return_match_id
                m1.time_between_games = time_between
                m2.pair_match_id = match_id
                m2.is_return_match = True
                m2.time_between_games = time_between
                await session.flush()

    async def get_unvalidated_return_matches(self) -> Sequence[Match]:
        """Return return matches that have at least one unvalidated alert."""
        async with self._session() as session:
            stmt = (
                select(Match)
                .join(Alert, Alert.match_id == Match.id)
                .where(
                    Match.is_return_match == True,  # noqa: E712
                    Alert.validated_at.is_(None),
                )
                .distinct()
                .order_by(Match.started_at.asc())
            )
            result = await session.execute(stmt)
            return result.scalars().all()

    async def get_ended_since(
        self, start: datetime, end: datetime
    ) -> Sequence[Match]:
        """Return ended matches between two timestamps."""
        async with self._session() as session:
            stmt = (
                select(Match)
                .where(
                    Match.status == "ended",
                    Match.ended_at >= start,
                    Match.ended_at <= end,
                )
                .order_by(Match.ended_at.asc())
            )
            result = await session.execute(stmt)
            return result.scalars().all()

    async def get_return_matches_by_player(self, player_name: str) -> Sequence[Match]:
        """Return all completed return matches where the player participated."""
        async with self._session() as session:
            stmt = (
                select(Match)
                .where(
                    Match.is_return_match == True,  # noqa: E712
                    Match.score_home.is_not(None),
                    or_(
                        Match.player_home == player_name,
                        Match.player_away == player_name,
                    ),
                )
            )
            result = await session.execute(stmt)
            return result.scalars().all()

    async def update_result(
        self,
        match_id: int,
        *,
        score_home: int,
        score_away: int,
        ended_at: Optional[datetime] = None,
        **extra_stats,
    ) -> Optional[Match]:
        """Update a match with its final result."""
        async with self._session() as session:
            stmt = select(Match).where(Match.id == match_id)
            match = (await session.execute(stmt)).scalar_one_or_none()
            if match is None:
                return None
            match.score_home = score_home
            match.score_away = score_away
            match.status = "ended"
            dt = ended_at or datetime.now(timezone.utc)
            match.ended_at = dt.replace(tzinfo=None) if dt.tzinfo is not None else dt
            for k, v in extra_stats.items():
                if v is not None and hasattr(match, k):
                    setattr(match, k, v)
            await session.flush()
            return match


# ---------------------------------------------------------------------------
# OddsRepository
# ---------------------------------------------------------------------------
class OddsRepository(_BaseRepository):
    """CRUD for the odds_history table."""

    async def save_snapshot(
        self,
        *,
        match_id: int,
        player: str,
        market: str,
        odds_value: float,
    ) -> OddsHistory:
        """Record a single odds snapshot."""
        async with self._session() as session:
            entry = OddsHistory(
                match_id=match_id,
                player=player,
                market=market,
                odds_value=odds_value,
            )
            session.add(entry)
            await session.flush()
            return entry

    async def get_history(
        self,
        match_id: int,
        player: str,
        market: str,
    ) -> Sequence[OddsHistory]:
        """Return full odds history for a (match, player, market) tuple."""
        async with self._session() as session:
            stmt = (
                select(OddsHistory)
                .where(
                    OddsHistory.match_id == match_id,
                    OddsHistory.player == player,
                    OddsHistory.market == market,
                )
                .order_by(OddsHistory.captured_at.asc())
            )
            result = await session.execute(stmt)
            return result.scalars().all()

    async def get_latest(
        self,
        match_id: int,
        market: str,
    ) -> Optional[OddsHistory]:
        """Return the most recent odds entry for a match + market."""
        async with self._session() as session:
            stmt = (
                select(OddsHistory)
                .where(
                    OddsHistory.match_id == match_id,
                    OddsHistory.market == market,
                )
                .order_by(OddsHistory.captured_at.desc())
                .limit(1)
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()


# ---------------------------------------------------------------------------
# AlertRepository
# ---------------------------------------------------------------------------
class AlertRepository(_BaseRepository):
    """CRUD and querying for the alerts table."""

    async def create(self, **kwargs) -> Alert:
        """Create a new alert record."""
        async with self._session() as session:
            alert = Alert(**kwargs)
            session.add(alert)
            await session.flush()
            return alert

    async def validate(
        self,
        alert_id: int,
        actual_goals: int,
        over25_hit: bool,
        over35_hit: bool,
        over15_hit: bool | None = None,
        over45_hit: bool | None = None,
        profit_flat: float | None = None,
        ml_hit: bool | None = None,
    ) -> Optional[Alert]:
        """Record post-game result for an alert."""
        async with self._session() as session:
            stmt = select(Alert).where(Alert.id == alert_id)
            result = await session.execute(stmt)
            alert = result.scalar_one_or_none()
            if alert:
                alert.actual_goals = actual_goals
                alert.over25_hit = over25_hit
                alert.over35_hit = over35_hit
                if over15_hit is not None:
                    alert.over15_hit = over15_hit
                else:
                    alert.over15_hit = actual_goals > 1
                if over45_hit is not None:
                    alert.over45_hit = over45_hit
                else:
                    alert.over45_hit = actual_goals > 4
                if profit_flat is not None:
                    alert.profit_flat = profit_flat
                if ml_hit is not None:
                    alert.ml_hit = ml_hit
                alert.validated_at = datetime.now(timezone.utc).replace(tzinfo=None)
                await session.flush()
            return alert

    async def get_period_stats(self, days: int = 50) -> Dict:
        """Return aggregated alert stats for the last N days."""
        async with self._session() as session:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            base_filter = Alert.sent_at >= cutoff

            total = (
                await session.execute(
                    select(func.count(Alert.id)).where(base_filter)
                )
            ).scalar() or 0

            validated = (
                await session.execute(
                    select(func.count(Alert.id)).where(
                        base_filter, Alert.validated_at.is_not(None)
                    )
                )
            ).scalar() or 0

            over25_hits = (
                await session.execute(
                    select(func.count(Alert.id)).where(
                        base_filter, Alert.over25_hit == True  # noqa: E712
                    )
                )
            ).scalar() or 0

            over35_hits = (
                await session.execute(
                    select(func.count(Alert.id)).where(
                        base_filter, Alert.over35_hit == True  # noqa: E712
                    )
                )
            ).scalar() or 0

            return {
                "total": total,
                "validated": validated,
                "over25_hits": over25_hits,
                "over35_hits": over35_hits,
                "hit_rate_25": over25_hits / validated if validated > 0 else 0.0,
                "hit_rate_35": over35_hits / validated if validated > 0 else 0.0,
            }

    async def get_daily_stats(self, target: date) -> Dict:
        """Return alert stats for a specific day."""
        async with self._session() as session:
            start = datetime.combine(target, datetime.min.time())
            end = datetime.combine(target, datetime.max.time())
            base_filter = and_(Alert.sent_at >= start, Alert.sent_at <= end)

            total = (await session.execute(
                select(func.count(Alert.id)).where(base_filter)
            )).scalar() or 0
            validated = (await session.execute(
                select(func.count(Alert.id)).where(
                    base_filter, Alert.validated_at.is_not(None)
                )
            )).scalar() or 0
            over25_hits = (await session.execute(
                select(func.count(Alert.id)).where(
                    base_filter, Alert.over25_hit == True  # noqa: E712
                )
            )).scalar() or 0
            over35_hits = (await session.execute(
                select(func.count(Alert.id)).where(
                    base_filter, Alert.over35_hit == True  # noqa: E712
                )
            )).scalar() or 0

            return {
                "date": target.isoformat(),
                "total": total,
                "validated": validated,
                "over25_hits": over25_hits,
                "over35_hits": over35_hits,
                "hit_rate_25": over25_hits / validated if validated > 0 else 0.0,
                "hit_rate_35": over35_hits / validated if validated > 0 else 0.0,
            }

    async def get_by_star_rating(self, min_stars: int) -> Sequence[Alert]:
        """Return all alerts with star_rating >= min_stars, newest first."""
        async with self._session() as session:
            stmt = (
                select(Alert)
                .where(Alert.star_rating >= min_stars)
                .order_by(Alert.sent_at.desc())
            )
            result = await session.execute(stmt)
            return result.scalars().all()

    async def get_unvalidated(self) -> Sequence[Alert]:
        """Return alerts that have not been validated yet."""
        async with self._session() as session:
            stmt = (
                select(Alert)
                .where(Alert.validated_at.is_(None))
                .order_by(Alert.sent_at.asc())
            )
            result = await session.execute(stmt)
            return result.scalars().all()

    async def get_by_match_id(self, match_id: int) -> Optional[Alert]:
        """Return alert for a given match_id (most recent)."""
        async with self._session() as session:
            stmt = (
                select(Alert)
                .where(Alert.match_id == match_id)
                .order_by(Alert.sent_at.desc())
                .limit(1)
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def get_all_by_match_id(self, match_id: int) -> Sequence[Alert]:
        """Return ALL alerts for a given match_id."""
        async with self._session() as session:
            stmt = (
                select(Alert)
                .where(Alert.match_id == match_id)
                .order_by(Alert.sent_at.asc())
            )
            result = await session.execute(stmt)
            return result.scalars().all()

    async def update_telegram_message_id(self, alert_id: int, message_id: int) -> None:
        """Save the Telegram message_id so we can edit the message later."""
        async with self._session() as session:
            stmt = (
                update(Alert)
                .where(Alert.id == alert_id)
                .values(telegram_message_id=message_id)
            )
            await session.execute(stmt)

    async def get_all_validated_for_export(self) -> Sequence[Alert]:
        """Return all validated alerts for spreadsheet export, oldest first."""
        async with self._session() as session:
            stmt = (
                select(Alert)
                .where(Alert.validated_at.is_not(None))
                .order_by(Alert.sent_at.asc())
            )
            result = await session.execute(stmt)
            return result.scalars().all()

    async def get_today_results(self) -> list[Alert]:
        """Return all alerts from today (UTC), ordered by sent_at."""
        async with self._session() as session:
            today_start = datetime.combine(date.today(), datetime.min.time())
            stmt = (
                select(Alert)
                .where(Alert.sent_at >= today_start)
                .order_by(Alert.sent_at.asc())
            )
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def get_pnl_summary(self, days: int = 30) -> Dict:
        """Resumo de P&L dos ultimos N dias."""
        async with self._session() as session:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            stmt = (
                select(Alert)
                .where(Alert.validated_at.is_not(None), Alert.sent_at >= cutoff)
                .order_by(Alert.sent_at.desc())
            )
            result = await session.execute(stmt)
            alerts = result.scalars().all()

            total = len(alerts)
            if total == 0:
                return {"total": 0, "wins": 0, "losses": 0, "profit": 0.0,
                        "roi": 0.0, "hit_rate": 0.0, "by_line": {}, "by_player": {}}

            profit = 0.0
            wins = 0
            by_line: Dict[str, Dict] = {}
            by_player: Dict[str, Dict] = {}

            for a in alerts:
                bl = a.best_line or "over25"
                if bl == "over45":
                    hit, odds = a.over45_hit, a.over45_odds
                elif bl == "over35":
                    hit, odds = a.over35_hit, a.over35_odds
                elif bl == "over15":
                    hit = a.over15_hit
                    odds = a.over15_odds
                else:
                    hit, odds = a.over25_hit, a.over25_odds
                odds = odds or 1.0
                p = (odds - 1.0) if hit else -1.0
                profit += p
                if hit:
                    wins += 1
                for group, key in [(by_line, bl), (by_player, a.losing_player)]:
                    if key not in group:
                        group[key] = {"total": 0, "wins": 0, "profit": 0.0}
                    group[key]["total"] += 1
                    group[key]["profit"] += p
                    if hit:
                        group[key]["wins"] += 1

            return {"total": total, "wins": wins, "losses": total - wins,
                    "profit": round(profit, 2), "roi": round(profit / total, 4) if total else 0.0,
                    "hit_rate": round(wins / total, 4) if total else 0.0,
                    "by_line": by_line, "by_player": by_player}

    async def get_player_performance(self, days: int = 30, min_alerts: int = 2) -> List[Dict]:
        """Return per-player performance stats sorted by profit."""
        async with self._session() as session:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).replace(tzinfo=None)
            stmt = (
                select(Alert)
                .where(Alert.validated_at.is_not(None), Alert.sent_at >= cutoff)
                .order_by(Alert.sent_at.asc())
            )
            result = await session.execute(stmt)
            alerts = result.scalars().all()

            players: Dict[str, Dict] = {}
            for a in alerts:
                bl = a.best_line or "over25"
                if bl == "over45":
                    hit, odds = a.over45_hit, a.over45_odds
                elif bl == "over35":
                    hit, odds = a.over35_hit, a.over35_odds
                elif bl == "over15":
                    hit, odds = a.over15_hit, a.over15_odds
                else:
                    hit, odds = a.over25_hit, a.over25_odds
                odds = odds or 1.0
                p = (odds - 1.0) if hit else -1.0
                name = a.losing_player or "?"
                if name not in players:
                    players[name] = {"total": 0, "wins": 0, "profit": 0.0,
                                     "avg_odds": 0.0, "streak": 0, "max_dd": 0.0,
                                     "odds_sum": 0.0}
                d = players[name]
                d["total"] += 1
                d["odds_sum"] += odds
                d["profit"] += p
                if hit:
                    d["wins"] += 1
                    d["streak"] = max(0, d["streak"]) + 1
                else:
                    d["streak"] = min(0, d["streak"]) - 1
                    d["max_dd"] = min(d["max_dd"], d["streak"])

            result_list = []
            for name, d in players.items():
                if d["total"] < min_alerts:
                    continue
                result_list.append({
                    "player": name,
                    "total": d["total"],
                    "wins": d["wins"],
                    "losses": d["total"] - d["wins"],
                    "profit": round(d["profit"], 2),
                    "roi": round(d["profit"] / d["total"], 4) if d["total"] else 0.0,
                    "hit_rate": round(d["wins"] / d["total"], 4) if d["total"] else 0.0,
                    "avg_odds": round(d["odds_sum"] / d["total"], 2) if d["total"] else 0.0,
                    "worst_streak": abs(int(d["max_dd"])),
                })
            result_list.sort(key=lambda x: x["profit"], reverse=True)
            return result_list

    async def get_recent_streak(self, n: int = 20) -> Dict:
        """Return recent win/loss streak and drawdown info for last N validated alerts."""
        async with self._session() as session:
            stmt = (
                select(Alert)
                .where(Alert.validated_at.is_not(None))
                .order_by(Alert.validated_at.desc())
                .limit(n)
            )
            result = await session.execute(stmt)
            alerts = list(result.scalars().all())
            alerts.reverse()  # oldest first

            if not alerts:
                return {"streak": 0, "consecutive_losses": 0, "recent_profit": 0.0,
                        "recent_hits": 0, "recent_total": 0}

            streak = 0
            consecutive_losses = 0
            profit = 0.0
            hits = 0
            current_loss_run = 0

            for a in alerts:
                bl = a.best_line or "over25"
                if bl == "over45":
                    hit, odds = a.over45_hit, a.over45_odds
                elif bl == "over35":
                    hit, odds = a.over35_hit, a.over35_odds
                elif bl == "over15":
                    hit, odds = a.over15_hit, a.over15_odds
                else:
                    hit, odds = a.over25_hit, a.over25_odds
                odds = odds or 1.0
                p = (odds - 1.0) if hit else -1.0
                profit += p
                if hit:
                    hits += 1
                    current_loss_run = 0
                else:
                    current_loss_run += 1
                    consecutive_losses = max(consecutive_losses, current_loss_run)

            # Current streak (from most recent)
            for a in reversed(alerts):
                bl = a.best_line or "over25"
                if bl == "over45":
                    hit = a.over45_hit
                elif bl == "over35":
                    hit = a.over35_hit
                elif bl == "over15":
                    hit = a.over15_hit
                else:
                    hit = a.over25_hit
                if hit:
                    if streak >= 0:
                        streak += 1
                    else:
                        break
                else:
                    if streak <= 0:
                        streak -= 1
                    else:
                        break

            return {
                "streak": streak,
                "consecutive_losses": consecutive_losses,
                "recent_profit": round(profit, 2),
                "recent_hits": hits,
                "recent_total": len(alerts),
            }

    async def get_weekly_breakdown(self, weeks: int = 4) -> List[Dict]:
        """Return P&L breakdown by week for the last N weeks."""
        async with self._session() as session:
            cutoff = (datetime.now(timezone.utc) - timedelta(weeks=weeks)).replace(tzinfo=None)
            stmt = (
                select(Alert)
                .where(Alert.validated_at.is_not(None), Alert.sent_at >= cutoff)
                .order_by(Alert.sent_at.asc())
            )
            result = await session.execute(stmt)
            alerts = result.scalars().all()

            weekly: Dict[str, Dict] = {}
            for a in alerts:
                # ISO week label
                week_label = a.sent_at.strftime("%Y-W%W") if a.sent_at else "?"
                bl = a.best_line or "over25"
                if bl == "over45":
                    hit, odds = a.over45_hit, a.over45_odds
                elif bl == "over35":
                    hit, odds = a.over35_hit, a.over35_odds
                elif bl == "over15":
                    hit, odds = a.over15_hit, a.over15_odds
                else:
                    hit, odds = a.over25_hit, a.over25_odds
                odds = odds or 1.0
                p = (odds - 1.0) if hit else -1.0

                if week_label not in weekly:
                    weekly[week_label] = {"week": week_label, "total": 0, "wins": 0,
                                          "profit": 0.0}
                d = weekly[week_label]
                d["total"] += 1
                d["profit"] += p
                if hit:
                    d["wins"] += 1

            result_list = []
            for d in weekly.values():
                d["profit"] = round(d["profit"], 2)
                d["roi"] = round(d["profit"] / d["total"], 4) if d["total"] else 0.0
                d["hit_rate"] = round(d["wins"] / d["total"], 4) if d["total"] else 0.0
                result_list.append(d)
            return result_list


# ---------------------------------------------------------------------------
# MethodStatsRepository
# ---------------------------------------------------------------------------
class MethodStatsRepository(_BaseRepository):
    """CRUD for the method_stats table (global/per-type hit rates)."""

    async def get(self, stat_key: str) -> MethodStats | None:
        """Return existing stat row or None (read-only, no INSERT)."""
        async with self._session() as session:
            stmt = select(MethodStats).where(MethodStats.stat_key == stat_key)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def get_or_create(
        self, stat_key: str, stat_type: str = "global"
    ) -> MethodStats:
        """Return existing stat row or create one with defaults."""
        async with self._session() as session:
            stmt = select(MethodStats).where(MethodStats.stat_key == stat_key)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                try:
                    row = MethodStats(stat_key=stat_key, stat_type=stat_type)
                    session.add(row)
                    await session.flush()
                    logger.debug("Created method_stats key={} type={}", stat_key, stat_type)
                except IntegrityError:
                    await session.rollback()
                    result = await session.execute(stmt)
                    row = result.scalar_one_or_none()
            return row

    async def upsert(
        self, stat_key: str, data: Dict, stat_type: str = "global"
    ) -> MethodStats:
        """Bulk-set all fields in data dict on the stat row (create if missing)."""
        async with self._session() as session:
            stmt = select(MethodStats).where(MethodStats.stat_key == stat_key)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                try:
                    row = MethodStats(stat_key=stat_key, stat_type=stat_type)
                    session.add(row)
                    await session.flush()
                except IntegrityError:
                    await session.rollback()
                    result = await session.execute(stmt)
                    row = result.scalar_one_or_none()
            for field, value in data.items():
                if hasattr(row, field):
                    setattr(row, field, value)
            row.last_updated = datetime.now(timezone.utc).replace(tzinfo=None)
            await session.flush()
            return row

    async def update_stat(
        self,
        stat_key: str,
        *,
        over25_hit: bool,
        over35_hit: bool,
        over45_hit: bool = False,
        goals: float,
        stat_type: str = "global",
    ) -> MethodStats:
        """Increment counters for a stat row after a validation event."""
        async with self._session() as session:
            stmt = select(MethodStats).where(MethodStats.stat_key == stat_key)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                try:
                    row = MethodStats(stat_key=stat_key, stat_type=stat_type)
                    session.add(row)
                    await session.flush()
                except IntegrityError:
                    await session.rollback()
                    result = await session.execute(stmt)
                    row = result.scalar_one_or_none()

            row.total_samples += 1
            if over25_hit:
                row.over25_hits += 1
            if over35_hit:
                row.over35_hits += 1
            if over45_hit:
                row.over45_hits += 1
            row.hit_rate_25 = row.over25_hits / row.total_samples
            row.hit_rate_35 = row.over35_hits / row.total_samples
            row.hit_rate_45 = row.over45_hits / row.total_samples
            prev_total = row.avg_goals * (row.total_samples - 1)
            row.avg_goals = (prev_total + goals) / row.total_samples
            row.last_updated = datetime.now(timezone.utc).replace(tzinfo=None)
            await session.flush()
            return row

    async def get_by_keys(self, keys: list[str]) -> Sequence[MethodStats]:
        """Return all stat rows matching any of the given keys."""
        if not keys:
            return []
        async with self._session() as session:
            stmt = select(MethodStats).where(MethodStats.stat_key.in_(keys))
            result = await session.execute(stmt)
            return result.scalars().all()

    async def get_global_stats(self) -> Optional[MethodStats]:
        """Return the global stats row (stat_key='global')."""
        async with self._session() as session:
            stmt = select(MethodStats).where(MethodStats.stat_key == "global")
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def get_by_type(self, stat_type: str) -> Sequence[MethodStats]:
        """Return all stat rows of a given type."""
        async with self._session() as session:
            stmt = (
                select(MethodStats)
                .where(MethodStats.stat_type == stat_type)
                .order_by(MethodStats.stat_key)
            )
            result = await session.execute(stmt)
            return result.scalars().all()


# ---------------------------------------------------------------------------
# LeagueRepository
# ---------------------------------------------------------------------------
class LeagueRepository(_BaseRepository):
    """CRUD for the leagues configuration table."""

    async def get_active_leagues(self) -> Sequence[League]:
        """Return all leagues with is_active=True."""
        async with self._session() as session:
            stmt = select(League).where(League.is_active == True)  # noqa: E712
            result = await session.execute(stmt)
            return result.scalars().all()

    async def get_by_name(self, name: str) -> Optional[League]:
        """Return a league by its exact name."""
        async with self._session() as session:
            stmt = select(League).where(League.name == name)
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def create(self, **kwargs) -> League:
        """Create a new league record."""
        async with self._session() as session:
            league = League(**kwargs)
            session.add(league)
            await session.flush()
            return league

    async def set_active(self, league_id: int, active: bool) -> Optional[League]:
        """Toggle a league's active status."""
        async with self._session() as session:
            stmt = select(League).where(League.id == league_id)
            result = await session.execute(stmt)
            league = result.scalar_one_or_none()
            if league:
                league.is_active = active
                await session.flush()
            return league


# ---------------------------------------------------------------------------
# TeamStatsRepository
# ---------------------------------------------------------------------------
class TeamStatsRepository(_BaseRepository):
    """CRUD for team_stats and matchup_stats tables."""

    async def get_or_create(self, team_name: str) -> TeamStats:
        """Return existing team stats or create a blank row."""
        async with self._session() as session:
            stmt = select(TeamStats).where(TeamStats.team_name == team_name)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                try:
                    row = TeamStats(team_name=team_name)
                    session.add(row)
                    await session.flush()
                    logger.debug("Created team_stats for {}", team_name)
                except IntegrityError:
                    await session.rollback()
                    result = await session.execute(stmt)
                    row = result.scalar_one_or_none()
            return row

    async def save_match_team(
        self,
        match_id: int,
        player_name: str,
        team_name: str,
        side: str,
    ) -> MatchTeam:
        """Create a MatchTeam entry linking a player to their team for a match."""
        async with self._session() as session:
            stmt = select(MatchTeam).where(
                MatchTeam.match_id == match_id,
                MatchTeam.player_name == player_name,
            )
            result = await session.execute(stmt)
            entry = result.scalar_one_or_none()
            if entry is None:
                entry = MatchTeam(
                    match_id=match_id,
                    player_name=player_name,
                    team_name=team_name,
                    side=side,
                )
                session.add(entry)
                await session.flush()
            return entry

    async def update_stats(
        self,
        team_name: str,
        *,
        goals_scored: int,
        goals_conceded: int,
    ) -> TeamStats:
        """Update team stats after a match result."""
        async with self._session() as session:
            stmt = select(TeamStats).where(TeamStats.team_name == team_name)
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()
            if row is None:
                try:
                    row = TeamStats(team_name=team_name)
                    session.add(row)
                    await session.flush()
                except IntegrityError:
                    await session.rollback()
                    result = await session.execute(stmt)
                    row = result.scalar_one_or_none()

            row.total_games += 1
            row.total_goals_scored += goals_scored
            row.total_goals_conceded += goals_conceded
            row.avg_goals_scored = row.total_goals_scored / row.total_games
            row.avg_goals_conceded = row.total_goals_conceded / row.total_games

            total_match_goals = goals_scored + goals_conceded
            prev_over25_count = max(0, round(row.over25_rate * (row.total_games - 1)))
            if total_match_goals > 2:
                prev_over25_count += 1
            row.over25_rate = prev_over25_count / row.total_games
            row.last_updated = datetime.now(timezone.utc).replace(tzinfo=None)
            await session.flush()
            return row

    async def get_matchup_stats(
        self, team_a: str, team_b: str
    ) -> Optional[MatchupStats]:
        """Return matchup stats between two teams (order-independent)."""
        async with self._session() as session:
            stmt = select(MatchupStats).where(
                or_(
                    and_(MatchupStats.team_a == team_a, MatchupStats.team_b == team_b),
                    and_(MatchupStats.team_a == team_b, MatchupStats.team_b == team_a),
                )
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def update_matchup_stats(
        self, team_a: str, team_b: str, total_goals: int
    ) -> MatchupStats:
        """Update (or create) matchup stats after a game."""
        async with self._session() as session:
            a, b = sorted([team_a, team_b])
            stmt = select(MatchupStats).where(
                MatchupStats.team_a == a, MatchupStats.team_b == b
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()

            if row is None:
                row = MatchupStats(team_a=a, team_b=b)
                session.add(row)
                await session.flush()

            row.total_games += 1
            prev_total = row.avg_total_goals * (row.total_games - 1)
            row.avg_total_goals = (prev_total + total_goals) / row.total_games

            prev_over25_count = max(0, round(row.over25_rate * (row.total_games - 1)))
            if total_goals > 2:
                prev_over25_count += 1
            row.over25_rate = prev_over25_count / row.total_games
            row.last_updated = datetime.now(timezone.utc).replace(tzinfo=None)
            await session.flush()
            return row

    async def get_player_team_preference(
        self, player_name: str, team_name: str
    ) -> Optional[PlayerTeamPreference]:
        """Return a specific player-team preference row."""
        async with self._session() as session:
            stmt = select(PlayerTeamPreference).where(
                PlayerTeamPreference.player_name == player_name,
                PlayerTeamPreference.team_name == team_name,
            )
            result = await session.execute(stmt)
            return result.scalar_one_or_none()

    async def update_player_team_preference(
        self,
        player_name: str,
        team_name: str,
        goals_scored: int,
    ) -> PlayerTeamPreference:
        """Update (or create) a player's team preference after a game."""
        async with self._session() as session:
            stmt = select(PlayerTeamPreference).where(
                PlayerTeamPreference.player_name == player_name,
                PlayerTeamPreference.team_name == team_name,
            )
            result = await session.execute(stmt)
            row = result.scalar_one_or_none()

            if row is None:
                row = PlayerTeamPreference(
                    player_name=player_name, team_name=team_name
                )
                session.add(row)
                await session.flush()

            row.times_used += 1
            row.goals_scored_with += goals_scored
            row.avg_goals_with = row.goals_scored_with / row.times_used

            total_games_stmt = select(
                func.sum(PlayerTeamPreference.times_used)
            ).where(PlayerTeamPreference.player_name == player_name)
            total_result = await session.execute(total_games_stmt)
            total_games = total_result.scalar() or 0

            if total_games > 0:
                row.is_main_team = (row.times_used / total_games) > 0.5

            if row.is_main_team:
                await session.execute(
                    update(PlayerTeamPreference)
                    .where(
                        PlayerTeamPreference.player_name == player_name,
                        PlayerTeamPreference.team_name != team_name,
                    )
                    .values(is_main_team=False)
                )

            await session.flush()
            return row
