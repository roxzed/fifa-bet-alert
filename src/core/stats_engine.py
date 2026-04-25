"""Statistical engine: calculates true probability, edge, EV, and regime status.

IMPORTANTE: todas as probabilidades sao para GOLS INDIVIDUAIS DO JOGADOR,
nao gols totais do jogo. O mercado na bet365 e "Time (Jogador) - Gols"
com linhas Over 2.5, 3.5, 4.5 gols do jogador especifico.

Layers de probabilidade:
  1. Global base (taxa de over do perdedor individual)
  2. Loss type (tight/medium/blowout)
  3. Player geral (min 30 amostras)
  4. Player forma recente (ultimas 20 partidas, min 10)
  5. H2H player: X vs Y especificamente (min 15)
  6. Y pos-vitoria: Y tende a ceder apos ganhar G1
  7. Player+time (min 15)
  8. Time slot (hora do dia)
  9. Market movement adjustment
  10. Gols do perdedor em G1
  11. Total de gols em G1
  12. Padrao do jogo G1
  13. Blacklist/Elite de jogadores

Multi-line: avalia over 2.5, 3.5 e 4.5 gols do jogador e alerta na melhor EV.

Melhorias aplicadas (v2 — 2026-03-25):
- MELHORIA 1: Consulta única com IN — todas as chaves de method_stats são
  buscadas em um único SELECT, eliminando 8+ round-trips ao banco.
- MELHORIA 2: Cache TTL em memória — resultados do method_stats são cacheados
  por 5 minutos, reduzindo I/O do banco drasticamente.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from loguru import logger

from src.config import settings
from src.core.probability import (
    bayesian_update,
    calculate_true_probability,
    detect_regime_change,
    edge,
    expected_value,
    fractional_kelly,
    implied_probability,
    should_alert,
    simulate_roi,
    star_rating,
    wilson_confidence_interval,
)


# ---------------------------------------------------------------------------
# Cache TTL em memória para method_stats (MELHORIA 2)
# ---------------------------------------------------------------------------

class _StatsCache:
    """Simple in-memory cache with TTL for method_stats rows.

    Stores stat objects by stat_key with a configurable TTL (default 5 min).
    Thread-safe for asyncio (single-threaded event loop).
    """

    def __init__(self, ttl_seconds: float = 300.0):
        self._ttl = ttl_seconds
        self._store: dict[str, tuple[float, object]] = {}  # key -> (expires_at, stat)
        self._bulk_expires: float = 0.0  # when the last bulk fetch expires

    def get(self, key: str):
        """Get a cached stat, or None if expired/missing."""
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, stat = entry
        if time.monotonic() > expires_at:
            del self._store[key]
            return None
        return stat

    def put(self, key: str, stat) -> None:
        """Cache a stat with TTL."""
        self._store[key] = (time.monotonic() + self._ttl, stat)

    def put_bulk(self, stats: dict[str, object]) -> None:
        """Cache multiple stats at once (from bulk fetch)."""
        expires = time.monotonic() + self._ttl
        for key, stat in stats.items():
            self._store[key] = (expires, stat)
        self._bulk_expires = expires

    def has_all(self, keys: list[str]) -> bool:
        """Check if all keys are cached and not expired."""
        now = time.monotonic()
        for k in keys:
            entry = self._store.get(k)
            if entry is None or now > entry[0]:
                return False
        return True

    def get_many(self, keys: list[str]) -> dict[str, object]:
        """Get multiple cached stats. Returns only valid (non-expired) entries."""
        now = time.monotonic()
        result = {}
        for k in keys:
            entry = self._store.get(k)
            if entry and now <= entry[0]:
                result[k] = entry[1]
        return result

    def invalidate(self, key: str) -> None:
        """Remove a specific key from cache."""
        self._store.pop(key, None)

    def clear(self) -> None:
        """Clear entire cache."""
        self._store.clear()


@dataclass
class LineEvaluation:
    """Evaluation for a single over/under line."""
    line: str            # "over25", "over35", "over45"
    odds: float
    true_prob: float
    true_prob_conservative: float
    implied_prob: float
    edge_val: float
    ev_val: float
    kelly_val: float
    stars: int
    should_alert: bool
    reason: str


@dataclass
class OpportunityEvaluation:
    """Full statistical evaluation of a betting opportunity."""
    # Decision (best line)
    should_alert: bool
    reason: str
    best_line: str          # "over25", "over35", "over45"

    # Probabilities (best line)
    implied_prob: float
    true_prob: float
    true_prob_conservative: float
    confidence_interval: tuple[float, float]

    # Value (best line)
    edge_val: float
    expected_value_val: float
    kelly_fraction_val: float
    star_rating_val: int

    # Probability layers (for best line)
    p_base: float
    p_loss_type: float
    p_player: float
    p_recent_form: float      # forma recente (ultimas 20)
    p_h2h: float              # H2H: taxa do perdedor vs esse oponente
    p_y_post_win: float       # Y tende a ceder apos ganhar G1
    p_time_slot: float
    p_team: float
    p_market_adj: float

    # Sample sizes
    player_sample_size: int
    h2h_sample_size: int
    recent_form_sample: int
    global_sample_size: int
    loss_type_sample_size: int
    team_sample_size: int

    # Context
    loss_type: str
    loss_margin: int

    # All line evaluations (for transparency)
    line_over15: LineEvaluation | None = None
    line_over25: LineEvaluation | None = None
    line_over35: LineEvaluation | None = None
    line_over45: LineEvaluation | None = None
    line_ml: LineEvaluation | None = None

    # Novos campos: gols do perdedor em G1 e streak
    p_g1_goals: float = 0.0
    loser_goals_g1: int = 0
    streak: int = 0
    streak_factor: float = 1.0

    # Novos layers v2: total G1, game pattern, player flag
    total_g1_goals: int = 0
    total_g1_factor: float = 1.0
    game_pattern: str = ""
    game_pattern_factor: float = 1.0
    player_flag: str = ""  # "blacklist", "elite", ""

    # Layers v3: H2H-validated (gols loser G1, total G1, hora, home/away)
    loser_g1_cat_factor: float = 1.0
    total_g1_cat_factor: float = 1.0
    hour_period_factor: float = 1.0
    home_away_g1_factor: float = 1.0


@dataclass
class RegimeCheck:
    status: str
    recent_rate: float
    historical_rate: float
    z_score: float
    message: str
    action: str


@dataclass
class ColdStartProgress:
    days_collected: int
    cold_start_days: int
    total_games: int
    total_pairs: int
    unique_players: int
    unique_teams: int
    is_complete: bool
    activation_date: str
    alerts_today: int = 0
    monitored_return_matches: int = 0
    pending_pairs: int = 0


class StatsEngine:
    """
    Core statistical engine. Combines historical data with probability
    calculations to decide when to alert.

    Melhorias v2:
    - Consulta única: _fetch_stats_bulk() busca todas as chaves necessárias
      em um único SELECT ... WHERE stat_key IN (...).
    - Cache TTL: resultados são cacheados por 5 min para evitar queries repetidas.
    """

    # Blacklist dinâmica: jogadores com WR < 30% em 8+ alertas validados
    DYNAMIC_BLACKLIST_MIN_ALERTS: int = 8
    DYNAMIC_BLACKLIST_MAX_WR: float = 0.30

    # Jogadores isentos da blacklist dinamica — gerenciados pelo auto-block per (player, line).
    # Decidido 2026-04-25: hotShot e Kavviro saem do conditional+dynamic para serem
    # avaliados pelo auto-block (mais cirurgico, separa por linha). Se tomarem -3u
    # numa linha especifica, auto-block bloqueia automaticamente.
    DYNAMIC_BLACKLIST_EXEMPT: set[str] = {"hotShot", "Kavviro"}

    # Filtro bad_hour removido 2026-04-21: overfit na janela 05-14 Abr.
    # Validacao out-of-sample (3 janelas) mostrou que sem filtro produz P/L
    # agregado melhor (+7u nos 7d pos-deploy).

    # tight AWAY: bloqueio total (WR=39.5%, ROI=-24.9%, 43 tips, -10.71u)
    # tight HOME: permitido (WR=52.9%, ROI=-0.2%, neutro)
    # Nota: filtro AWAY edge extra removido — blacklist + tight_away ja limpam os piores

    def __init__(
        self,
        match_repo,
        player_repo,
        alert_repo,
        method_stats_repo,
        team_stats_repo,
    ) -> None:
        self.matches = match_repo
        self.players = player_repo
        self.alerts = alert_repo
        self.method_stats = method_stats_repo
        self.team_stats = team_stats_repo
        # MELHORIA 2: Cache em memória com TTL de 5 minutos
        self._cache = _StatsCache(ttl_seconds=300.0)
        # Cache da blacklist dinâmica (TTL 15 min para não consultar DB toda vez)
        self._dynamic_blacklist: set[str] = set()
        self._dynamic_blacklist_expires: float = 0.0
        # Cache de jogadores com histórico positivo (para filtro de horário)
        self._positive_players: set[str] = set()
        self._positive_players_expires: float = 0.0

    # ------------------------------------------------------------------
    # Blacklist dinâmica e jogadores positivos (cache com TTL)
    # ------------------------------------------------------------------

    async def _refresh_dynamic_blacklist(self) -> None:
        """Atualiza blacklist dinâmica consultando performance real dos jogadores.

        Jogadores com WR < 30% em 8+ alertas validados são bloqueados.
        Cache de 15 minutos para não sobrecarregar o banco.
        """
        now = time.monotonic()
        if now < self._dynamic_blacklist_expires:
            return  # cache ainda válido

        try:
            perf = await self.alerts.get_player_performance(days=30, min_alerts=1)
            blacklisted = set()
            positive = set()
            for p in perf:
                total = p["total"]
                hr = p["hit_rate"]
                if p["player"] in self.DYNAMIC_BLACKLIST_EXEMPT:
                    logger.debug(
                        f"Dynamic blacklist EXEMPT: {p['player']} gerenciado pelo auto-block"
                    )
                elif total >= self.DYNAMIC_BLACKLIST_MIN_ALERTS and hr < self.DYNAMIC_BLACKLIST_MAX_WR:
                    blacklisted.add(p["player"])
                    logger.info(
                        f"Blacklist dinâmica: {p['player']} bloqueado "
                        f"(WR={hr:.1%}, {total} alertas, P/L={p['profit']:+.2f})"
                    )
                # Jogador positivo: 5+ alertas com WR >= 50% e profit > 0
                if total >= 5 and hr >= 0.50 and p["profit"] > 0:
                    positive.add(p["player"])

            self._dynamic_blacklist = blacklisted
            self._positive_players = positive
            self._dynamic_blacklist_expires = now + 900.0  # 15 min TTL
            self._positive_players_expires = now + 900.0
            logger.debug(
                f"Blacklist dinâmica atualizada: {len(blacklisted)} bloqueados, "
                f"{len(positive)} positivos"
            )
        except Exception as e:
            logger.warning(f"Erro ao atualizar blacklist dinâmica: {e}")
            # Manter cache anterior em caso de erro
            self._dynamic_blacklist_expires = now + 60.0  # retry em 1 min

    def is_dynamically_blacklisted(self, player: str) -> bool:
        """Verifica se jogador está na blacklist dinâmica (cache)."""
        return player in self._dynamic_blacklist

    def is_positive_player(self, player: str) -> bool:
        """Verifica se jogador tem histórico positivo recente."""
        return player in self._positive_players

    def is_conditionally_blacklisted(
        self, player: str, line_name: str, loser_was_home_g1: bool | None
    ) -> tuple[bool, str]:
        """Verifica blacklist condicional (jogador bloqueado em contextos específicos).

        Em Esoccer Battle, o loser mantém o mesmo lado (home/away) em G1 e G2.
        Portanto loser_was_home_g1=True significa loser é HOME em G2 também.

        Returns (is_blocked, reason).
        """
        cond = self.PLAYER_CONDITIONAL_BLACKLIST.get(player)
        if cond is None:
            return False, ""

        # Hard block (SKIP): bloqueia em qualquer linha/lado
        if cond.get("block_all"):
            return True, f"{player} bloqueado (SKIP)"

        # Check home/away block (G2 side = same as G1 side in Esoccer)
        if loser_was_home_g1 is not None:
            if cond.get("block_home_g2") and loser_was_home_g1:
                return True, f"{player} bloqueado HOME em G2"
            if cond.get("block_away_g2") and not loser_was_home_g1:
                return True, f"{player} bloqueado AWAY em G2"

        # Check line block
        blocked_lines = cond.get("block_lines")
        if blocked_lines and line_name in blocked_lines:
            return True, f"{player} bloqueado em {line_name}"

        return False, ""

    # ------------------------------------------------------------------
    # MELHORIA 1: Consulta única com IN
    # ------------------------------------------------------------------

    async def _fetch_stats_bulk(self, keys: list[str]) -> dict[str, object]:
        """Fetch multiple method_stats rows in a single SELECT ... WHERE IN.

        First checks the in-memory cache. Only queries the DB for missing keys.
        Results are cached for future calls.

        Returns a dict of stat_key -> stat object (or None for missing keys).
        """
        # Check cache first
        cached = self._cache.get_many(keys)
        missing_keys = [k for k in keys if k not in cached]

        if not missing_keys:
            return cached

        # Single bulk query for all missing keys
        try:
            rows = await self.method_stats.get_by_keys(missing_keys)

            fetched = {}
            for row in rows:
                fetched[row.stat_key] = row
                self._cache.put(row.stat_key, row)

            # Cache None for keys not found (avoid re-querying)
            for k in missing_keys:
                if k not in fetched:
                    self._cache.put(k, None)
                    fetched[k] = None

            # Merge cached + fetched
            cached.update(fetched)

        except Exception as e:
            logger.warning(f"Bulk stats fetch failed: {e}")
            # Fallback: return cached + None for missing
            for k in missing_keys:
                cached[k] = None

        return cached

    def _extract_stat(
        self, stats: dict, key: str, default_15: float = 0.75,
        default_25: float = 0.50, default_35: float = 0.35,
        default_45: float = 0.20,
    ) -> tuple[float, float, float, float, int]:
        """Extract hit rates (O1.5, O2.5, O3.5, O4.5) from a stat object."""
        stat = stats.get(key)
        if stat is None or stat.total_samples == 0:
            return (default_15, default_25, default_35, default_45, 0)
        hr15 = getattr(stat, "hit_rate_15", 0.0) or 0.0
        # Fallback: se hit_rate_15 nao populado ainda, estimar do over15_hits
        if hr15 == 0.0 and stat.total_samples > 0:
            o15_hits = getattr(stat, "over15_hits", 0) or 0
            if o15_hits > 0:
                hr15 = o15_hits / stat.total_samples
            else:
                # Ultimo fallback: ratio global O1.5/O2.5
                hr15 = min(0.95, stat.hit_rate_25 * 1.47)
        return (hr15, stat.hit_rate_25, stat.hit_rate_35, stat.hit_rate_45, stat.total_samples)

    # ------------------------------------------------------------------
    # Pre-warm cache (chamado antes do kickoff para evitar delay no alerta)
    # ------------------------------------------------------------------

    async def pre_warm_cache(self, losing_player: str, opponent_player: str) -> None:
        """Pré-aquece o cache buscando todas as stats necessárias antes do kickoff."""
        keys = [
            "global",
            "loss_tight", "loss_medium", "loss_blowout",
            f"player_general_{losing_player}",
            f"recent_form_{losing_player}",
            f"h2h_{losing_player}_vs_{opponent_player}",
            f"y_post_win_{opponent_player}",
            f"ml_player_{losing_player}",
            f"ml_h2h_{losing_player}_vs_{opponent_player}",
            f"ml_cedente_{opponent_player}",
        ]
        # Adicionar slots de tempo comuns
        for base in range(0, 24, 6):
            keys.append(f"time_{base:02d}-{base + 6:02d}h")
        await self._fetch_stats_bulk(keys)
        logger.debug(f"Cache pre-warmed for {losing_player} vs {opponent_player} ({len(keys)} keys)")

    # ------------------------------------------------------------------
    # Main evaluation
    # ------------------------------------------------------------------

    async def evaluate_opportunity(
        self,
        losing_player: str,
        opponent_player: str | None,
        game1_score_winner: int,
        game1_score_loser: int,
        over25_odds: float,
        over35_odds: float | None,
        over45_odds: float | None = None,
        over15_odds: float | None = None,
        ml_odds: float | None = None,
        match_time: datetime | None = None,
        loser_team: str | None = None,
        opponent_team: str | None = None,
        odds_history: list | None = None,
        loser_goals_g1: int = 0,
        loser_was_home_g1: bool | None = None,
    ) -> OpportunityEvaluation:
        """Full statistical evaluation for a betting opportunity.

        Evaluates over 2.5, 3.5 and 4.5 lines and returns evaluation
        anchored on the line with the best expected value.
        """
        from src.core.probability import classify_loss

        if match_time is None:
            match_time = datetime.now(timezone.utc)

        # ── Blacklist dinâmica: atualiza cache e bloqueia jogadores ruins ──
        await self._refresh_dynamic_blacklist()
        if self.is_dynamically_blacklisted(losing_player):
            logger.info(
                f"Jogador {losing_player} na blacklist dinâmica: bloqueado"
            )
            loss_type = classify_loss(game1_score_winner, game1_score_loser)
            return OpportunityEvaluation(
                should_alert=False,
                reason=f"Blacklist dinâmica: {losing_player} WR<30% em 8+ alertas",
                best_line="over25",
                implied_prob=0.0,
                true_prob=0.0,
                true_prob_conservative=0.0,
                confidence_interval=(0.0, 1.0),
                edge_val=0.0,
                expected_value_val=0.0,
                kelly_fraction_val=0.0,
                star_rating_val=0,
                p_base=0.0, p_loss_type=0.0, p_player=0.0,
                p_recent_form=0.0, p_h2h=0.0, p_y_post_win=0.0,
                p_time_slot=0.0, p_team=0.0, p_market_adj=0.0,
                player_sample_size=0, h2h_sample_size=0,
                recent_form_sample=0, global_sample_size=0,
                loss_type_sample_size=0, team_sample_size=0,
                loss_type=loss_type,
                loss_margin=game1_score_winner - game1_score_loser,
                player_flag="blacklist",
            )

        loss_type = classify_loss(game1_score_winner, game1_score_loser)
        loss_margin = game1_score_winner - game1_score_loser

        # ── MELHORIA 1: Montar todas as chaves e buscar de uma vez ──────
        hour = match_time.hour
        slot = f"{(hour // 6) * 6:02d}-{(hour // 6) * 6 + 6:02d}h"

        stat_keys = [
            "global",
            f"loss_{loss_type}",
            f"player_general_{losing_player}",
            f"recent_form_{losing_player}",
            f"time_{slot}",
            # ML keys
            f"ml_player_{losing_player}",
        ]
        if opponent_player:
            stat_keys.append(f"h2h_{losing_player}_vs_{opponent_player}")
            stat_keys.append(f"y_post_win_{opponent_player}")
            # ML H2H and cedente
            stat_keys.append(f"ml_h2h_{losing_player}_vs_{opponent_player}")
            stat_keys.append(f"ml_cedente_{opponent_player}")

        # Busca única — 1 SELECT em vez de 8+
        all_stats = await self._fetch_stats_bulk(stat_keys)

        # ── Extrair probabilidades de cada layer ────────────────────────
        p_base_15, p_base_25, p_base_35, p_base_45, n_global = self._extract_stat(all_stats, "global")
        p_loss_15, p_loss_25, p_loss_35, p_loss_45, n_loss = self._extract_stat(all_stats, f"loss_{loss_type}")

        # Player probability (with bayesian update)
        player_key = f"player_general_{losing_player}"
        p_player_stat = all_stats.get(player_key)
        if p_player_stat and p_player_stat.total_samples >= settings.min_player_sample:
            o15_hits = getattr(p_player_stat, "over15_hits", 0) or 0
            p_player_15 = bayesian_update(int(0.75 * 10), 10, o15_hits, p_player_stat.total_samples)
            p_player_25 = bayesian_update(int(0.50 * 10), 10, p_player_stat.over25_hits, p_player_stat.total_samples)
            p_player_35 = bayesian_update(int(0.35 * 10), 10, p_player_stat.over35_hits, p_player_stat.total_samples)
            p_player_45 = bayesian_update(int(0.20 * 10), 10, p_player_stat.over45_hits, p_player_stat.total_samples)
            n_player = p_player_stat.total_samples
        else:
            p_player_15, p_player_25, p_player_35, p_player_45 = 0.75, 0.50, 0.35, 0.20
            n_player = p_player_stat.total_samples if p_player_stat else 0
            # Fallback: Player table
            try:
                profile = await self.players.get_profile(losing_player)
                if profile and profile.total_return_matches >= settings.min_player_sample:
                    o15_loss = getattr(profile, "over15_after_loss", 0) or 0
                    p_player_15 = bayesian_update(7, 10, o15_loss, profile.total_return_matches)
                    p_player_25 = bayesian_update(5, 10, profile.over25_after_loss, profile.total_return_matches)
                    p_player_35 = bayesian_update(3, 10, profile.over35_after_loss, profile.total_return_matches)
                    p_player_45 = 0.20
                    n_player = profile.total_return_matches
            except Exception:
                pass

        # Recent form (with bayesian update)
        form_key = f"recent_form_{losing_player}"
        form_stat = all_stats.get(form_key)
        if form_stat and form_stat.total_samples >= 10:
            o15_hits_f = getattr(form_stat, "over15_hits", 0) or 0
            p_form_15 = bayesian_update(7, 10, o15_hits_f, form_stat.total_samples)
            p_form_25 = bayesian_update(5, 10, form_stat.over25_hits, form_stat.total_samples)
            p_form_35 = bayesian_update(3, 10, form_stat.over35_hits, form_stat.total_samples)
            p_form_45 = bayesian_update(2, 10, form_stat.over45_hits, form_stat.total_samples)
            n_form = form_stat.total_samples
        else:
            p_form_15, p_form_25, p_form_35, p_form_45 = 0.75, 0.50, 0.35, 0.20
            n_form = form_stat.total_samples if form_stat else 0

        # H2H (with bayesian update)
        h2h_key = f"h2h_{losing_player}_vs_{opponent_player}" if opponent_player else None
        h2h_stat = all_stats.get(h2h_key) if h2h_key else None
        if h2h_stat and h2h_stat.total_samples >= settings.min_h2h_sample:
            o15_hits_h = getattr(h2h_stat, "over15_hits", 0) or 0
            p_h2h_15 = bayesian_update(3, 4, o15_hits_h, h2h_stat.total_samples)
            p_h2h_25 = bayesian_update(2, 4, h2h_stat.over25_hits, h2h_stat.total_samples)
            p_h2h_35 = bayesian_update(1, 4, h2h_stat.over35_hits, h2h_stat.total_samples)
            p_h2h_45 = bayesian_update(1, 4, h2h_stat.over45_hits, h2h_stat.total_samples)
            n_h2h = h2h_stat.total_samples
        else:
            p_h2h_15, p_h2h_25, p_h2h_35, p_h2h_45 = 0.75, 0.50, 0.35, 0.20
            n_h2h = 0

        # Y post-win (with bayesian update)
        ypw_key = f"y_post_win_{opponent_player}" if opponent_player else None
        ypw_stat = all_stats.get(ypw_key) if ypw_key else None
        if ypw_stat and ypw_stat.total_samples >= 10:
            o15_hits_y = getattr(ypw_stat, "over15_hits", 0) or 0
            p_ypw_15 = bayesian_update(7, 10, o15_hits_y, ypw_stat.total_samples)
            p_ypw_25 = bayesian_update(5, 10, ypw_stat.over25_hits, ypw_stat.total_samples)
            p_ypw_35 = bayesian_update(3, 10, ypw_stat.over35_hits, ypw_stat.total_samples)
            p_ypw_45 = bayesian_update(2, 10, ypw_stat.over45_hits, ypw_stat.total_samples)
            n_ypw = ypw_stat.total_samples
        else:
            p_ypw_15, p_ypw_25, p_ypw_35, p_ypw_45 = 0.75, 0.50, 0.35, 0.20
            n_ypw = 0

        # Time slot
        time_key = f"time_{slot}"
        time_stat = all_stats.get(time_key)
        if time_stat and time_stat.total_samples >= 10:
            p_time_15 = getattr(time_stat, "hit_rate_15", 0.0) or p_base_15
            p_time_25, p_time_35, p_time_45 = time_stat.hit_rate_25, time_stat.hit_rate_35, time_stat.hit_rate_45
        else:
            p_time_15, p_time_25, p_time_35, p_time_45 = p_base_15, p_base_25, p_base_35, p_base_45

        # Loss type fallback to global
        loss_stat = all_stats.get(f"loss_{loss_type}")
        if not loss_stat or loss_stat.total_samples < 10:
            p_loss_15, p_loss_25, p_loss_35, p_loss_45 = p_base_15, p_base_25, p_base_35, p_base_45

        # ── PARALELIZACAO (2026-04-23): 7 queries DB independentes em 1 gather ──
        # Antes: 7 awaits sequenciais (~5-6s em Supabase). Agora: 1 gather (~700ms).
        # Todas sao read-only e nao dependem umas das outras. Resultado: latencia de
        # alerta cai de 7s para ~2s. Rollback: desfazer o gather e voltar a chamadas
        # sequenciais nas linhas originais (625, 656, 686, 707, 713, 817, 818).
        (
            (p_team_25, n_team),
            (streak_val, streak_factor),
            (player_team_factor, player_team_n),
            session_momentum,
            (is_new_player, new_player_matches),
            regime,
            cold_done,
        ) = await asyncio.gather(
            self.get_team_probability(loser_team, opponent_team, losing_player),
            self.get_streak_factor(losing_player),
            self.get_player_team_factor(losing_player, loser_team),
            self.get_session_momentum_factor(losing_player),
            self.check_new_player(losing_player),
            self.check_regime(),
            self.is_cold_start_complete(),
        )
        # Scale team rate proportionally for other lines using global base ratios
        if p_base_25 > 0:
            p_team_15 = p_team_25 * (p_base_15 / p_base_25)
            p_team_35 = p_team_25 * (p_base_35 / p_base_25)
            p_team_45 = p_team_25 * (p_base_45 / p_base_25)
        else:
            p_team_15, p_team_35, p_team_45 = p_base_15, p_base_35, p_base_45

        # p_market_adj agora calculado pelo enhanced_market_adjustment (mais abaixo)

        # Hierarquia de player layer:
        # H2H (min 7) > forma recente (min 10) > player geral (min 30) > base
        if n_h2h >= settings.min_h2h_sample:
            eff_p15, eff_p25, eff_p35, eff_p45, eff_n = p_h2h_15, p_h2h_25, p_h2h_35, p_h2h_45, n_h2h
        elif n_form >= 10:
            eff_p15, eff_p25, eff_p35, eff_p45, eff_n = p_form_15, p_form_25, p_form_35, p_form_45, n_form
        elif n_player >= settings.min_player_sample:
            eff_p15, eff_p25, eff_p35, eff_p45, eff_n = p_player_15, p_player_25, p_player_35, p_player_45, n_player
        else:
            eff_p15, eff_p25, eff_p35, eff_p45, eff_n = p_base_15, p_base_25, p_base_35, p_base_45, 0

        # Y post-win bonus: se Y tende a ceder (alta taxa), boost +3%
        ypw_adj = 0.0
        if n_ypw >= 10:
            ypw_adj = (p_ypw_25 - p_base_25) * 0.2  # 20% do desvio de Y como bonus

        # Layer: fator de ajuste G1 (secundario — base e o historico G2 do jogador)
        g1_goals_factor = self.get_g1_goals_factor(loser_goals_g1)

        # Layer: streak de derrotas consecutivas (ja resolvido no gather acima)

        # Layer: total de gols em G1
        total_g1 = game1_score_winner + game1_score_loser
        total_g1_factor = self.get_total_g1_factor(total_g1)

        # Layer: padrao do jogo G1
        game_pattern, game_pattern_factor = self.get_game_pattern(
            game1_score_winner, game1_score_loser
        )

        # Layer: blacklist/elite de jogadores
        player_flag = self.get_player_flag(losing_player)

        # Layer: vencedor de G1 (cede ou tranca?)
        winner_factor = self.get_winner_factor(opponent_player)

        # Layer: time do perdedor em G2
        loser_team_factor = self.get_loser_team_factor(loser_team)

        # Layer: time do oponente em G2 (defesa fraca/forte)
        opp_team_factor = self.get_opp_team_factor(opponent_team)

        # Layer: hora do dia
        hour_factor = self.get_hour_factor(match_time.hour)

        # Layer: placar exato G1 (fator mais preciso — 44pp entre melhor e pior)
        exact_score_factor = self.get_exact_score_factor(game1_score_winner, game1_score_loser)

        # Layer: combo jogador + time em G2 (ja resolvido no gather acima)

        # Layer: perdedor "on fire" (4+ gols em G1)
        on_fire_factor = self.get_on_fire_factor(loser_goals_g1)

        # Layer: tipo de matchup (elite vs elite)
        matchup_type_factor = self.get_matchup_type_factor(losing_player, opponent_player or "")

        # Layer: H2H-validated — gols do loser em G1 (categoria)
        loser_g1_cat_factor = self.get_loser_g1_cat_factor(loser_goals_g1)

        # Layer: H2H-validated — total gols G1 (categoria)
        total_g1_cat_factor = self.get_total_g1_cat_factor(total_g1)

        # Layer: H2H-validated — periodo do dia
        hour_period_factor = self.get_hour_period_factor(match_time.hour)

        # Layer: H2H-validated — loser era home ou away em G1
        home_away_g1_factor = self.get_home_away_g1_factor(loser_was_home_g1)

        # Layer: momentum de sessao (ja resolvido no gather acima)

        # Layer: odds movement (enhanced com time decay)
        p_market_adj = self.get_enhanced_market_adjustment(odds_history or [])

        # Layer: jogador novo (ja resolvido no gather acima)

        def _dampen(factor: float, strength: float = 0.5) -> float:
            """Compress factor towards 1.0. strength=0.5 = half effect."""
            return 1.0 + (factor - 1.0) * strength

        def compute_true_prob(pb, pl, pp, pt, pteam, pma, extra_adj=0.0):
            # Base principal: historico do jogador em G2 (via pp = player prob)
            tp = calculate_true_probability(
                p_base=pb,
                p_loss_type=pl,
                p_player=pp,
                p_time_slot=pt,
                p_team=pteam,
                p_market_adj=pma + extra_adj,
                n_player=eff_n,
                n_team=n_team,
                min_sample=settings.min_player_sample,
            )
            # Fatores multiplicativos consolidados (5 independentes, dampened)
            # Removidos: hour_factor (ja em p_time_slot), on_fire (ja em exact_score),
            #   matchup_type (redundante c/ winner+player), elite/blacklist (ja em p_player)
            effective_team_factor = player_team_factor if player_team_n >= 5 else loser_team_factor
            # Absorve streak no session_momentum (ambos medem forma recente)
            combined_momentum = session_momentum * streak_factor

            product = (
                _dampen(exact_score_factor)    # placar exato G1 (absorve on_fire)
                * _dampen(winner_factor)        # quem ganhou G1 (absorve matchup_type)
                * _dampen(effective_team_factor) # combo jogador+time (ou time generico)
                * _dampen(opp_team_factor)      # time oponente em G2 (defesa)
                * _dampen(combined_momentum)    # momentum + streak combinados
                # H2H-validated factors (dampening mais fraco pois overlap parcial)
                * _dampen(loser_g1_cat_factor, 0.3)  # gols loser G1: shutout/normal/on-fire
                * _dampen(total_g1_cat_factor, 0.3)   # total G1: fechado/normal/aberto
                * _dampen(hour_period_factor, 0.2)     # periodo: manha/tarde/madrugada/noite
                * _dampen(home_away_g1_factor, 0.4)    # perdeu em casa ou fora
            )
            # Cap: max ±20% de ajuste sobre a media ponderada
            product = max(0.80, min(1.20, product))
            tp = tp * product
            return max(0.0, min(1.0, tp))

        tp15 = compute_true_prob(p_base_15, p_loss_15, eff_p15, p_time_15, p_team_15, p_market_adj, ypw_adj)
        tp25 = compute_true_prob(p_base_25, p_loss_25, eff_p25, p_time_25, p_team_25, p_market_adj, ypw_adj)
        tp35 = compute_true_prob(p_base_35, p_loss_35, eff_p35, p_time_35, p_team_35, p_market_adj, ypw_adj)
        tp45 = compute_true_prob(p_base_45, p_loss_45, eff_p45, p_time_45, p_team_45, p_market_adj, ypw_adj)

        # Bloqueios hard: nao alertar
        # 1. Streak >= 5
        if streak_val >= 5:
            logger.info(
                f"Streak de {streak_val} derrotas para {losing_player}: nao alertar"
            )
            return OpportunityEvaluation(
                should_alert=False,
                reason=f"Streak de {streak_val} derrotas consecutivas (penalidade maxima)",
                best_line="over25",
                implied_prob=0.0,
                true_prob=tp25,
                true_prob_conservative=tp25,
                confidence_interval=(0.0, 1.0),
                edge_val=0.0,
                expected_value_val=0.0,
                kelly_fraction_val=0.0,
                star_rating_val=0,
                p_base=p_base_25,
                p_loss_type=p_loss_25,
                p_player=eff_p25,
                p_recent_form=p_form_25,
                p_h2h=p_h2h_25,
                p_y_post_win=p_ypw_25,
                p_time_slot=p_time_25,
                p_team=p_team_25,
                p_market_adj=p_market_adj,
                player_sample_size=n_player,
                h2h_sample_size=n_h2h,
                recent_form_sample=n_form,
                global_sample_size=n_global,
                loss_type_sample_size=n_loss,
                team_sample_size=n_team,
                loss_type=loss_type,
                loss_margin=loss_margin,
                p_g1_goals=g1_goals_factor,
                loser_goals_g1=loser_goals_g1,
                streak=streak_val,
                streak_factor=streak_factor,
                total_g1_goals=total_g1,
                total_g1_factor=total_g1_factor,
                game_pattern=game_pattern,
                game_pattern_factor=game_pattern_factor,
                player_flag=player_flag,
                loser_g1_cat_factor=loser_g1_cat_factor,
                total_g1_cat_factor=total_g1_cat_factor,
                hour_period_factor=hour_period_factor,
                home_away_g1_factor=home_away_g1_factor,
            )

        # 2. Blacklist: penalidade forte ja aplicada (x0.92) no compute_true_prob
        if player_flag == "blacklist":
            logger.debug(
                f"Jogador {losing_player} na blacklist (penalidade x0.92 aplicada)"
            )

        # regime e cold_done ja resolvidos no gather acima

        # Wilson CI: usar dados do jogador quando disponivel (mais preciso)
        # Hierarquia: H2H (min 10) > forma recente (min 10) > player geral (min 10) > global
        global_stat = all_stats.get("global")
        _MIN_CI_SAMPLES = 10  # minimo pra confiar no CI individual

        # Escolher a melhor fonte de dados para o CI
        _ci_stat = None
        _ci_source = "global"
        if h2h_stat and h2h_stat.total_samples >= _MIN_CI_SAMPLES:
            _ci_stat = h2h_stat
            _ci_source = "h2h"
        elif form_stat and form_stat.total_samples >= _MIN_CI_SAMPLES:
            _ci_stat = form_stat
            _ci_source = "recent_form"
        elif p_player_stat and p_player_stat.total_samples >= _MIN_CI_SAMPLES:
            _ci_stat = p_player_stat
            _ci_source = "player"

        if _ci_stat:
            _obs = {
                "over15": getattr(_ci_stat, "over15_hits", 0) or 0,
                "over25": _ci_stat.over25_hits,
                "over35": _ci_stat.over35_hits,
                "over45": _ci_stat.over45_hits,
                "ml": 0,
            }
            _obs_total = _ci_stat.total_samples
        else:
            _obs = {
                "over15": getattr(global_stat, "over15_hits", 0) if global_stat else 0,
                "over25": global_stat.over25_hits if global_stat else 0,
                "over35": global_stat.over35_hits if global_stat else 0,
                "over45": global_stat.over45_hits if global_stat else 0,
                "ml": 0,
            }
            _obs_total = global_stat.total_samples if global_stat else 0

        logger.debug(
            f"Wilson CI source for {losing_player}: {_ci_source} "
            f"(n={_obs_total})"
        )

        def eval_line(line_name: str, odds: float | None, tp: float, n_samp: int) -> LineEvaluation | None:
            if odds is None or odds <= 0:
                return None

            # ── Blacklist condicional (producao 05-14/Abr) ──────────────
            cond_blocked, cond_reason = self.is_conditionally_blacklisted(
                losing_player, line_name, loser_was_home_g1
            )
            if cond_blocked:
                return LineEvaluation(
                    line=line_name, odds=odds, true_prob=tp,
                    true_prob_conservative=tp,
                    implied_prob=implied_probability(odds),
                    edge_val=0.0, ev_val=0.0, kelly_val=0.0, stars=0,
                    should_alert=False,
                    reason=f"Blacklist condicional: {cond_reason}",
                )

            # ── Filtros por linha (auditoria 2026-04-04) ──────────────
            # O1.5: só alerta se odds >= 1.65 E perdedor fez 2+ gols em G1.
            # Para SWAP players: odds minima 1.70 (requisito do backtest da estrategia).
            if line_name == "over15":
                min_odds_o15 = (
                    self.SWAP_OVER15_MIN_ODDS
                    if losing_player in self.PLAYER_SWAP_TO_OVER15
                    else 1.65
                )
                if odds < min_odds_o15 or loser_goals_g1 < 2:
                    return LineEvaluation(
                        line=line_name, odds=odds, true_prob=tp,
                        true_prob_conservative=tp,
                        implied_prob=implied_probability(odds),
                        edge_val=0.0, ev_val=0.0, kelly_val=0.0, stars=0,
                        should_alert=False,
                        reason=f"O1.5 filtrado: odds={odds:.2f}<{min_odds_o15} ou g1_goals={loser_goals_g1}<2",
                    )

            # O2.5 e O3.5: odds mínima 1.80
            if line_name in ("over25", "over35"):
                if odds < 1.80:
                    return LineEvaluation(
                        line=line_name, odds=odds, true_prob=tp,
                        true_prob_conservative=tp,
                        implied_prob=implied_probability(odds),
                        edge_val=0.0, ev_val=0.0, kelly_val=0.0, stars=0,
                        should_alert=False,
                        reason=f"{line_name} filtrado: odds={odds:.2f}<1.80",
                    )

            # Probabilidade de decisao: blend entre tp (Bayesiano, todas camadas)
            # e taxa individual real do jogador.
            # Com 6+ amostras individuais, individual pesa mais que tp.
            obs_hits = min(_obs.get(line_name, 0), _obs_total)
            individual_rate = obs_hits / _obs_total if _obs_total > 0 else tp

            # Peso individual: n=3 -> 30%, n=6 -> 60%, n=10+ -> 100%
            ind_weight = min(1.0, _obs_total / 10.0) if _obs_total > 0 else 0.0
            decision_prob = tp * (1.0 - ind_weight) + individual_rate * ind_weight

            # Wilson CI conservador (para display/transparencia)
            ci = wilson_confidence_interval(
                successes=obs_hits,
                total=_obs_total,
            )
            tp_cons = ci[0] if _obs_total > 0 else tp

            impl = implied_probability(odds)
            edge_v = edge(decision_prob, odds)
            ev_v = expected_value(decision_prob, odds)
            kelly_v = fractional_kelly(decision_prob, odds, settings.kelly_fraction)
            stars = star_rating(edge_v, ev_v)
            # Jogador novo: exigir edge mais alto (menos dados = mais incerteza)
            effective_min_edge = settings.min_edge
            if is_new_player:
                effective_min_edge = max(settings.min_edge, 0.25)  # min 25% para novos

            # Loser é HOME ou AWAY em G2 (mesmo lado que G1 no Esoccer)
            loser_is_home_g2 = loser_was_home_g1  # None se desconhecido

            # Filtro tight AWAY: bloqueio total (WR=39.5%, ROI=-24.9%)
            if loser_is_home_g2 is False and loss_type == "tight":
                return LineEvaluation(
                    line=line_name, odds=odds, true_prob=tp,
                    true_prob_conservative=tp,
                    implied_prob=implied_probability(odds),
                    edge_val=edge_v, ev_val=0.0, kelly_val=0.0, stars=0,
                    should_alert=False,
                    reason="tight AWAY bloqueado (WR=39.5%, ROI=-24.9%)",
                )

            alert, reason = should_alert(
                edge_val=edge_v,
                ev_val=ev_v,
                true_prob_conservative=decision_prob,
                odds=odds,
                global_sample=n_samp,
                regime_status=regime["status"],
                cold_start_complete=cold_done,
                min_edge=effective_min_edge,
                min_ev=settings.min_ev,
                min_true_prob=settings.min_true_prob,
                min_odds=settings.min_odds,
                max_odds=settings.max_odds,
                min_global_sample=settings.min_global_sample,
            )
            return LineEvaluation(
                line=line_name,
                odds=odds,
                true_prob=tp,
                true_prob_conservative=decision_prob,
                implied_prob=impl,
                edge_val=edge_v,
                ev_val=ev_v,
                kelly_val=kelly_v,
                stars=stars,
                should_alert=alert,
                reason=reason,
            )

        le15 = eval_line("over15", over15_odds, tp15, n_global)
        le25 = eval_line("over25", over25_odds, tp25, n_global)
        le35 = eval_line("over35", over35_odds, tp35, n_global)
        le45 = eval_line("over45", over45_odds, tp45, n_global)

        # ── ML (Moneyline): perdedor vence G2 ──────────────────────────
        le_ml = None
        if ml_odds and ml_odds > 1.0:
            tp_ml = self._compute_ml_true_prob(
                losing_player=losing_player,
                opponent_player=opponent_player or "",
                loser_team=loser_team,
                opponent_team=opponent_team,
                loss_margin=loss_margin,
                loser_goals_g1=loser_goals_g1,
                game1_score_winner=game1_score_winner,
                game1_score_loser=game1_score_loser,
                hour=match_time.hour if match_time else 12,
                player_flag=player_flag,
                streak_val=streak_val,
                loss_type=loss_type,
                db_stats=all_stats,
            )
            le_ml = eval_line("ml", ml_odds, tp_ml, n_global)

        # Pick best EV among alertable lines; fallback to first available
        all_evals = [le15, le25, le35, le45, le_ml]
        candidates = [l for l in all_evals if l and l.should_alert]
        best = max(candidates, key=lambda l: l.ev_val) if candidates else (le25 or le35 or le15 or le45)
        if best is None:
            best = LineEvaluation(
                line="over25", odds=over25_odds or 1.5,
                true_prob=tp25, true_prob_conservative=tp25,
                implied_prob=(
                    implied_probability(over25_odds)
                    if over25_odds and over25_odds >= 1.0 else 0.5
                ),
                edge_val=0.0, ev_val=0.0, kelly_val=0.0, stars=0,
                should_alert=False, reason="No line available"
            )

        logger.info(
            f"Eval: player={losing_player}, best={best.line} @{best.odds:.2f}, "
            f"edge={best.edge_val:.2%}, ev={best.ev_val:.2%}, stars={best.stars}, alert={best.should_alert}"
        )

        return OpportunityEvaluation(
            should_alert=best.should_alert,
            reason=best.reason,
            best_line=best.line,
            implied_prob=best.implied_prob,
            true_prob=best.true_prob,
            true_prob_conservative=best.true_prob_conservative,
            confidence_interval=(0.0, 1.0),
            edge_val=best.edge_val,
            expected_value_val=best.ev_val,
            kelly_fraction_val=best.kelly_val,
            star_rating_val=best.stars,
            p_base=p_base_25,
            p_loss_type=p_loss_25,
            p_player=eff_p25,
            p_recent_form=p_form_25,
            p_h2h=p_h2h_25,
            p_y_post_win=p_ypw_25,
            p_time_slot=p_time_25,
            p_team=p_team_25,
            p_market_adj=p_market_adj,
            player_sample_size=n_player,
            h2h_sample_size=n_h2h,
            recent_form_sample=n_form,
            global_sample_size=n_global,
            loss_type_sample_size=n_loss,
            team_sample_size=n_team,
            loss_type=loss_type,
            loss_margin=loss_margin,
            line_over15=le15,
            line_over25=le25,
            line_over35=le35,
            line_over45=le45,
            line_ml=le_ml,
            p_g1_goals=g1_goals_factor,
            loser_goals_g1=loser_goals_g1,
            streak=streak_val,
            streak_factor=streak_factor,
            total_g1_goals=total_g1,
            total_g1_factor=total_g1_factor,
            game_pattern=game_pattern,
            game_pattern_factor=game_pattern_factor,
            player_flag=player_flag,
            loser_g1_cat_factor=loser_g1_cat_factor,
            total_g1_cat_factor=total_g1_cat_factor,
            hour_period_factor=hour_period_factor,
            home_away_g1_factor=home_away_g1_factor,
        )

    def _blocked_evaluation(self, reason: str, **kw) -> OpportunityEvaluation:
        """Helper para retornar avaliacao bloqueada com todos os campos."""
        return OpportunityEvaluation(
            should_alert=False,
            reason=reason,
            best_line="over25",
            implied_prob=0.0,
            true_prob=kw.get("tp25", 0.0),
            true_prob_conservative=kw.get("tp25", 0.0),
            confidence_interval=(0.0, 1.0),
            edge_val=0.0,
            expected_value_val=0.0,
            kelly_fraction_val=0.0,
            star_rating_val=0,
            p_base=kw.get("p_base_25", 0.0),
            p_loss_type=kw.get("p_loss_25", 0.0),
            p_player=kw.get("eff_p25", 0.0),
            p_recent_form=kw.get("p_form_25", 0.0),
            p_h2h=kw.get("p_h2h_25", 0.0),
            p_y_post_win=kw.get("p_ypw_25", 0.0),
            p_time_slot=kw.get("p_time_25", 0.0),
            p_team=kw.get("p_team_25", 0.0),
            p_market_adj=kw.get("p_market_adj", 0.0),
            player_sample_size=kw.get("n_player", 0),
            h2h_sample_size=kw.get("n_h2h", 0),
            recent_form_sample=kw.get("n_form", 0),
            global_sample_size=kw.get("n_global", 0),
            loss_type_sample_size=kw.get("n_loss", 0),
            team_sample_size=kw.get("n_team", 0),
            loss_type=kw.get("loss_type", ""),
            loss_margin=kw.get("loss_margin", 0),
            p_g1_goals=kw.get("p_g1_25", 0.0),
            loser_goals_g1=kw.get("loser_goals_g1", 0),
            streak=kw.get("streak_val", 0),
            streak_factor=kw.get("streak_factor", 1.0),
            total_g1_goals=kw.get("total_g1", 0),
            total_g1_factor=kw.get("total_g1_factor", 1.0),
            game_pattern=kw.get("game_pattern", ""),
            game_pattern_factor=kw.get("game_pattern_factor", 1.0),
            player_flag=kw.get("player_flag", ""),
            loser_g1_cat_factor=kw.get("loser_g1_cat_factor", 1.0),
            total_g1_cat_factor=kw.get("total_g1_cat_factor", 1.0),
            hour_period_factor=kw.get("hour_period_factor", 1.0),
            home_away_g1_factor=kw.get("home_away_g1_factor", 1.0),
        )

    # ------------------------------------------------------------------
    # Watch (pre-alerta T-90s) — prediz candidato com odds-alvo
    # ------------------------------------------------------------------

    WATCH_MIN_TP: float = 0.60  # decision_prob minima para acionar watch

    async def predict_watch_candidate(
        self,
        return_match,
        game1_match,
        losing_player: str,
        opponent_player: str,
        loser_goals_g1: int,
        loser_was_home_g1: bool | None = None,
    ) -> dict | None:
        """Prediz se um match vai gerar alerta, retorna info do watch ou None.

        Usa evaluate_opportunity com odds-alvo (1.65/1.80) para detectar linhas
        com decision_prob >= WATCH_MIN_TP que nao foram bloqueadas por filtros
        estruturais (blacklist, conditional, swap, g1 goals).
        Retorna o candidato com maior decision_prob.
        """
        from datetime import datetime, timezone

        over15_target = (
            self.SWAP_OVER15_MIN_ODDS
            if losing_player in self.PLAYER_SWAP_TO_OVER15
            else 1.65
        )

        score_home = game1_match.score_home or 0
        score_away = game1_match.score_away or 0
        if game1_match.player_home == losing_player:
            score_winner = score_away
            score_loser = score_home
            loser_team = game1_match.team_home
            opponent_team = game1_match.team_away
        else:
            score_winner = score_home
            score_loser = score_away
            loser_team = game1_match.team_away
            opponent_team = game1_match.team_home

        try:
            evaluation = await self.evaluate_opportunity(
                losing_player=losing_player,
                opponent_player=opponent_player,
                game1_score_winner=score_winner,
                game1_score_loser=score_loser,
                over25_odds=1.80,
                over35_odds=1.80,
                over15_odds=over15_target,
                over45_odds=None,
                ml_odds=None,
                match_time=return_match.started_at or datetime.now(timezone.utc),
                loser_team=loser_team,
                opponent_team=opponent_team,
                odds_history=[],
                loser_goals_g1=loser_goals_g1,
                loser_was_home_g1=loser_was_home_g1,
            )
        except Exception as e:
            logger.warning(f"predict_watch_candidate failed for {losing_player}: {e}")
            return None

        candidates = []
        for le, tgt in [
            (evaluation.line_over15, over15_target),
            (evaluation.line_over25, 1.80),
            (evaluation.line_over35, 1.80),
        ]:
            if le is None or le.odds is None or le.odds <= 0:
                continue
            reason_lower = (le.reason or "").lower()
            if any(tok in reason_lower for tok in ("blacklist", "filtrado", "bloqueado")):
                continue
            if le.true_prob_conservative >= self.WATCH_MIN_TP:
                candidates.append((le, tgt))

        if not candidates:
            return None

        best, target = max(candidates, key=lambda x: x[0].true_prob_conservative)

        line_labels = {"over15": "over 1.5", "over25": "over 2.5", "over35": "over 3.5"}
        return {
            "line": best.line,
            "line_label": line_labels.get(best.line, best.line),
            "target_odds": target,
            "target_player": losing_player,
            "predicted_tp": best.true_prob_conservative,
            "would_alert_at_target": best.should_alert,
        }

    # ------------------------------------------------------------------
    # Novos layers: G1 goals, streak, total G1, game pattern, blacklist
    # ------------------------------------------------------------------

    # Calibrado em 9904 pares G1→G2 reais (2026-03-25)
    G1_GOALS_FACTOR: dict[int, float] = {
        0: 0.80, 1: 0.92, 2: 1.05, 3: 1.12, 4: 1.10, 5: 1.20,
    }
    # 4g mantido em 1.10 pois on_fire(4+)=1.10 completa → 1.21 (real=1.21)
    # 5g subiu para 1.20 → com on_fire: 1.20*1.10=1.32 (real=1.33)
    # 3g subiu de 1.05→1.12: maior gap isolado, sem bonus on_fire

    # Calibrado em 9904 pares G1→G2 reais (2026-03-25)
    TOTAL_G1_FACTOR: dict[int, float] = {
        1: 0.85, 2: 0.69, 3: 0.91, 4: 0.89, 5: 0.98,
        6: 1.02, 7: 1.10, 8: 1.04, 9: 1.16, 10: 1.26,
    }

    # Calibrado em 9907 pares reais (2026-03-25)
    # 8 categorias em vez de 5, com fatores baseados em O2.5 real de cada padrao
    GAME_PATTERN_FACTOR: dict[str, float] = {
        "cleansheet_low": 0.80,       # 0g perdedor, total<=2 (O2.5=38.8%)
        "cleansheet_high": 0.87,      # 0g perdedor, total>2  (O2.5=43.4%)
        "passive_loss": 0.91,         # 1g perdedor, margem>=2 (O2.5=46.6%)
        "tight_game": 0.95,           # 1g perdedor, margem=1  (O2.5=48.7%)
        "balanced": 1.02,             # 2+g perdedor, margem=1 (O2.5=52.3%)
        "massacre_open": 1.13,        # 3+g perdedor, margem=2 (O2.5=58.1%)
        "high_open": 1.16,            # 3+g perdedor, margem=1 (O2.5=59.5%)
        "goleada_open": 1.19,         # 3+g perdedor, margem>=3 (O2.5=61.2%)
    }

    # Jogadores com WR consistentemente ruim em alertas reais
    # Atualizado 2026-04-14 com dados de producao (05-14/Abr, 385 alertas)
    PLAYER_BLACKLIST: set[str] = {
        "Kavviro", "SPACE", "R0ge",
        "maksdh", "Kot",
        "Boulevard", "A1ose",
        # Novos (producao 05-14/Abr): WR < 35%, sem nichos positivos consistentes
        "Revange",   # 5 alertas, WR=0%, P/L=-5.00
        "V1nn",      # 12 alertas, WR=33%, P/L=-4.73
        "Stormi",    # Adicionado 2026-04-24: saiu dynamic blacklist e voltou a dar RED
    }

    # Jogadores que devem ter a linha trocada para Over 1.5 (swap strategy).
    # Baseado em backtest 30d: real_pl negativo mas over1.5@>=1.70 positivo.
    # Em eval_line: over25/35/45 bloqueados; over15 exige odds>=1.70.
    PLAYER_SWAP_TO_OVER15: set[str] = {
        "pikalicaaa", "Jekunam", "RossFCDK",
        # Kivu17 removido 2026-04-23: A/B 8d mostrou ANTIGA O2.5 +3.37u (WR 55.6%, 9 alertas)
        # vs SWAP O1.5 -1.00u (1 alerta). SWAP estava custando -4.37u no caso dele. Volta ao default.
    }
    SWAP_OVER15_MIN_ODDS: float = 1.70

    # Jogadores bloqueados condicionalmente (producao 05-14/Abr)
    # Formato: jogador -> {"block_home": bool, "block_away": bool,
    #                      "block_lines": set, "block_all": bool}
    # Bloqueio se QUALQUER condicao bater (OR)
    PLAYER_CONDITIONAL_BLACKLIST: dict[str, dict] = {
        # volvo: HOME=WR 57% P/L+0.69 (OK), AWAY=WR 0% P/L-5.00 (desastroso)
        "volvo": {"block_away_g2": True},
        # Grellz: HOME=WR 0% P/L-4.00 (zero greens), AWAY=WR 62% P/L+0.66 (OK)
        "Grellz": {"block_home_g2": True},
        # nikkitta: HOME=WR 25% P/L-4.53 (ruim), Over2.5=WR 0% P/L-4.00 (zero greens)
        "nikkitta": {"block_home_g2": True, "block_lines": {"over25", "over35", "over45"}},
        # Cira: Over1.5=WR 75% P/L+1.12 (bom), Over2.5=WR 25% P/L-3.98 (ruim)
        "Cira": {"block_lines": {"over25", "over35", "over45"}},
        # tohi4: Over1.5=WR 75% P/L+1.24 (bom), Over2.5=WR 20% P/L-3.13 (ruim)
        "tohi4": {"block_lines": {"over25", "over35", "over45"}},
        # dor1an: 30d over25=39% (33 tips, -9.46u), over35=80% (5 tips, +2.96u ok)
        # Removido do ELITE e WINNER_BOOST. Bloqueia only over25 (linha principal de perda).
        "dor1an": {"block_lines": {"over25"}},
        # 2026-04-25: hotShot e Kavviro removidos do conditional + dynamic exempt
        # para serem avaliados pelo auto-block per (player, line). Se tomarem -3u
        # numa linha especifica, auto-block bloqueia automaticamente.
        # (Revange, Boulevard, SPACE ja estao em PLAYER_BLACKLIST —
        # duplicados aqui para garantir o gate, ja que a blacklist estatica nao
        # bloqueia por si so)
        "Revange":   {"block_all": True},
        "Boulevard": {"block_all": True},
        "SPACE":     {"block_all": True},
        # SWAP → bloqueia over25/35/45; over15 passa se odds>=1.70 (ver eval_line)
        # Kivu17 removido 2026-04-23: A/B 8d ANTIGA +3.37u (WR 55.6%) vs SWAP -1u. Volta ao default.
        "pikalicaaa": {"block_lines": {"over25", "over35", "over45"}},
        "Jekunam":    {"block_lines": {"over25", "over35", "over45"}},
        "RossFCDK":   {"block_lines": {"over25", "over35", "over45"}},
    }

    # Jogadores com O2.5 >= 62% em G2 (n>=90, dados calibrados)
    # V1nn removido: WR=33% em producao (05-14/Abr), movido para blacklist
    # dor1an removido 2026-04-21: 7d WR=36.4% (22 tips, -7.08u), 30d -6.51u
    PLAYER_ELITE: set[str] = {
        "Bomb1to", "DaVa", "LaikingDast",
        "OG", "DangerDim77", "tonexo",
        "Wboy", "RuBIX", "Uncle", "Kray",
    }

    # Vencedores que cedem mais ao perdedor no G2 (facilitam O2.5)
    # dor1an removido 2026-04-21: performance 7d degradada
    WINNER_BOOST: set[str] = {
        "hrk", "V1nn", "gelennzz", "Wboy", "tohi4", "Arcos",
        "Bomb1to", "LaikingDast", "dm1trena", "JKey", "tonexo",
        "RossFCDK", "sane4ek8", "Asura",
        "GianniKid", "Aveaaaaa", "Nightxx", "Kodak",
    }

    # Vencedores que trancam o perdedor no G2 (dificultam O2.5)
    WINNER_PENALTY: set[str] = {
        "noeN", "TikitakaMaster", "Boulevard", "A1ose",
        "deroll", "Morty", "BlackStar98", "hit",
    }

    # Time do PERDEDOR: com qual time ele marca mais em G2 (O2.5 >= 55%, n>=70)
    TEAM_BOOST: set[str] = {
        "Croatia", "Netherlands", "Dynamo Kyiv", "Morocco", "Italy",
        "Benfica", "Sassuolo", "Al Hilal", "Czechia", "England",
    }

    # Time do PERDEDOR: com qual time ele marca menos (O2.5 <= 47%, n>=60)
    TEAM_PENALTY: set[str] = {
        "Al Ittihad", "Hungary", "FC Salzburg", "FC Porto",
        "Crystal Palace", "PSG", "Barcelona", "Chelsea",
    }

    # Time do OPONENTE com defesa fraca — cede mais gols ao perdedor (O2.5 cedido >= 55%, n>=60)
    OPP_TEAM_BOOST: set[str] = {
        "Netherlands", "Portugal", "Italy", "Vitoria SC", "Croatia",
        "Sassuolo", "Crystal Palace", "Sporting", "PSG", "Juventus",
        "Morocco", "France", "Arsenal", "Denmark", "A.Bilbao",
    }

    # Time do OPONENTE com defesa forte — cede menos gols (O2.5 cedido <= 47%, n>=60)
    OPP_TEAM_PENALTY: set[str] = {
        "Benfica", "Aston Villa", "Hungary", "River Plate", "Shakhtar",
        "Liverpool", "Barcelona", "Argentina", "Czechia", "Al Ahli",
    }

    EXACT_SCORE_FACTOR: dict[str, float] = {
        "7-5": 1.55, "6-4": 1.28, "6-5": 1.28, "7-3": 1.26,
        "6-3": 1.19, "5-4": 1.19, "7-4": 1.18, "4-3": 1.13,
        "5-2": 1.10, "6-2": 1.07, "4-2": 1.07, "5-3": 1.05,
        "3-2": 1.02, "6-1": 0.99, "5-0": 0.96, "2-1": 0.95,
        "7-2": 0.94, "5-1": 0.93, "4-1": 0.90, "3-1": 0.89,
        "4-0": 0.88, "1-0": 0.85, "3-0": 0.79, "6-0": 0.74,
        "7-1": 0.73, "2-0": 0.69,
    }

    # Calibrado em 9904 pares G1→G2 reais (2026-03-25)
    HOUR_FACTOR: dict[int, float] = {
        0: 0.95, 1: 0.84, 2: 0.98, 3: 1.18, 4: 1.04, 5: 1.19,
        6: 1.11, 7: 1.13, 8: 1.09, 9: 1.09, 10: 1.10, 11: 1.13,
        12: 0.91, 13: 0.91, 14: 0.96, 15: 0.88, 16: 0.96, 17: 0.93,
        18: 1.15, 19: 0.94, 20: 0.98, 21: 0.91, 22: 0.85, 23: 0.99,
    }

    # H2H-validated (10165 pares, 312 H2Hs com 10+ jogos, 2026-03-28)
    # Gols do loser em G1 — delta medio 25.9%, 91% dos H2Hs com efeito
    LOSER_G1_CAT_FACTOR: dict[str, float] = {
        "shutout": 0.85,   # 0 gols: O1.5=66.9%, O2.5=39.0%, O3.5=19.1%
        "neutral": 1.00,   # 1 gol: baseline
        "on_fire": 1.10,   # 2+ gols: O1.5=78.2%, O2.5=55.1%, O3.5=31.1%
    }

    # Total gols G1 — delta medio 27.5%, 96% dos H2Hs com efeito
    TOTAL_G1_CAT_FACTOR: dict[str, float] = {
        "fechado": 0.88,   # 1-4 gols: O1.5=71.5%, O2.5=44.2%, O3.5=22.0%
        "normal": 1.00,    # 5-6 gols: baseline
        "aberto": 1.10,    # 7+ gols: O1.5=79.9%, O2.5=56.5%, O3.5=33.6%
    }

    # Periodo do dia — delta medio 18.4%, 82% dos H2Hs com efeito
    HOUR_PERIOD_FACTOR: dict[str, float] = {
        "manha": 1.08,       # 6-11h: O1.5=80.4%, O2.5=55.7%, O3.5=31.6%
        "tarde": 1.00,       # 12-17h: baseline
        "madrugada": 0.93,   # 0-5h: O1.5=74.0%, O2.5=47.1%, O3.5=24.9%
        "noite": 0.92,       # 18-23h: O1.5=71.7%, O2.5=46.8%, O3.5=24.5%
    }

    # Home/Away em G1 — delta medio 16.6%, 82% dos H2Hs com efeito
    HOME_AWAY_G1_FACTOR: dict[str, float] = {
        "away": 1.04,    # perdeu fora: O2.5=51.4%
        "home": 0.96,    # perdeu em casa: O2.5=49.3%
    }

    def get_g1_goals_factor(self, loser_goals_g1: int) -> float:
        return self.G1_GOALS_FACTOR[min(loser_goals_g1, 5)]

    def get_total_g1_factor(self, total_g1: int) -> float:
        return self.TOTAL_G1_FACTOR.get(max(1, min(total_g1, 10)), 1.0)

    def get_game_pattern(self, winner_goals: int, loser_goals: int) -> tuple[str, float]:
        """Classifica o padrao do jogo G1 em 8 categorias baseado em 9907 pares."""
        margin = winner_goals - loser_goals
        if loser_goals == 0 and (winner_goals + loser_goals) <= 2:
            pattern = "cleansheet_low"
        elif loser_goals == 0:
            pattern = "cleansheet_high"
        elif loser_goals >= 3 and margin >= 3:
            pattern = "goleada_open"
        elif loser_goals >= 3 and margin >= 2:
            pattern = "massacre_open"
        elif loser_goals >= 3 and margin == 1:
            pattern = "high_open"
        elif loser_goals >= 2 and margin == 1:
            pattern = "balanced"
        elif loser_goals == 1 and margin == 1:
            pattern = "tight_game"
        elif loser_goals == 1 and margin >= 2:
            pattern = "passive_loss"
        else:
            pattern = "balanced"
        return (pattern, self.GAME_PATTERN_FACTOR.get(pattern, 1.0))

    def get_player_flag(self, player_name: str) -> str:
        if player_name in self.PLAYER_BLACKLIST:
            return "blacklist"
        if player_name in self.PLAYER_ELITE:
            return "elite"
        return ""

    def get_on_fire_factor(self, loser_goals_g1: int) -> float:
        """3g em G1 = aquecido (+5%), 4+g = on fire (+10%). Dados: 3g→+6.2pp, 4g→+10.9pp."""
        if loser_goals_g1 >= 4:
            return 1.10
        if loser_goals_g1 == 3:
            return 1.05
        return 1.0

    def get_matchup_type_factor(self, loser: str, winner: str) -> float:
        loser_is_elite = loser in self.PLAYER_ELITE
        winner_is_elite = winner in self.PLAYER_ELITE or winner in self.WINNER_BOOST
        if loser_is_elite and winner_is_elite:
            return 1.15
        if loser_is_elite:
            return 1.10
        return 1.0

    def get_winner_factor(self, winner_name: str | None) -> float:
        if not winner_name:
            return 1.0
        if winner_name in self.WINNER_BOOST:
            return 1.12
        if winner_name in self.WINNER_PENALTY:
            return 0.85
        return 1.0

    def get_loser_team_factor(self, team: str | None) -> float:
        """Fator do time do perdedor. Spread real: 64.3% (Croatia) vs 42.4% (Al Ittihad)."""
        if not team:
            return 1.0
        if team in self.TEAM_BOOST:
            return 1.12
        if team in self.TEAM_PENALTY:
            return 0.86
        return 1.0

    def get_opp_team_factor(self, opp_team: str | None) -> float:
        """Fator do time oponente (defesa). Spread real: 62.7% (Netherlands) vs 38.1% (Benfica)."""
        if not opp_team:
            return 1.0
        if opp_team in self.OPP_TEAM_BOOST:
            return 1.12
        if opp_team in self.OPP_TEAM_PENALTY:
            return 0.84
        return 1.0

    def get_hour_factor(self, hour: int) -> float:
        return self.HOUR_FACTOR.get(hour, 1.0)

    def get_loser_g1_cat_factor(self, loser_goals_g1: int) -> float:
        """H2H-validated: shutout(0) vs neutral(1) vs on-fire(2+)."""
        if loser_goals_g1 == 0:
            return self.LOSER_G1_CAT_FACTOR["shutout"]
        elif loser_goals_g1 == 1:
            return self.LOSER_G1_CAT_FACTOR["neutral"]
        return self.LOSER_G1_CAT_FACTOR["on_fire"]

    def get_total_g1_cat_factor(self, total_g1: int) -> float:
        """H2H-validated: fechado(1-4) vs normal(5-6) vs aberto(7+)."""
        if total_g1 <= 4:
            return self.TOTAL_G1_CAT_FACTOR["fechado"]
        elif total_g1 <= 6:
            return self.TOTAL_G1_CAT_FACTOR["normal"]
        return self.TOTAL_G1_CAT_FACTOR["aberto"]

    def get_hour_period_factor(self, hour: int) -> float:
        """H2H-validated: manha(6-11) vs tarde(12-17) vs madrugada(0-5) vs noite(18-23)."""
        if 6 <= hour <= 11:
            return self.HOUR_PERIOD_FACTOR["manha"]
        elif 12 <= hour <= 17:
            return self.HOUR_PERIOD_FACTOR["tarde"]
        elif hour <= 5:
            return self.HOUR_PERIOD_FACTOR["madrugada"]
        return self.HOUR_PERIOD_FACTOR["noite"]

    def get_home_away_g1_factor(self, loser_was_home_g1: bool | None) -> float:
        """H2H-validated: loser perdeu em casa ou fora em G1."""
        if loser_was_home_g1 is None:
            return 1.0
        return self.HOME_AWAY_G1_FACTOR["home" if loser_was_home_g1 else "away"]

    def get_exact_score_factor(self, winner_goals: int, loser_goals: int) -> float:
        return self.EXACT_SCORE_FACTOR.get(f"{winner_goals}-{loser_goals}", 1.0)

    async def get_player_team_factor(
        self, player_name: str, team_name: str | None
    ) -> tuple[float, int]:
        """Fator baseado no combo JOGADOR + TIME especifico em G2.

        Usa PlayerTeamPreference (pré-computada) quando disponível,
        com fallback para query direta na Match.
        """
        if not team_name:
            return (1.0, 0)
        try:
            # Tentar tabela pré-computada primeiro (rápido)
            pref = await self.team_stats.get_player_team_preference(player_name, team_name)
            if pref and pref.times_used >= 5:
                combo_avg = pref.avg_goals_with
                # Buscar média geral do jogador para comparar
                key = f"player_general_{player_name}"
                stat = self._cache.get(key)
                if stat is None:
                    stat = await self.method_stats.get(key)
                if stat and stat.total_samples > 0 and stat.hit_rate_25 > 0:
                    global_rate = stat.hit_rate_25
                else:
                    global_rate = 0.515

                # combo_avg/2.5 como proxy de over25 rate vs global_rate
                combo_proxy = max(0.20, min(0.90, combo_avg / 5.0))
                factor = max(0.7, min(1.5, combo_proxy / global_rate)) if global_rate > 0 else 1.0

                logger.debug(
                    f"Player+team {player_name} c/ {team_name}: "
                    f"avg={combo_avg:.1f} proxy={combo_proxy:.1%} vs global {global_rate:.1%} "
                    f"→ fator {factor:.2f} (n={pref.times_used})"
                )
                return (factor, pref.times_used)

            # Fallback: query direta na Match (lento, mas preciso para dados legacy)
            from sqlalchemy import select
            from src.db.models import Match as MatchModel

            stmt = (
                select(MatchModel)
                .where(
                    MatchModel.is_return_match == True,  # noqa: E712
                    MatchModel.score_home.is_not(None),
                    MatchModel.score_away.is_not(None),
                )
                .where(
                    (
                        (MatchModel.player_home == player_name) & (MatchModel.team_home == team_name)
                    ) | (
                        (MatchModel.player_away == player_name) & (MatchModel.team_away == team_name)
                    )
                )
            )
            result = await self.matches.execute_query(stmt)
            matches = result.scalars().all()

            if len(matches) < 5:
                return (1.0, 0)

            total = 0
            over25 = 0
            for m in matches:
                goals = m.score_home if m.player_home == player_name else m.score_away
                if goals is None:
                    continue
                total += 1
                if goals > 2:
                    over25 += 1

            if total < 5:
                return (1.0, 0)

            combo_rate = over25 / total
            key = f"player_general_{player_name}"
            stat = self._cache.get(key)
            if stat is None:
                stat = await self.method_stats.get(key)
            if stat and stat.total_samples > 0 and stat.hit_rate_25 > 0:
                global_rate = stat.hit_rate_25
            else:
                global_rate = 0.515

            factor = max(0.7, min(1.5, combo_rate / global_rate)) if global_rate > 0 else 1.0

            logger.debug(
                f"Player+team {player_name} c/ {team_name} (fallback): "
                f"{combo_rate:.1%} vs global {global_rate:.1%} → fator {factor:.2f} (n={total})"
            )
            return (factor, total)

        except Exception as e:
            logger.warning(f"Erro ao buscar combo player+team: {e}")
            return (1.0, 0)

    async def get_streak_factor(self, losing_player: str) -> tuple[int, float]:
        """Calcula fator baseado no streak de derrotas consecutivas."""
        try:
            from sqlalchemy import select, or_
            from src.db.models import Match as MatchModel

            stmt = (
                select(MatchModel)
                .where(
                    MatchModel.is_return_match == False,  # noqa: E712
                    or_(
                        MatchModel.player_home == losing_player,
                        MatchModel.player_away == losing_player,
                    ),
                )
                .order_by(MatchModel.started_at.desc())
                .limit(10)
            )
            result = await self.matches.execute_query(stmt)
            recent_matches = result.scalars().all()
        except Exception as exc:
            logger.warning(f"Erro ao buscar streak de {losing_player}: {exc}")
            recent_matches = []

        streak = 0
        for match in recent_matches:
            if match.score_home is None or match.score_away is None:
                continue
            if match.player_home == losing_player:
                player_score = match.score_home
                opp_score = match.score_away
            else:
                player_score = match.score_away
                opp_score = match.score_home

            if player_score < opp_score:
                streak += 1
            else:
                break

        if streak <= 1:
            factor = 1.0
        elif streak == 2:
            factor = 0.97
        elif streak == 3:
            factor = 0.92
        elif streak == 4:
            factor = 0.88
        else:
            factor = 0.82

        logger.debug(f"Streak de {losing_player}: {streak} derrotas consecutivas \u2192 fator {factor:.2f}")
        return (streak, factor)

    async def get_session_momentum_factor(self, losing_player: str) -> float:
        """Momentum de sessao: analisa W/L nos ultimos 5 jogos G2 do perdedor.

        Se o jogador venceu 3+ dos ultimos 5 G2 = aquecido (+5%).
        Se perdeu 4+ dos ultimos 5 G2 = em tilt (-5%).
        Dados: baseado em observacao de que jogadores aquecidos mantem performance.
        """
        try:
            from sqlalchemy import select, or_
            from src.db.models import Match as MatchModel

            stmt = (
                select(MatchModel)
                .where(
                    MatchModel.is_return_match == True,  # noqa: E712
                    MatchModel.score_home.is_not(None),
                    or_(
                        MatchModel.player_home == losing_player,
                        MatchModel.player_away == losing_player,
                    ),
                )
                .order_by(MatchModel.started_at.desc())
                .limit(5)
            )
            result = await self.matches.execute_query(stmt)
            recent_g2 = result.scalars().all()
        except Exception:
            return 1.0

        if len(recent_g2) < 3:
            return 1.0

        wins = 0
        high_scoring = 0
        for m in recent_g2:
            pg = m.score_home if m.player_home == losing_player else m.score_away
            og = m.score_away if m.player_home == losing_player else m.score_home
            if pg is None or og is None:
                continue
            if pg > og:
                wins += 1
            if pg > 2:
                high_scoring += 1

        n = len(recent_g2)
        if wins >= 3 and high_scoring >= 3:
            return 1.08  # aquecido E marcando bem
        if wins >= 3:
            return 1.05  # aquecido
        if wins <= 1 and high_scoring <= 1:
            return 0.95  # em tilt
        return 1.0

    def get_enhanced_market_adjustment(self, odds_history: list) -> float:
        """Analise de movement de odds com decaimento temporal.

        - Queda rapida (>5% em poucos snapshots) = mercado confiante (+5%)
        - Queda lenta (>5% ao longo de muitos snapshots) = mercado entrando (+3%)
        - Subida (>5%) = mercado saindo (-3%)
        - Estavel = neutro (0%)
        """
        if not odds_history or len(odds_history) < 2:
            return 0.0

        first = odds_history[0] if isinstance(odds_history[0], (int, float)) else getattr(odds_history[0], "odds_value", 0)
        last = odds_history[-1] if isinstance(odds_history[-1], (int, float)) else getattr(odds_history[-1], "odds_value", 0)

        if not first or not last or first <= 0:
            return 0.0

        pct_change = (last - first) / first
        n_snapshots = len(odds_history)

        if pct_change <= -0.08:
            # Queda forte (>8%) = mercado muito confiante
            return 0.05
        elif pct_change <= -0.05:
            # Queda moderada — rapidez importa
            if n_snapshots <= 5:
                return 0.04  # rapida
            return 0.03      # lenta
        elif pct_change >= 0.08:
            # Subida forte = mercado fugindo
            return -0.04
        elif pct_change >= 0.05:
            return -0.03
        return 0.0

    async def check_new_player(self, player_name: str) -> tuple[bool, int]:
        """Verifica se jogador eh novo (< 15 jogos de volta conhecidos).

        Returns: (is_new, total_return_matches)
        """
        try:
            profile = await self.players.get_profile(player_name)
            if not profile:
                return (True, 0)
            total = profile.total_return_matches or 0
            return (total < 15, total)
        except Exception:
            return (True, 0)

    # ------------------------------------------------------------------
    # ML (Moneyline): probabilidade do perdedor vencer G2
    # Recalibrado com 9983 pares historicos (análise 2026-03-26)
    # ------------------------------------------------------------------

    # Taxa global: perdedor vence G2 em 37.1% dos casos
    ML_BASE_RATE: float = 0.371

    # Jogadores elite para ML — taxa de vitoria >= 45% em G2 (n>=30)
    # Spread: 43pp (KaLuBa 62% vs Kavviro 19%)
    ML_PLAYER_ELITE: dict[str, float] = {
        "KaLuBa": 0.622, "labotryas": 0.587, "DaVa": 0.583, "OG": 0.583,
        "Hyper": 0.568, "TikitakaMaster": 0.556, "Inquisitor": 0.537,
        "dor1an": 0.534, "Kray": 0.494, "Bomb1to": 0.493,
        "deroll": 0.463, "JKey": 0.460, "V1nx7": 0.452,
    }

    # Jogadores a evitar em ML — taxa <= 26% (n>=30)
    ML_PLAYER_AVOID: set[str] = {
        "Kavviro", "tohi4", "Nightxx", "Cira", "cappo",
        "MeLToSik", "gelennzz", "maksdh", "hrk", "ssstasonn",
        "Grellz", "Duka", "Nathana1l", "Cofi111",
    }

    # Cedentes: quando X vence G1, perdedor vence G2 em >= 45% (n>=30)
    # Spread: 57pp (hrk 75% vs hit 17%)
    ML_WINNER_CEDENTE: dict[str, float] = {
        "hrk": 0.747, "borees": 0.667, "RossFCDK": 0.598,
        "ssstasonn": 0.570, "gelennzz": 0.570, "Giox": 0.556,
        "SPACE": 0.511, "MeLToSik": 0.509, "Kodak": 0.506,
        "Rodja": 0.500, "dm1trena": 0.495, "Asura": 0.473,
        "llulle": 0.467, "Wboy": 0.460, "hotShot": 0.459,
    }

    # Trancas: quando X vence G1, perdedor vence G2 em <= 28% (n>=50)
    ML_WINNER_TRANCA: dict[str, float] = {
        "hit": 0.175, "TikitakaMaster": 0.191, "Morty": 0.224,
        "Flewless_phoenix": 0.226, "deroll": 0.231, "zoyir": 0.255,
        "nekishka": 0.255, "A1ose": 0.273, "Frantsuz": 0.286,
    }

    # H2H ML: combinacoes especificas com taxa muito alta (n>=10, rate>=65%)
    ML_H2H_BOOST: dict[tuple[str, str], float] = {
        ("OG", "hrk"): 0.944, ("DaVa", "MeLToSik"): 0.909,
        ("Simaponika", "Asura"): 0.818, ("sane4ek8", "dm1trena"): 0.800,
        ("LaikingDast", "KraftVK"): 0.786, ("TikitakaMaster", "maksdh"): 0.786,
        ("tonexo", "hrk"): 0.778, ("Jekunam", "dm1trena"): 0.769,
        ("sane4ek8", "RossFCDK"): 0.769, ("dor1an", "hrk"): 0.750,
        ("TikitakaMaster", "Nightxx"): 0.727, ("Asura", "ssstasonn"): 0.724,
        ("dor1an", "tonexo"): 0.692, ("Inquisitor", "Kodak"): 0.690,
        ("RuBIX", "GianniKid"): 0.667, ("gelennzz", "Aveaaaaa"): 0.667,
        ("DaVa", "Boulevard"): 0.650, ("Simaponika", "noeN"): 0.650,
        ("Kray", "Kodak"): 0.645, ("Kray", "hotShot"): 0.645,
    }

    # Fator por margem de derrota no G1 (ML) — calibrado 9983 pares
    # margem 1: 39.5%, 2: 36.9%, 3: 35.2%, 4: 34.6%, 5: 34.7%, 6+: 23.9%
    ML_MARGIN_FACTOR: dict[int, float] = {
        1: 1.065,  # 39.5% / 37.1%
        2: 0.995,  # 36.9% / 37.1%
        3: 0.949,  # 35.2% / 37.1%
        4: 0.933,  # 34.6% / 37.1%
        5: 0.935,  # 34.7% / 37.1%
        6: 0.644,  # 23.9% / 37.1%
    }

    # Fator por tipo de derrota (ML) — tight favorece virada
    # tight: 39.5%, medium: 36.3%, blowout: 33.4%
    ML_LOSS_TYPE_FACTOR: dict[str, float] = {
        "tight": 1.065,    # 39.5% / 37.1%
        "medium": 0.978,   # 36.3% / 37.1%
        "blowout": 0.900,  # 33.4% / 37.1%
    }

    # Fator por gols do perdedor em G1 (ML) — ativo = mais virada
    # 0: 34.7%, 1: 36.4%, 2: 38.0%, 3: 37.8%, 4: 38.9%, 5+: 41.9%
    ML_LOSER_GOALS_G1_FACTOR: dict[int, float] = {
        0: 0.935,  # 34.7% / 37.1%
        1: 0.981,  # 36.4% / 37.1%
        2: 1.024,  # 38.0% / 37.1%
        3: 1.019,  # 37.8% / 37.1%
        4: 1.049,  # 38.9% / 37.1%
        5: 1.129,  # 41.9% / 37.1%
    }

    # Times do perdedor com ML alta (rate >= 42%, n>=60)
    ML_LOSER_TEAM_BOOST: dict[str, float] = {
        "Netherlands": 1.47, "Al Hilal": 1.40, "Benfica": 1.36,
        "Morocco": 1.26, "Ukraine": 1.25, "Croatia": 1.19,
        "Denmark": 1.18, "A.Bilbao": 1.16, "Sporting": 1.15,
        "Real Madrid": 1.15, "Liverpool": 1.13,
    }

    # Times do perdedor a evitar ML (rate <= 30%, n>=60)
    ML_LOSER_TEAM_PENALTY: dict[str, float] = {
        "Hungary": 0.73, "Boca Juniors": 0.76, "Shakhtar": 0.79,
        "FC Porto": 0.80, "Crystal Palace": 0.81,
    }

    # Time do oponente que cede ML (rate >= 42%, n>=60)
    ML_OPP_TEAM_CEDE: dict[str, float] = {
        "Vitoria SC": 1.30, "Portugal": 1.28, "A.Bilbao": 1.27,
        "Netherlands": 1.24, "Crystal Palace": 1.23, "Al Ittihad": 1.21,
        "Croatia": 1.19, "FC Porto": 1.18, "FC Seoul": 1.17,
        "Sporting": 1.14, "Fenerbahce": 1.12, "Chelsea": 1.10,
    }

    # Time do oponente que tranca ML (rate <= 30%, n>=80)
    ML_OPP_TEAM_TRANCA: dict[str, float] = {
        "Benfica": 0.71, "River Plate": 0.72, "Napoli": 0.80,
        "Shakhtar": 0.80, "Dortmund": 0.82, "France": 0.84,
    }

    # Combos jogador+time explosivos para ML (rate >= 60%, n>=10)
    ML_PLAYER_TEAM_BOOST: dict[tuple[str, str], float] = {
        ("OG", "Netherlands"): 0.857, ("OG", "Portugal"): 0.818,
        ("OG", "France"): 0.800, ("OG", "Juventus"): 0.706,
        ("Kray", "Arsenal"): 0.700, ("Inquisitor", "Chelsea"): 0.700,
        ("dor1an", "England"): 0.700, ("V1nx7", "Real Madrid"): 0.688,
        ("Inquisitor", "Liverpool"): 0.619, ("dor1an", "Morocco"): 0.636,
    }

    # Fator por hora do dia (ML) — calibrado nos 9983 pares
    ML_HOUR_FACTOR: dict[int, float] = {
        0: 0.96, 1: 0.90, 2: 0.98, 3: 1.10, 4: 1.05, 5: 1.12,
        6: 1.10, 7: 1.10, 8: 1.08, 9: 1.07, 10: 1.05, 11: 1.06,
        12: 0.95, 13: 0.94, 14: 0.97, 15: 0.92, 16: 0.96, 17: 0.95,
        18: 1.08, 19: 0.96, 20: 0.98, 21: 0.93, 22: 0.90, 23: 0.97,
    }

    def _compute_ml_true_prob(
        self,
        losing_player: str,
        opponent_player: str,
        loser_team: str | None,
        opponent_team: str | None,
        loss_margin: int,
        loser_goals_g1: int,
        game1_score_winner: int,
        game1_score_loser: int,
        hour: int,
        player_flag: str,
        streak_val: int,
        loss_type: str = "medium",
        db_stats: dict | None = None,
    ) -> float:
        """Calcula probabilidade real do perdedor vencer o G2.

        12 layers calibrados em 9983 pares historicos (2026-03-26).
        Hipotese: perdedor do G1 tem handicap oculto que favorece vitoria no G2.

        Usa listas hardcoded como base, mas consulta method_stats do banco
        (via db_stats) para dados dinamicos que se atualizam com o backtest.
        """
        tp = self.ML_BASE_RATE  # 37.1%
        MIN_ML_SAMPLE = 10  # minimo de jogos para usar dados do banco

        # Layer 1: H2H especifico (fator MAIS forte — spread ate 57pp)
        # Primeiro checa hardcoded, depois banco
        h2h_key = (losing_player, opponent_player)
        h2h_applied = False
        if h2h_key in self.ML_H2H_BOOST:
            h2h_rate = self.ML_H2H_BOOST[h2h_key]
            tp = tp * 0.3 + h2h_rate * 0.7
            h2h_applied = True
        elif db_stats:
            db_h2h = db_stats.get(f"ml_h2h_{losing_player}_vs_{opponent_player}")
            if db_h2h and db_h2h.total_samples >= MIN_ML_SAMPLE:
                db_rate = db_h2h.hit_rate_25  # hit_rate_25 armazena ml_win_rate
                if db_rate >= 0.50:  # so usar se favoravel
                    tp = tp * 0.3 + db_rate * 0.7
                    h2h_applied = True

        if not h2h_applied:
            # Layer 2: Perfil do jogador perdedor (spread 43pp)
            # Hardcoded primeiro, fallback para banco
            if losing_player in self.ML_PLAYER_ELITE:
                player_rate = self.ML_PLAYER_ELITE[losing_player]
                tp = tp * (player_rate / self.ML_BASE_RATE) ** 0.6
            elif losing_player in self.ML_PLAYER_AVOID:
                tp *= 0.75
            elif db_stats:
                db_player = db_stats.get(f"ml_player_{losing_player}")
                if db_player and db_player.total_samples >= MIN_ML_SAMPLE:
                    db_rate = db_player.hit_rate_25  # ml_win_rate
                    if db_rate >= 0.45:
                        tp = tp * (db_rate / self.ML_BASE_RATE) ** 0.5
                    elif db_rate <= 0.25:
                        tp *= 0.80

        # Layer 3: Perfil do vencedor — cedente vs tranca (spread 57pp!)
        # Hardcoded primeiro, fallback para banco
        if opponent_player in self.ML_WINNER_CEDENTE:
            cedente_rate = self.ML_WINNER_CEDENTE[opponent_player]
            tp = tp * (cedente_rate / self.ML_BASE_RATE) ** 0.5
        elif opponent_player in self.ML_WINNER_TRANCA:
            tranca_rate = self.ML_WINNER_TRANCA[opponent_player]
            tp = tp * (tranca_rate / self.ML_BASE_RATE) ** 0.5
        elif db_stats:
            db_cedente = db_stats.get(f"ml_cedente_{opponent_player}")
            if db_cedente and db_cedente.total_samples >= 15:
                ced_rate = db_cedente.hit_rate_25  # cedente_rate
                if ced_rate >= 0.45:
                    tp = tp * (ced_rate / self.ML_BASE_RATE) ** 0.4
                elif ced_rate <= 0.28:
                    tp = tp * (ced_rate / self.ML_BASE_RATE) ** 0.4

        # Layer 4: Tipo de derrota (tight favorece virada)
        tp *= self.ML_LOSS_TYPE_FACTOR.get(loss_type, 1.0)

        # Layer 5: Margem de derrota (1g = melhor, 6g+ = muito pior)
        margin_capped = min(max(loss_margin, 1), 6)
        tp *= self.ML_MARGIN_FACTOR.get(margin_capped, 1.0)

        # Layer 6: Gols do perdedor em G1 (ativo = mais chance de virar)
        lg1_key = min(loser_goals_g1, 5)
        tp *= self.ML_LOSER_GOALS_G1_FACTOR.get(lg1_key, 1.0)

        # Layer 7: Hora do dia (spread 8pp)
        tp *= self.ML_HOUR_FACTOR.get(hour, 1.0)

        # Layer 8: Combo jogador+time (spread ate 48pp — OG+Netherlands 85.7%)
        pt_key = (losing_player, loser_team) if loser_team else None
        if pt_key and pt_key in self.ML_PLAYER_TEAM_BOOST:
            combo_rate = self.ML_PLAYER_TEAM_BOOST[pt_key]
            tp = tp * 0.5 + combo_rate * 0.5  # peso 50% combo
        else:
            # Layer 9: Time do perdedor (calibrado ML-especifico)
            if loser_team:
                if loser_team in self.ML_LOSER_TEAM_BOOST:
                    tp *= self.ML_LOSER_TEAM_BOOST[loser_team]
                elif loser_team in self.ML_LOSER_TEAM_PENALTY:
                    tp *= self.ML_LOSER_TEAM_PENALTY[loser_team]

        # Layer 10: Time do oponente (defesa que cede/tranca — ML-especifico)
        if opponent_team:
            if opponent_team in self.ML_OPP_TEAM_CEDE:
                tp *= self.ML_OPP_TEAM_CEDE[opponent_team]
            elif opponent_team in self.ML_OPP_TEAM_TRANCA:
                tp *= self.ML_OPP_TEAM_TRANCA[opponent_team]

        # Layer 11: Streak (ML é mais sensível — jogador em tilt perde mais)
        if streak_val >= 5:
            tp *= 0.70
        elif streak_val >= 3:
            tp *= 0.85
        elif streak_val >= 2:
            tp *= 0.93

        # Layer 12: Elite vs Cedente (combinacao explosiva)
        loser_elite = losing_player in self.ML_PLAYER_ELITE
        winner_cedente = opponent_player in self.ML_WINNER_CEDENTE
        if loser_elite and winner_cedente:
            tp *= 1.12

        # Clamp
        return max(0.05, min(0.95, tp))

    # ------------------------------------------------------------------
    # Probability layers (mantidos para compatibilidade, mas agora usam cache)
    # ------------------------------------------------------------------

    async def get_base_probability(self) -> tuple[float, float, float, int]:
        """Global hit rate for all return matches after a loss."""
        stat = self._cache.get("global")
        if stat is None:
            stat = await self.method_stats.get("global")
            if stat:
                self._cache.put("global", stat)
        if not stat or stat.total_samples == 0:
            return (0.50, 0.35, 0.20, 0)
        return (stat.hit_rate_25, stat.hit_rate_35, stat.hit_rate_45, stat.total_samples)

    async def get_loss_type_probability(self, loss_type: str) -> tuple[float, float, float, int]:
        """Hit rate segmented by loss type."""
        key = f"loss_{loss_type}"
        stat = self._cache.get(key)
        if stat is None:
            stat = await self.method_stats.get(key)
            if stat:
                self._cache.put(key, stat)
        if not stat or stat.total_samples < 10:
            base = self._cache.get("global")
            if base is None:
                base = await self.method_stats.get("global")
            r25 = base.hit_rate_25 if base and base.total_samples > 0 else 0.50
            r35 = base.hit_rate_35 if base and base.total_samples > 0 else 0.35
            r45 = base.hit_rate_45 if base and base.total_samples > 0 else 0.20
            n = stat.total_samples if stat else 0
            return (r25, r35, r45, n)
        return (stat.hit_rate_25, stat.hit_rate_35, stat.hit_rate_45, stat.total_samples)

    async def get_player_probability(self, player_name: str) -> tuple[float, float, float, int]:
        """Hit rate for a specific player after losses."""
        key = f"player_general_{player_name}"
        stat = self._cache.get(key)
        if stat is None:
            stat = await self.method_stats.get(key)
            if stat:
                self._cache.put(key, stat)
        if stat and stat.total_samples >= settings.min_player_sample:
            r25 = bayesian_update(int(0.50 * 10), 10, stat.over25_hits, stat.total_samples)
            r35 = bayesian_update(int(0.35 * 10), 10, stat.over35_hits, stat.total_samples)
            r45 = bayesian_update(int(0.20 * 10), 10, stat.over45_hits, stat.total_samples)
            return (r25, r35, r45, stat.total_samples)
        try:
            profile = await self.players.get_profile(player_name)
            if profile and profile.total_return_matches >= settings.min_player_sample:
                r25 = bayesian_update(5, 10, profile.over25_after_loss, profile.total_return_matches)
                r35 = bayesian_update(3, 10, profile.over35_after_loss, profile.total_return_matches)
                return (r25, r35, 0.20, profile.total_return_matches)
            n = profile.total_return_matches if profile else 0
        except Exception:
            n = 0
        return (0.50, 0.35, 0.20, n)

    async def get_recent_form_probability(self, player_name: str) -> tuple[float, float, float, int]:
        """Hit rate based on last 20 pairs for this player."""
        key = f"recent_form_{player_name}"
        stat = self._cache.get(key)
        if stat is None:
            stat = await self.method_stats.get(key)
            if stat:
                self._cache.put(key, stat)
        if stat and stat.total_samples >= 10:
            r25 = bayesian_update(5, 10, stat.over25_hits, stat.total_samples)
            r35 = bayesian_update(3, 10, stat.over35_hits, stat.total_samples)
            r45 = bayesian_update(2, 10, stat.over45_hits, stat.total_samples)
            return (r25, r35, r45, stat.total_samples)
        n = stat.total_samples if stat else 0
        return (0.50, 0.35, 0.20, n)

    async def get_h2h_probability(self, loser: str, opponent: str | None) -> tuple[float, float, float, int]:
        """Taxa do perdedor vs este oponente especifico."""
        MIN = settings.min_h2h_sample
        if opponent:
            key = f"h2h_{loser}_vs_{opponent}"
            stat = self._cache.get(key)
            if stat is None:
                stat = await self.method_stats.get(key)
                if stat:
                    self._cache.put(key, stat)
            if stat and stat.total_samples >= MIN:
                r25 = bayesian_update(2, 4, stat.over25_hits, stat.total_samples)
                r35 = bayesian_update(1, 4, stat.over35_hits, stat.total_samples)
                r45 = bayesian_update(1, 4, stat.over45_hits, stat.total_samples)
                return (r25, r35, r45, stat.total_samples)
        return (0.50, 0.35, 0.20, 0)

    async def get_y_post_win_probability(self, opponent: str | None) -> tuple[float, float, float, int]:
        """Taxa de over do perdedor quando Y (oponente) acabou de ganhar G1."""
        if not opponent:
            return (0.50, 0.35, 0.20, 0)
        key = f"y_post_win_{opponent}"
        stat = self._cache.get(key)
        if stat is None:
            stat = await self.method_stats.get(key)
            if stat:
                self._cache.put(key, stat)
        if stat and stat.total_samples >= 10:
            r25 = bayesian_update(5, 10, stat.over25_hits, stat.total_samples)
            r35 = bayesian_update(3, 10, stat.over35_hits, stat.total_samples)
            r45 = bayesian_update(2, 10, stat.over45_hits, stat.total_samples)
            return (r25, r35, r45, stat.total_samples)
        return (0.50, 0.35, 0.20, 0)

    async def get_time_slot_probability(self, hour: int) -> tuple[float, float, float, int]:
        """Hit rate for this time slot (6h buckets)."""
        slot = f"{(hour // 6) * 6:02d}-{(hour // 6) * 6 + 6:02d}h"
        key = f"time_{slot}"
        stat = self._cache.get(key)
        if stat is None:
            stat = await self.method_stats.get(key)
            if stat:
                self._cache.put(key, stat)
        if not stat or stat.total_samples < 10:
            base = self._cache.get("global")
            if base is None:
                base = await self.method_stats.get("global")
            r25 = base.hit_rate_25 if base and base.total_samples > 0 else 0.50
            r35 = base.hit_rate_35 if base and base.total_samples > 0 else 0.35
            r45 = base.hit_rate_45 if base and base.total_samples > 0 else 0.20
            n = stat.total_samples if stat else 0
            return (r25, r35, r45, n)
        return (stat.hit_rate_25, stat.hit_rate_35, stat.hit_rate_45, stat.total_samples)

    async def get_team_probability(
        self,
        team_name: str | None,
        opponent_team: str | None,
        player_name: str | None = None,
    ) -> tuple[float, int]:
        """Hit rate for a team, matchup, or player+team combo.

        Prioridade:
        1. Matchup especifico (team vs opponent) se n >= min_team_sample
        2. Player+team combo (PlayerTeamPreference) se n >= 5
        3. Team generico se n >= min_team_sample
        4. Default 0.50
        """
        if not team_name:
            return (0.50, 0)
        try:
            # 1. Matchup especifico
            if opponent_team:
                matchup = await self.team_stats.get_matchup_stats(team_name, opponent_team)
                if matchup and matchup.total_games >= settings.min_team_sample:
                    return (matchup.over25_rate, matchup.total_games)

            # 2. Player+team combo (mais valioso que time generico)
            if player_name:
                pref = await self.team_stats.get_player_team_preference(player_name, team_name)
                if pref and pref.times_used >= 5:
                    # avg_goals_with é média de gols do jogador com este time
                    # Converter para proxy de over25_rate: P(goals > 2)
                    # Usando relação empírica: se média é X, P(>2) ≈ clip(X/5, 0.2, 0.9)
                    avg = pref.avg_goals_with
                    over25_proxy = max(0.20, min(0.90, avg / 5.0))
                    return (over25_proxy, pref.times_used)

            # 3. Team generico
            team_stat = await self.team_stats.get_or_create(team_name)
            if team_stat.total_games >= settings.min_team_sample:
                return (team_stat.over25_rate, team_stat.total_games)

            return (0.50, team_stat.total_games)
        except Exception:
            return (0.50, 0)

    async def get_market_adjustment(self, odds_history: list) -> float:
        """Adjustment based on odds movement direction."""
        if len(odds_history) < 2:
            return 0.0
        first = odds_history[0].odds_value if hasattr(odds_history[0], "odds_value") else odds_history[0]
        last = odds_history[-1].odds_value if hasattr(odds_history[-1], "odds_value") else odds_history[-1]
        if last < first * 0.95:
            return 0.03
        elif last > first * 1.05:
            return -0.03
        return 0.0

    # ------------------------------------------------------------------
    # Regime
    # ------------------------------------------------------------------

    async def check_regime(self) -> dict:
        """Check if method is performing within expectations.

        Uses best_line_hits (hit pela linha que foi alertada) em vez de
        over25_hit fixo, para nao penalizar alertas de O1.5 que acertam
        mas nao batem O2.5.
        """
        try:
            recent = await self.alerts.get_period_stats(days=settings.regime_window)
            validated = recent.get("validated", 0)
            if validated < 20:
                return {"status": "HEALTHY", "z_score": 0.0, "recent_rate": 0.0,
                        "message": "Insufficient data", "action": "Collect more data"}

            best_hits = recent.get("best_line_hits", 0)
            recent_rate = best_hits / validated
            # Taxa historica esperada: ~60% (baseline do metodo)
            historical_rate = 0.60

            result = detect_regime_change(
                recent_hits=best_hits,
                recent_total=validated,
                historical_rate=historical_rate,
                z_threshold_warning=settings.regime_warning_z,
                z_threshold_degraded=settings.regime_degraded_z,
            )
            return result
        except Exception:
            return {"status": "HEALTHY", "z_score": 0.0, "recent_rate": 0.0,
                    "message": "Error checking regime", "action": "Continue"}

    # ------------------------------------------------------------------
    # Cold start
    # ------------------------------------------------------------------

    async def is_cold_start_complete(self) -> bool:
        """Returns True if we have 90+ days of data."""
        progress = await self.get_cold_start_progress()
        return progress.is_complete

    async def get_cold_start_progress(self) -> ColdStartProgress:
        """Compute cold start collection progress."""
        from datetime import datetime, timezone
        try:
            oldest = await self.matches.get_oldest_match_date()
            if not oldest:
                return ColdStartProgress(
                    days_collected=0, cold_start_days=settings.cold_start_days,
                    total_games=0, total_pairs=0, unique_players=0, unique_teams=0,
                    is_complete=False, activation_date="calculando...",
                )

            oldest_utc = oldest.replace(tzinfo=timezone.utc) if oldest.tzinfo is None else oldest
            days = (datetime.now(timezone.utc) - oldest_utc).days
            total_games = await self.matches.count_total()
            total_pairs = await self.matches.count_pairs()
            unique_players = await self.players.count_unique()
            is_complete = days >= settings.cold_start_days

            from datetime import timedelta
            activation = oldest + timedelta(days=settings.cold_start_days)
            activation_str = activation.strftime("%Y-%m-%d")

            return ColdStartProgress(
                days_collected=days,
                cold_start_days=settings.cold_start_days,
                total_games=total_games,
                total_pairs=total_pairs,
                unique_players=unique_players,
                unique_teams=0,
                is_complete=is_complete,
                activation_date=activation_str,
            )
        except Exception as e:
            logger.warning(f"Cold start check failed: {e}")
            try:
                oldest = await self.matches.get_oldest_match_date()
                if oldest:
                    from datetime import timedelta
                    days = (datetime.now(timezone.utc) - (oldest.replace(tzinfo=timezone.utc) if oldest.tzinfo is None else oldest)).days
                    is_complete = days >= settings.cold_start_days
                    logger.info(f"Cold start retry ok: {days} dias coletados, complete={is_complete}")
                    return ColdStartProgress(
                        days_collected=days, cold_start_days=settings.cold_start_days,
                        total_games=0, total_pairs=0, unique_players=0, unique_teams=0,
                        is_complete=is_complete, activation_date="recuperado",
                    )
            except Exception as e2:
                logger.warning(f"Cold start retry also failed: {e2}")
            return ColdStartProgress(
                days_collected=0, cold_start_days=settings.cold_start_days,
                total_games=0, total_pairs=0, unique_players=0, unique_teams=0,
                is_complete=False, activation_date="erro",
            )

    # ------------------------------------------------------------------
    # Post-validation update (invalida cache ao atualizar stats)
    # ------------------------------------------------------------------

    async def update_after_validation(
        self,
        alert_id: int,
        alert,
        actual_goals: int,
    ) -> None:
        """Update all stats tables after a return match result is known.

        NOTE: alert.validate() é chamado pelo Validator na sua própria session.
        Este método apenas atualiza stats globais e cache.
        """
        over25_hit = actual_goals > 2
        over35_hit = actual_goals > 3
        over45_hit = actual_goals > 4

        try:
            # Update global stats
            global_stat = await self.method_stats.get_or_create("global")
            new_total = global_stat.total_samples + 1
            new_25 = global_stat.over25_hits + (1 if over25_hit else 0)
            new_35 = global_stat.over35_hits + (1 if over35_hit else 0)
            new_45 = global_stat.over45_hits + (1 if over45_hit else 0)
            await self.method_stats.upsert("global", {
                "total_samples": new_total,
                "over25_hits": new_25,
                "over35_hits": new_35,
                "over45_hits": new_45,
                "hit_rate_25": new_25 / new_total,
                "hit_rate_35": new_35 / new_total,
                "hit_rate_45": new_45 / new_total,
            })

            # Update loss type
            loss_key = f"loss_{getattr(alert, 'loss_type', 'tight')}"
            await self.method_stats.update_stat(
                loss_key,
                over25_hit=over25_hit,
                over35_hit=over35_hit,
                over45_hit=over45_hit,
                goals=float(actual_goals),
            )

            # Update player
            await self.players.update_stats_after_validation(
                name=getattr(alert, 'losing_player', ''),
                over25_hit=over25_hit,
                over35_hit=over35_hit,
                actual_goals=actual_goals,
                loss_type=getattr(alert, 'loss_type', 'tight'),
            )

            # Update team stats for G2 (return match)
            try:
                match = await self.matches.get_by_id(getattr(alert, 'match_id', 0))
                if match and match.is_return_match:
                    h_team, a_team = match.team_home, match.team_away
                    h_goals = match.score_home or 0
                    a_goals = match.score_away or 0
                    if h_team:
                        await self.team_stats.update_stats(h_team, goals_scored=h_goals, goals_conceded=a_goals)
                        await self.team_stats.update_player_team_preference(match.player_home, h_team, h_goals)
                    if a_team:
                        await self.team_stats.update_stats(a_team, goals_scored=a_goals, goals_conceded=h_goals)
                        await self.team_stats.update_player_team_preference(match.player_away, a_team, a_goals)
                    if h_team and a_team:
                        await self.team_stats.update_matchup_stats(h_team, a_team, h_goals + a_goals)
            except Exception as e:
                logger.debug(f"Team stats G2 update failed (non-critical): {e}")

            logger.info(
                f"Stats updated for alert {alert_id}: "
                f"goals={actual_goals}, o25={'OK' if over25_hit else 'X'}, "
                f"o35={'OK' if over35_hit else 'X'}, o45={'OK' if over45_hit else 'X'}"
            )
        except Exception as e:
            logger.warning(f"Stats update_after_validation failed for alert {alert_id}: {e}")

        # Invalidar cache independente do sucesso do DB
        self._cache.invalidate("global")
        loss_key = f"loss_{getattr(alert, 'loss_type', 'tight')}"
        self._cache.invalidate(loss_key)
        player_key = f"player_general_{getattr(alert, 'losing_player', '')}"
        self._cache.invalidate(player_key)
        self._cache.invalidate(f"recent_form_{getattr(alert, 'losing_player', '')}")
