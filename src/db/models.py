"""
SQLAlchemy 2.0 ORM models for FIFA Bet Alert.

Every table matches the PRD schema exactly, with additional SQLAlchemy
relationships and indexes for frequently queried columns.
"""

from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Player
# ---------------------------------------------------------------------------
class Player(Base):
    __tablename__ = "players"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)

    # Contadores gerais
    total_games: Mapped[int] = mapped_column(Integer, default=0)
    total_losses: Mapped[int] = mapped_column(Integer, default=0)
    total_return_matches: Mapped[int] = mapped_column(Integer, default=0)

    # Over stats apos derrota
    over15_after_loss: Mapped[int] = mapped_column(Integer, default=0)
    over25_after_loss: Mapped[int] = mapped_column(Integer, default=0)
    over35_after_loss: Mapped[int] = mapped_column(Integer, default=0)
    hit_rate_15: Mapped[float] = mapped_column(Float, default=0.0)
    hit_rate_25: Mapped[float] = mapped_column(Float, default=0.0)
    hit_rate_35: Mapped[float] = mapped_column(Float, default=0.0)
    avg_goals_after_loss: Mapped[float] = mapped_column(Float, default=0.0)

    # Stats por tipo de derrota
    tight_loss_count: Mapped[int] = mapped_column(Integer, default=0)
    tight_loss_over25: Mapped[int] = mapped_column(Integer, default=0)
    medium_loss_count: Mapped[int] = mapped_column(Integer, default=0)
    medium_loss_over25: Mapped[int] = mapped_column(Integer, default=0)
    blowout_loss_count: Mapped[int] = mapped_column(Integer, default=0)
    blowout_loss_over25: Mapped[int] = mapped_column(Integer, default=0)

    # Confiabilidade
    is_reliable: Mapped[bool] = mapped_column(Boolean, default=False)
    reliability_score: Mapped[float] = mapped_column(Float, default=0.0)

    # Metadata
    last_seen: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )

    # Relationships
    team_preferences: Mapped[List["PlayerTeamPreference"]] = relationship(
        "PlayerTeamPreference",
        primaryjoin="Player.name == foreign(PlayerTeamPreference.player_name)",
        back_populates="player",
        lazy="selectin",
        viewonly=True,
    )

    __table_args__ = (
        Index("ix_players_name", "name"),
        Index("ix_players_is_reliable", "is_reliable"),
    )

    def __repr__(self) -> str:
        return f"<Player(id={self.id}, name={self.name!r})>"


# ---------------------------------------------------------------------------
# Match
# ---------------------------------------------------------------------------
class Match(Base):
    __tablename__ = "matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    api_event_id: Mapped[Optional[str]] = mapped_column(
        String, unique=True, nullable=True
    )
    league: Mapped[str] = mapped_column(String, nullable=False)
    player_home: Mapped[str] = mapped_column(String, nullable=False)
    player_away: Mapped[str] = mapped_column(String, nullable=False)
    team_home: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    team_away: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    score_home: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    score_away: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    score_home_ht: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    score_away_ht: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Stats extras
    corners_home: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    corners_away: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    shots_home: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    shots_away: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    possession_home: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    possession_away: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cards_home: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    cards_away: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Contexto
    status: Mapped[str] = mapped_column(String, default="scheduled")
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    ended_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    day_of_week: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    hour_of_day: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Vinculo ida/volta
    pair_match_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("matches.id"), nullable=True
    )
    is_return_match: Mapped[bool] = mapped_column(Boolean, default=False)
    time_between_games: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )

    # Relationships
    pair_match: Mapped[Optional["Match"]] = relationship(
        "Match", remote_side="Match.id", foreign_keys=[pair_match_id], lazy="selectin"
    )
    odds_history: Mapped[List["OddsHistory"]] = relationship(
        back_populates="match", lazy="selectin"
    )
    alerts: Mapped[List["Alert"]] = relationship(
        back_populates="match", lazy="selectin"
    )
    match_teams: Mapped[List["MatchTeam"]] = relationship(
        back_populates="match", lazy="selectin"
    )

    __table_args__ = (
        Index("ix_matches_api_event_id", "api_event_id"),
        Index("ix_matches_status", "status"),
        Index("ix_matches_league", "league"),
        Index("ix_matches_player_home", "player_home"),
        Index("ix_matches_player_away", "player_away"),
        Index("ix_matches_pair_match_id", "pair_match_id"),
        Index("ix_matches_ended_at", "ended_at"),
        Index("ix_matches_is_return_match", "is_return_match"),
    )

    def __repr__(self) -> str:
        return (
            f"<Match(id={self.id}, {self.player_home} vs {self.player_away}, "
            f"status={self.status!r})>"
        )


# ---------------------------------------------------------------------------
# OddsHistory
# ---------------------------------------------------------------------------
class OddsHistory(Base):
    __tablename__ = "odds_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("matches.id"), nullable=False
    )
    player: Mapped[str] = mapped_column(String, nullable=False)
    market: Mapped[str] = mapped_column(String, nullable=False)
    odds_value: Mapped[float] = mapped_column(Float, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )

    # Relationships
    match: Mapped["Match"] = relationship(back_populates="odds_history")

    __table_args__ = (
        Index("ix_odds_history_match_id", "match_id"),
        Index("ix_odds_history_player", "player"),
        Index("ix_odds_history_market", "market"),
        Index("ix_odds_history_captured_at", "captured_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<OddsHistory(match_id={self.match_id}, player={self.player!r}, "
            f"market={self.market!r}, odds={self.odds_value})>"
        )


# ---------------------------------------------------------------------------
# Alert
# ---------------------------------------------------------------------------
class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("matches.id"), nullable=False
    )
    losing_player: Mapped[str] = mapped_column(String, nullable=False)
    game1_score: Mapped[str] = mapped_column(String, nullable=False)
    loss_margin: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    loss_type: Mapped[Optional[str]] = mapped_column(String, nullable=True)

    # Odds no momento do alerta
    best_line: Mapped[Optional[str]] = mapped_column(String, nullable=True)   # over25/over35/over45
    over25_odds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    over35_odds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    over45_odds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    over15_odds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Motor estatistico
    implied_prob: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    true_prob: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    true_prob_conservative: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    edge: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    expected_value: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    kelly_fraction: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    star_rating: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Camadas de probabilidade
    p_base: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    p_loss_type: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    p_player: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    p_recent_form: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    p_h2h: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    p_y_post_win: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    p_time_slot: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    p_market_adj: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    player_sample_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    h2h_sample_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    recent_form_sample: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    global_sample_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    loss_type_sample_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    team_sample_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    confidence_interval_low: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )
    confidence_interval_high: Mapped[Optional[float]] = mapped_column(
        Float, nullable=True
    )

    # Metadata
    alert_level: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )

    # Novos campos: gols do perdedor em G1, streak, probabilidade G1 goals
    loser_goals_g1: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    streak: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    p_g1_goals: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Telegram message ID (para editar apos resultado)
    telegram_message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Resultado pos-jogo
    actual_goals: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    over25_hit: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    over35_hit: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    over45_hit: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    validated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Moneyline (vitória do perdedor em G2)
    ml_odds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    ml_hit: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    # P&L tracking
    profit_flat: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    over15_hit: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    # Relationships
    match: Mapped["Match"] = relationship(back_populates="alerts")

    __table_args__ = (
        Index("ix_alerts_match_id", "match_id"),
        Index("ix_alerts_losing_player", "losing_player"),
        Index("ix_alerts_star_rating", "star_rating"),
        Index("ix_alerts_sent_at", "sent_at"),
        Index("ix_alerts_validated_at", "validated_at"),
        Index("ix_alerts_alert_level", "alert_level"),
    )

    def __repr__(self) -> str:
        return (
            f"<Alert(id={self.id}, match_id={self.match_id}, "
            f"loser={self.losing_player!r}, stars={self.star_rating})>"
        )


# ---------------------------------------------------------------------------
# League
# ---------------------------------------------------------------------------
class League(Base):
    __tablename__ = "leagues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    api_league_id: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    poll_interval_seconds: Mapped[int] = mapped_column(Integer, default=30)
    return_match_delay_minutes: Mapped[int] = mapped_column(Integer, default=55)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_leagues_name", "name"),
        Index("ix_leagues_is_active", "is_active"),
    )

    def __repr__(self) -> str:
        return f"<League(id={self.id}, name={self.name!r}, active={self.is_active})>"


# ---------------------------------------------------------------------------
# MethodStats
# ---------------------------------------------------------------------------
class MethodStats(Base):
    __tablename__ = "method_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    stat_key: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    stat_type: Mapped[str] = mapped_column(String, nullable=False)
    total_samples: Mapped[int] = mapped_column(Integer, default=0)
    over15_hits: Mapped[int] = mapped_column(Integer, default=0)
    over25_hits: Mapped[int] = mapped_column(Integer, default=0)
    over35_hits: Mapped[int] = mapped_column(Integer, default=0)
    over45_hits: Mapped[int] = mapped_column(Integer, default=0)
    hit_rate_15: Mapped[float] = mapped_column(Float, default=0.0)
    hit_rate_25: Mapped[float] = mapped_column(Float, default=0.0)
    hit_rate_35: Mapped[float] = mapped_column(Float, default=0.0)
    hit_rate_45: Mapped[float] = mapped_column(Float, default=0.0)
    avg_goals: Mapped[float] = mapped_column(Float, default=0.0)
    last_updated: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_method_stats_stat_key", "stat_key"),
        Index("ix_method_stats_stat_type", "stat_type"),
    )

    def __repr__(self) -> str:
        return (
            f"<MethodStats(key={self.stat_key!r}, type={self.stat_type!r}, "
            f"samples={self.total_samples})>"
        )


# ---------------------------------------------------------------------------
# RegimeCheck
# ---------------------------------------------------------------------------
class RegimeCheck(Base):
    __tablename__ = "regime_checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    window_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    recent_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    historical_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    z_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    status: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    action_taken: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_regime_checks_checked_at", "checked_at"),
        Index("ix_regime_checks_status", "status"),
    )

    def __repr__(self) -> str:
        return (
            f"<RegimeCheck(id={self.id}, status={self.status!r}, "
            f"z={self.z_score})>"
        )


# ---------------------------------------------------------------------------
# RoiSimulation
# ---------------------------------------------------------------------------
class RoiSimulation(Base):
    __tablename__ = "roi_simulations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    strategy_name: Mapped[str] = mapped_column(String, nullable=False)
    simulated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )
    total_bets: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    wins: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    losses: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    win_rate: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    roi: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    profit_units: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    max_drawdown: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    __table_args__ = (
        Index("ix_roi_simulations_strategy_name", "strategy_name"),
    )

    def __repr__(self) -> str:
        return (
            f"<RoiSimulation(strategy={self.strategy_name!r}, "
            f"roi={self.roi}, bets={self.total_bets})>"
        )


# ---------------------------------------------------------------------------
# MatchTeam
# ---------------------------------------------------------------------------
class MatchTeam(Base):
    __tablename__ = "match_teams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("matches.id"), nullable=False
    )
    player_name: Mapped[str] = mapped_column(String, nullable=False)
    team_name: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    side: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now()
    )

    # Relationships
    match: Mapped["Match"] = relationship(back_populates="match_teams")

    __table_args__ = (
        Index("ix_match_teams_match_id", "match_id"),
        Index("ix_match_teams_player_name", "player_name"),
        Index("ix_match_teams_team_name", "team_name"),
    )

    def __repr__(self) -> str:
        return (
            f"<MatchTeam(match_id={self.match_id}, player={self.player_name!r}, "
            f"team={self.team_name!r})>"
        )


# ---------------------------------------------------------------------------
# TeamStats
# ---------------------------------------------------------------------------
class TeamStats(Base):
    __tablename__ = "team_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    team_name: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    total_games: Mapped[int] = mapped_column(Integer, default=0)
    total_goals_scored: Mapped[int] = mapped_column(Integer, default=0)
    total_goals_conceded: Mapped[int] = mapped_column(Integer, default=0)
    avg_goals_scored: Mapped[float] = mapped_column(Float, default=0.0)
    avg_goals_conceded: Mapped[float] = mapped_column(Float, default=0.0)
    over25_rate: Mapped[float] = mapped_column(Float, default=0.0)
    last_updated: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        Index("ix_team_stats_team_name", "team_name"),
    )

    def __repr__(self) -> str:
        return (
            f"<TeamStats(team={self.team_name!r}, games={self.total_games})>"
        )


# ---------------------------------------------------------------------------
# MatchupStats
# ---------------------------------------------------------------------------
class MatchupStats(Base):
    __tablename__ = "matchup_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    team_a: Mapped[str] = mapped_column(String, nullable=False)
    team_b: Mapped[str] = mapped_column(String, nullable=False)
    total_games: Mapped[int] = mapped_column(Integer, default=0)
    avg_total_goals: Mapped[float] = mapped_column(Float, default=0.0)
    over25_rate: Mapped[float] = mapped_column(Float, default=0.0)
    last_updated: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("team_a", "team_b", name="uq_matchup_teams"),
        Index("ix_matchup_stats_team_a", "team_a"),
        Index("ix_matchup_stats_team_b", "team_b"),
    )

    def __repr__(self) -> str:
        return (
            f"<MatchupStats(team_a={self.team_a!r}, team_b={self.team_b!r}, "
            f"games={self.total_games})>"
        )


# ---------------------------------------------------------------------------
# PlayerTeamPreference
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# AlertV2 (Method 2)
# ---------------------------------------------------------------------------
class AlertV2(Base):
    __tablename__ = "alerts_v2"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    match_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("matches.id"), nullable=False
    )
    losing_player: Mapped[str] = mapped_column(String, nullable=False)
    opponent_player: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    game1_score: Mapped[str] = mapped_column(String, nullable=False)
    camada: Mapped[str] = mapped_column(String, nullable=False)  # C1a, C1b, C2

    best_line: Mapped[Optional[str]] = mapped_column(String, nullable=True)
    over15_odds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    over25_odds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    over35_odds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    over45_odds: Mapped[Optional[float]] = mapped_column(Float, nullable=True)

    # Stats da avaliacao
    prob: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sample_size: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    prob_4elem: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    prob_3elem: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    sample_4elem: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    sample_3elem: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Telegram
    telegram_message_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    # Resultado pos-jogo
    actual_goals: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    hit: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    profit_flat: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    validated_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    # Relationships
    match: Mapped["Match"] = relationship("Match", foreign_keys=[match_id])

    __table_args__ = (
        Index("ix_alerts_v2_match_id", "match_id"),
        Index("ix_alerts_v2_losing_player", "losing_player"),
        Index("ix_alerts_v2_camada", "camada"),
        Index("ix_alerts_v2_sent_at", "sent_at"),
        Index("ix_alerts_v2_validated_at", "validated_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<AlertV2(id={self.id}, match_id={self.match_id}, "
            f"loser={self.losing_player!r}, camada={self.camada!r})>"
        )


class PlayerTeamPreference(Base):
    __tablename__ = "player_team_preferences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    player_name: Mapped[str] = mapped_column(String, nullable=False)
    team_name: Mapped[str] = mapped_column(String, nullable=False)
    times_used: Mapped[int] = mapped_column(Integer, default=0)
    goals_scored_with: Mapped[int] = mapped_column(Integer, default=0)
    avg_goals_with: Mapped[float] = mapped_column(Float, default=0.0)
    is_main_team: Mapped[bool] = mapped_column(Boolean, default=False)

    # Relationship back to Player (via player_name, not FK to keep schema aligned with PRD)
    player: Mapped[Optional["Player"]] = relationship(
        "Player",
        primaryjoin="foreign(PlayerTeamPreference.player_name) == Player.name",
        back_populates="team_preferences",
        viewonly=True,
    )

    __table_args__ = (
        UniqueConstraint("player_name", "team_name", name="uq_player_team"),
        Index("ix_player_team_pref_player_name", "player_name"),
        Index("ix_player_team_pref_team_name", "team_name"),
    )

    def __repr__(self) -> str:
        return (
            f"<PlayerTeamPreference(player={self.player_name!r}, "
            f"team={self.team_name!r}, used={self.times_used})>"
        )
