"""
Pure mathematical/probability functions for FIFA Bet Alert.

All functions in this module are PURE: no I/O, no database, no state.
Fully deterministic and testable.

See PRD Section 12 - "Motor Estatistico e de Probabilidade" for full context.
"""

import math


# ---------------------------------------------------------------------------
# 12.2 - Implied Probability
# ---------------------------------------------------------------------------

def implied_probability(decimal_odds: float) -> float:
    """Convert decimal odds to implied probability.

    Examples:
        @1.85 -> 0.5405
        @2.00 -> 0.5000
        @1.50 -> 0.6667

    Raises:
        ValueError: If odds <= 0.
    """
    if decimal_odds <= 0:
        raise ValueError(f"Decimal odds must be positive, got {decimal_odds}")
    if decimal_odds < 1.0:
        raise ValueError(f"Decimal odds must be >= 1.0, got {decimal_odds}")
    return 1.0 / decimal_odds


# ---------------------------------------------------------------------------
# 12.3 - True Probability (composite from 6 layers)
# ---------------------------------------------------------------------------

def calculate_true_probability(
    p_base: float,
    p_loss_type: float,
    p_player: float,
    p_time_slot: float,
    p_team: float,
    p_market_adj: float,
    n_player: int,
    n_team: int,
    min_sample: int = 10,
) -> float:
    """Composite probability from 6 layers with dynamic weights.

    Weights (from PRD 12.3):
        - Loss type (Camada 2): 0.25 fixed
        - Time slot  (Camada 4): 0.10 fixed
        - Market adj (Camada 6): 0.05 fixed (applied to p_base + p_market_adj)
        - Player     (Camada 3): 0 .. 0.25 scaling with n_player / min_sample
        - Team       (Camada 5): 0 .. 0.10 scaling with n_team / min_sample
        - Base       (Camada 1): absorbs unused weight from player & team

    The base weight formula from the PRD:
        w_base = 1.0 - 0.25 - w_player - 0.10 - w_team - 0.05

    Args:
        p_base: Global hit-rate for return matches after a loss.
        p_loss_type: Hit-rate for this specific loss type (tight/medium/blowout).
        p_player: Hit-rate for this specific player after losses.
        p_time_slot: Hit-rate for this time slot.
        p_team: Hit-rate for this team / matchup.
        p_market_adj: Market adjustment (-0.03 to +0.03).
        n_player: Number of samples for this player.
        n_team: Number of samples for this team.
        min_sample: Minimum samples for full weight (default 10).

    Returns:
        Clipped probability in [0.0, 1.0].
    """
    # Dynamic weights scaling with sample size
    w_player = min(0.25, 0.25 * (n_player / min_sample)) if min_sample > 0 else 0.0
    w_team = min(0.10, 0.10 * (n_team / min_sample)) if min_sample > 0 else 0.0

    # Base absorbs what player and team don't use
    # Total probability weights: loss_type(0.25) + time_slot(0.10) = 0.35 fixed
    # Remaining 0.65 split among base(up to 0.65), player(up to 0.25), team(up to 0.10)
    # Market adjustment is additive (not a weighted layer) to avoid double-counting p_base
    w_base = 1.0 - 0.25 - w_player - 0.10 - w_team

    p_true = (
        w_base * p_base
        + 0.25 * p_loss_type
        + w_player * p_player
        + 0.10 * p_time_slot
        + w_team * p_team
        + p_market_adj  # additive adjustment (-0.03 to +0.03)
    )

    return max(0.0, min(1.0, p_true))


# ---------------------------------------------------------------------------
# 12.4 - Expected Value
# ---------------------------------------------------------------------------

def expected_value(true_prob: float, decimal_odds: float) -> float:
    """Expected Value for a 1-unit stake.

    EV = (prob * profit) - ((1 - prob) * stake)
    For a 1 unit stake, profit = decimal_odds - 1.

    Examples:
        true_prob=0.68, odds=1.85 -> EV ~ +0.258
        true_prob=0.50, odds=1.85 -> EV ~ -0.075
    """
    profit = decimal_odds - 1.0
    return (true_prob * profit) - ((1.0 - true_prob) * 1.0)


# ---------------------------------------------------------------------------
# 12.5 - Edge
# ---------------------------------------------------------------------------

def edge(true_prob: float, decimal_odds: float) -> float:
    """Edge = true probability - implied probability.

    Positive edge means we have an advantage over the bookmaker.

    Examples:
        true_prob=0.68, odds=1.85 -> edge ~ +0.1395
    """
    return true_prob - implied_probability(decimal_odds)


# ---------------------------------------------------------------------------
# 12.6 - Kelly Criterion
# ---------------------------------------------------------------------------

def kelly_fraction(true_prob: float, decimal_odds: float) -> float:
    """Full Kelly fraction: (b*p - q) / b.

    Where b = decimal_odds - 1, p = true_prob, q = 1 - p.
    Result is never negative.

    Examples:
        true_prob=0.68, odds=1.85 -> ~0.304 (30.4% of bankroll)
    """
    b = decimal_odds - 1.0
    if b <= 0:
        return 0.0
    p = true_prob
    q = 1.0 - p
    k = (b * p - q) / b
    return max(0.0, k)


def fractional_kelly(
    true_prob: float,
    decimal_odds: float,
    fraction: float = 0.25,
) -> float:
    """Conservative fractional Kelly (default 25%).

    Reduces variance at the cost of slightly lower expected growth.

    Examples:
        true_prob=0.68, odds=1.85, fraction=0.25 -> ~0.076 (7.6%)
    """
    return kelly_fraction(true_prob, decimal_odds) * fraction


# ---------------------------------------------------------------------------
# 12.9 - Wilson Confidence Interval
# ---------------------------------------------------------------------------

def wilson_confidence_interval(
    successes: int,
    total: int,
    z: float = 1.96,
) -> tuple[float, float]:
    """Wilson score interval - better than normal approximation for small samples.

    Args:
        successes: Number of successes.
        total: Total number of trials.
        z: Z-score for confidence level (1.96 = 95%).

    Returns:
        (lower, upper) bounds of the confidence interval.
    """
    if total == 0:
        return (0.0, 1.0)

    p_hat = successes / total
    denominator = 1.0 + z ** 2 / total
    center = (p_hat + z ** 2 / (2.0 * total)) / denominator
    margin = (z / denominator) * math.sqrt(
        (p_hat * (1.0 - p_hat) / total) + (z ** 2 / (4.0 * total ** 2))
    )

    lower = max(0.0, center - margin)
    upper = min(1.0, center + margin)
    return (lower, upper)


# ---------------------------------------------------------------------------
# 12.10 - Bayesian Update
# ---------------------------------------------------------------------------

def bayesian_update(
    prior_successes: int,
    prior_total: int,
    new_successes: int,
    new_total: int,
) -> float:
    """Bayesian update with a weak Beta(2, 2) prior.

    The prior represents a gentle belief towards 50% before observing data.
    As data accumulates, the posterior converges to the true rate.

    Args:
        prior_successes: Successes from prior data.
        prior_total: Total trials from prior data.
        new_successes: Successes from new data.
        new_total: Total trials from new data.

    Returns:
        Posterior mean probability.
    """
    alpha = prior_successes + new_successes + 2  # +2 = Beta prior alpha
    beta = (prior_total - prior_successes) + (new_total - new_successes) + 2  # +2 = Beta prior beta
    return alpha / (alpha + beta)


# ---------------------------------------------------------------------------
# Loss Classification
# ---------------------------------------------------------------------------

def classify_loss(score_winner: int, score_loser: int) -> str:
    """Classify a loss by goal difference and total goals.

    Categories:
        - 'tight':   1 goal difference
        - 'medium':  2 goals difference
        - 'blowout': 3+ goals difference

    If total goals > 5, append '_open' (e.g., 'medium_open').

    Args:
        score_winner: Goals scored by the winner.
        score_loser: Goals scored by the loser.

    Returns:
        Loss classification string.
    """
    diff = score_winner - score_loser
    if diff <= 0:
        raise ValueError(
            f"classify_loss requires a loss (winner > loser), "
            f"got {score_winner}-{score_loser}"
        )
    total = score_winner + score_loser

    if diff == 1:
        label = "tight"
    elif diff == 2:
        label = "medium"
    else:
        label = "blowout"

    if total > 5:
        label += "_open"

    return label


# ---------------------------------------------------------------------------
# 12.8 - Star Rating
# ---------------------------------------------------------------------------

def star_rating(edge_val: float, ev_val: float) -> int:
    """Rate an opportunity from 1-5 stars based on edge and EV.

    Thresholds (from PRD 12.8):
        1 star:  edge 5-8%,   EV 3-5%
        2 stars: edge 8-12%,  EV 5-10%
        3 stars: edge 12-18%, EV 10-15%
        4 stars: edge 18-25%, EV 15-25%
        5 stars: edge >25%,   EV >25%

    Uses the LOWER of the edge-based and EV-based ratings.
    Returns 0 if below minimum thresholds (edge < 5% or EV < 3%).

    Args:
        edge_val: Edge as a fraction (e.g., 0.14 = 14%).
        ev_val: Expected value as a fraction (e.g., 0.25 = 25%).

    Returns:
        Star rating from 0 to 5.
    """
    # Edge-based rating
    if edge_val >= 0.25:
        edge_stars = 5
    elif edge_val >= 0.18:
        edge_stars = 4
    elif edge_val >= 0.12:
        edge_stars = 3
    elif edge_val >= 0.08:
        edge_stars = 2
    elif edge_val >= 0.05:
        edge_stars = 1
    else:
        edge_stars = 0

    # EV-based rating
    if ev_val >= 0.25:
        ev_stars = 5
    elif ev_val >= 0.15:
        ev_stars = 4
    elif ev_val >= 0.10:
        ev_stars = 3
    elif ev_val >= 0.05:
        ev_stars = 2
    elif ev_val >= 0.03:
        ev_stars = 1
    else:
        ev_stars = 0

    return min(edge_stars, ev_stars)


# ---------------------------------------------------------------------------
# 12.7 / 12.14 - Should Alert Decision
# ---------------------------------------------------------------------------

def should_alert(
    edge_val: float,
    ev_val: float,
    true_prob_conservative: float,
    odds: float,
    global_sample: int,
    regime_status: str,
    cold_start_complete: bool,
    min_edge: float = 0.05,
    min_ev: float = 0.03,
    min_true_prob: float = 0.55,
    min_odds: float = 1.40,
    max_odds: float = 4.00,
    min_global_sample: int = 500,
) -> tuple[bool, str]:
    """Decide whether to send an alert.

    All criteria must pass. Returns (should_alert, reason) where reason
    explains the decision.

    Args:
        edge_val: Computed edge (fraction).
        ev_val: Computed expected value (fraction).
        true_prob_conservative: Conservative true probability (lower bound of CI).
        odds: Current decimal odds.
        global_sample: Number of global samples collected.
        regime_status: 'HEALTHY', 'WARNING', or 'DEGRADED'.
        cold_start_complete: Whether the 90-day cold start is complete.
        min_edge: Minimum edge threshold (default 5%).
        min_ev: Minimum EV threshold (default 3%).
        min_true_prob: Minimum true probability (default 55%).
        min_odds: Minimum acceptable odds (default 1.40).
        max_odds: Maximum acceptable odds (default 4.00).
        min_global_sample: Minimum global sample size (default 500).

    Returns:
        Tuple of (bool, str) - whether to alert and the reason.
    """
    # Cold start must be complete
    if not cold_start_complete:
        return (False, "Cold start not complete - still collecting data")

    # Regime must not be DEGRADED
    if regime_status == "DEGRADED":
        return (False, f"Regime is DEGRADED - alerts paused for safety")

    # Global sample size
    if global_sample < min_global_sample:
        return (False, f"Insufficient global sample: {global_sample} < {min_global_sample}")

    # Odds range
    if odds < min_odds:
        return (False, f"Odds too low: {odds:.2f} < {min_odds:.2f}")
    if odds > max_odds:
        return (False, f"Odds too high: {odds:.2f} > {max_odds:.2f}")

    # Edge threshold
    if edge_val < min_edge:
        return (False, f"Edge too low: {edge_val:.2%} < {min_edge:.2%}")

    # EV threshold
    if ev_val < min_ev:
        return (False, f"EV too low: {ev_val:.2%} < {min_ev:.2%}")

    # True probability threshold
    if true_prob_conservative < min_true_prob:
        return (
            False,
            f"True probability too low: {true_prob_conservative:.2%} < {min_true_prob:.2%}",
        )

    # All criteria met
    stars = star_rating(edge_val, ev_val)
    return (
        True,
        f"All criteria met - Edge: {edge_val:.2%}, EV: {ev_val:.2%}, "
        f"Prob: {true_prob_conservative:.2%}, Odds: {odds:.2f}, "
        f"Stars: {stars}/5",
    )


# ---------------------------------------------------------------------------
# 12.11 - Regime Change Detection
# ---------------------------------------------------------------------------

def detect_regime_change(
    recent_hits: int,
    recent_total: int,
    historical_rate: float,
    z_threshold_warning: float = -1.5,
    z_threshold_degraded: float = -2.0,
) -> dict:
    """Detect if the method is performing significantly below historical rate.

    Uses a Z-test for proportions to compare recent performance against
    the historical baseline.

    Args:
        recent_hits: Number of hits in the recent window.
        recent_total: Total bets in the recent window.
        historical_rate: Historical hit rate (e.g., 0.65).
        z_threshold_warning: Z-score threshold for WARNING (default -1.5).
        z_threshold_degraded: Z-score threshold for DEGRADED (default -2.0).

    Returns:
        Dict with keys: status, z_score, recent_rate, message, action.
    """
    if recent_total == 0:
        return {
            "status": "HEALTHY",
            "z_score": 0.0,
            "recent_rate": 0.0,
            "message": "No recent data to evaluate",
            "action": "Continue collecting data",
        }

    recent_rate = recent_hits / recent_total

    # Standard error for proportion
    # Guard against edge cases where historical_rate is 0 or 1
    variance = historical_rate * (1.0 - historical_rate)
    if variance <= 0:
        # If historical rate is 0 or 1, we can't compute a meaningful z-score
        return {
            "status": "HEALTHY",
            "z_score": 0.0,
            "recent_rate": recent_rate,
            "message": f"Historical rate is extreme ({historical_rate:.1%}), z-test not applicable",
            "action": "Review historical data",
        }

    se = math.sqrt(variance / recent_total)
    z_score = (recent_rate - historical_rate) / se

    if z_score < z_threshold_degraded:
        return {
            "status": "DEGRADED",
            "z_score": z_score,
            "recent_rate": recent_rate,
            "message": (
                f"Method dropped from {historical_rate:.1%} to {recent_rate:.1%}"
            ),
            "action": "PAUSE alerts and investigate",
        }
    elif z_score < z_threshold_warning:
        return {
            "status": "WARNING",
            "z_score": z_score,
            "recent_rate": recent_rate,
            "message": (
                f"Downward trend: {recent_rate:.1%} vs historical {historical_rate:.1%}"
            ),
            "action": "Monitor closely",
        }
    else:
        return {
            "status": "HEALTHY",
            "z_score": z_score,
            "recent_rate": recent_rate,
            "message": (
                f"Method performing within expectations: {recent_rate:.1%}"
            ),
            "action": "Continue normal operation",
        }


# ---------------------------------------------------------------------------
# 12.12 - ROI Simulation
# ---------------------------------------------------------------------------

def simulate_roi(
    results: list[dict],
    stake_method: str = "flat",
    initial_bankroll: float = 1000.0,
    flat_stake: float = 10.0,
    kelly_frac: float = 0.25,
) -> dict:
    """Simulate ROI for a list of bet results.

    Args:
        results: List of dicts, each with keys:
            - 'odds' (float): Decimal odds.
            - 'hit' (bool): Whether the bet won.
            - 'true_prob' (float): Estimated true probability.
        stake_method: 'flat' or 'kelly'.
        initial_bankroll: Starting bankroll (default 1000).
        flat_stake: Stake per bet for flat method (default 10).
        kelly_frac: Kelly fraction for kelly method (default 0.25).

    Returns:
        Dict with: total_bets, wins, losses, win_rate, roi, profit_units,
                   max_drawdown, bankroll_final.
    """
    bankroll = initial_bankroll
    max_bankroll = bankroll
    max_drawdown = 0.0
    wins = 0
    losses = 0

    for result in results:
        odds = result["odds"]
        hit = result["hit"]
        true_prob = result["true_prob"]

        if stake_method == "kelly":
            stake = bankroll * fractional_kelly(true_prob, odds, kelly_frac)
        else:  # flat
            stake = flat_stake

        # Don't bet more than we have
        stake = min(stake, bankroll)

        if stake <= 0:
            continue

        if hit:
            bankroll += stake * (odds - 1.0)
            wins += 1
        else:
            bankroll -= stake
            losses += 1

        max_bankroll = max(max_bankroll, bankroll)
        if max_bankroll > 0:
            drawdown = (max_bankroll - bankroll) / max_bankroll
            max_drawdown = max(max_drawdown, drawdown)

    total = wins + losses
    return {
        "total_bets": total,
        "wins": wins,
        "losses": losses,
        "win_rate": wins / total if total > 0 else 0.0,
        "roi": (bankroll - initial_bankroll) / initial_bankroll if initial_bankroll > 0 else 0.0,
        "profit_units": bankroll - initial_bankroll,
        "max_drawdown": max_drawdown,
        "bankroll_final": bankroll,
    }
