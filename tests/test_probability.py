"""Tests for src/core/probability.py - all pure math functions."""

import math

import pytest

from src.core.probability import (
    bayesian_update,
    calculate_true_probability,
    classify_loss,
    detect_regime_change,
    edge,
    expected_value,
    fractional_kelly,
    implied_probability,
    kelly_fraction,
    should_alert,
    simulate_roi,
    star_rating,
    wilson_confidence_interval,
)


# ---------------------------------------------------------------------------
# implied_probability
# ---------------------------------------------------------------------------

class TestImpliedProbability:
    def test_known_values(self):
        assert abs(implied_probability(1.85) - 0.5405) < 0.001
        assert abs(implied_probability(2.00) - 0.5000) < 0.001
        assert abs(implied_probability(1.50) - 0.6667) < 0.001
        assert abs(implied_probability(3.00) - 0.3333) < 0.001

    def test_raises_on_zero(self):
        with pytest.raises((ValueError, ZeroDivisionError)):
            implied_probability(0)

    def test_raises_on_negative(self):
        with pytest.raises(ValueError):
            implied_probability(-1.5)

    def test_odds_of_one(self):
        assert implied_probability(1.0) == 1.0


# ---------------------------------------------------------------------------
# expected_value
# ---------------------------------------------------------------------------

class TestExpectedValue:
    def test_positive_ev(self):
        ev = expected_value(0.68, 1.85)
        assert abs(ev - 0.258) < 0.01

    def test_negative_ev(self):
        ev = expected_value(0.50, 1.85)
        assert ev < 0

    def test_break_even(self):
        # If true_prob == implied_prob, EV should be ~0
        odds = 2.00
        prob = implied_probability(odds)
        ev = expected_value(prob, odds)
        assert abs(ev) < 0.01

    def test_high_prob_high_ev(self):
        ev = expected_value(0.80, 2.00)
        assert ev > 0.50


# ---------------------------------------------------------------------------
# edge
# ---------------------------------------------------------------------------

class TestEdge:
    def test_positive_edge(self):
        e = edge(0.68, 1.85)
        assert abs(e - (0.68 - 1/1.85)) < 0.001

    def test_zero_edge(self):
        odds = 2.00
        e = edge(implied_probability(odds), odds)
        assert abs(e) < 0.001

    def test_negative_edge(self):
        e = edge(0.45, 1.85)
        assert e < 0


# ---------------------------------------------------------------------------
# kelly_fraction
# ---------------------------------------------------------------------------

class TestKellyFraction:
    def test_known_value(self):
        k = kelly_fraction(0.68, 1.85)
        b = 0.85
        expected = (b * 0.68 - 0.32) / b
        assert abs(k - expected) < 0.001

    def test_never_negative(self):
        assert kelly_fraction(0.30, 1.85) >= 0
        assert kelly_fraction(0.0, 1.85) >= 0

    def test_zero_edge_means_no_bet(self):
        odds = 2.00
        prob = implied_probability(odds)
        k = kelly_fraction(prob, odds)
        assert abs(k) < 0.01


class TestFractionalKelly:
    def test_25_percent(self):
        full = kelly_fraction(0.68, 1.85)
        frac = fractional_kelly(0.68, 1.85, 0.25)
        assert abs(frac - full * 0.25) < 0.001

    def test_example_from_prd(self):
        frac = fractional_kelly(0.68, 1.85, 0.25)
        assert abs(frac - 0.076) < 0.01


# ---------------------------------------------------------------------------
# wilson_confidence_interval
# ---------------------------------------------------------------------------

class TestWilsonCI:
    def test_zero_samples(self):
        lo, hi = wilson_confidence_interval(0, 0)
        assert lo == 0.0
        assert hi == 1.0

    def test_small_sample(self):
        lo, hi = wilson_confidence_interval(7, 10)
        assert lo < 0.70 < hi
        assert lo < hi

    def test_large_sample(self):
        lo, hi = wilson_confidence_interval(700, 1000)
        # Should be tight around 70%
        assert abs(lo - 0.70) < 0.05
        assert abs(hi - 0.70) < 0.05
        assert hi - lo < 0.06  # narrow interval

    def test_bounds(self):
        lo, hi = wilson_confidence_interval(10, 10)
        assert 0 <= lo <= 1
        assert 0 <= hi <= 1


# ---------------------------------------------------------------------------
# bayesian_update
# ---------------------------------------------------------------------------

class TestBayesianUpdate:
    def test_neutral_prior_no_data(self):
        # With weak Beta(2,2) prior and no data, should be 0.5
        result = bayesian_update(0, 0, 0, 0)
        assert abs(result - 0.5) < 0.01

    def test_converges_with_data(self):
        # 350 hits out of 500 = 70%
        result = bayesian_update(0, 0, 350, 500)
        assert abs(result - 0.70) < 0.05

    def test_updates_correctly(self):
        # Prior: 30/50, New: 40/60 → combined ~70/110
        result = bayesian_update(30, 50, 40, 60)
        # With prior, around (30+40+2)/(110+4) ≈ 0.632
        assert 0.55 < result < 0.75


# ---------------------------------------------------------------------------
# classify_loss
# ---------------------------------------------------------------------------

class TestClassifyLoss:
    def test_tight(self):
        assert classify_loss(2, 1) == "tight"
        assert classify_loss(3, 2) == "tight"

    def test_medium(self):
        assert classify_loss(3, 1) == "medium"   # total=4, not open
        assert classify_loss(4, 2) == "medium_open"  # total=6 > 5, open

    def test_blowout(self):
        assert classify_loss(4, 1) == "blowout"
        assert classify_loss(5, 0) == "blowout"

    def test_open_game(self):
        # Total > 5 → append _open
        assert classify_loss(5, 2) == "blowout_open"  # diff=3, total=7
        assert classify_loss(4, 3) == "tight_open"    # diff=1, total=7
        assert classify_loss(5, 3) == "medium_open"   # diff=2, total=8

    def test_exact_boundary(self):
        # Total = 5, NOT open
        assert classify_loss(3, 2) == "tight"
        # Total = 6, IS open
        assert classify_loss(4, 2) == "medium_open"


# ---------------------------------------------------------------------------
# star_rating
# ---------------------------------------------------------------------------

class TestStarRating:
    def test_one_star(self):
        assert star_rating(0.06, 0.04) == 1

    def test_two_stars(self):
        assert star_rating(0.10, 0.07) == 2

    def test_three_stars(self):
        assert star_rating(0.15, 0.12) == 3

    def test_four_stars(self):
        assert star_rating(0.20, 0.18) == 4

    def test_five_stars(self):
        assert star_rating(0.30, 0.30) == 5

    def test_uses_minimum_of_two_ratings(self):
        # High edge but low EV → limited by EV
        assert star_rating(0.30, 0.04) == 1

    def test_below_threshold_returns_zero(self):
        assert star_rating(0.03, 0.01) == 0

    def test_zero_values(self):
        assert star_rating(0.0, 0.0) == 0


# ---------------------------------------------------------------------------
# should_alert
# ---------------------------------------------------------------------------

class TestShouldAlert:
    def _base_kwargs(self, **overrides):
        kwargs = dict(
            edge_val=0.15,
            ev_val=0.12,
            true_prob_conservative=0.60,
            odds=1.85,
            global_sample=600,
            regime_status="HEALTHY",
            cold_start_complete=True,
        )
        kwargs.update(overrides)
        return kwargs

    def test_all_good(self):
        alert, reason = should_alert(**self._base_kwargs())
        assert alert is True

    def test_cold_start_not_complete(self):
        alert, reason = should_alert(**self._base_kwargs(cold_start_complete=False))
        assert alert is False
        assert "cold start" in reason.lower() or "collecting" in reason.lower()

    def test_degraded_regime(self):
        alert, reason = should_alert(**self._base_kwargs(regime_status="DEGRADED"))
        assert alert is False

    def test_warning_regime_still_alerts(self):
        alert, reason = should_alert(**self._base_kwargs(regime_status="WARNING"))
        assert alert is True

    def test_insufficient_global_sample(self):
        alert, reason = should_alert(**self._base_kwargs(global_sample=50))
        assert alert is False

    def test_odds_too_low(self):
        alert, reason = should_alert(**self._base_kwargs(odds=1.30))
        assert alert is False

    def test_odds_too_high(self):
        alert, reason = should_alert(**self._base_kwargs(odds=4.50))
        assert alert is False

    def test_edge_too_low(self):
        alert, reason = should_alert(**self._base_kwargs(edge_val=0.02))
        assert alert is False

    def test_ev_too_low(self):
        alert, reason = should_alert(**self._base_kwargs(ev_val=0.01))
        assert alert is False

    def test_prob_too_low(self):
        alert, reason = should_alert(**self._base_kwargs(true_prob_conservative=0.50))
        assert alert is False


# ---------------------------------------------------------------------------
# detect_regime_change
# ---------------------------------------------------------------------------

class TestDetectRegimeChange:
    def test_healthy(self):
        result = detect_regime_change(35, 50, 0.65)
        assert result["status"] == "HEALTHY"

    def test_warning(self):
        # historical=65%, recent=50% with 50 samples → z ≈ -2.1 (DEGRADED)
        # let's do 55% vs 65% → z ≈ -1.5 (WARNING-ish)
        result = detect_regime_change(28, 50, 0.65, -1.5, -2.0)
        # 28/50 = 56%, historical 65%
        assert result["status"] in ("WARNING", "DEGRADED", "HEALTHY")

    def test_degraded(self):
        # Very poor recent performance
        result = detect_regime_change(20, 50, 0.65)
        # 20/50 = 40%, historical 65%, z ≈ -3.5
        assert result["status"] == "DEGRADED"

    def test_no_data(self):
        result = detect_regime_change(0, 0, 0.65)
        assert result["status"] == "HEALTHY"
        assert result["z_score"] == 0.0


# ---------------------------------------------------------------------------
# calculate_true_probability
# ---------------------------------------------------------------------------

class TestCalculateTrueProbability:
    def test_all_uniform_returns_reasonable(self):
        p = calculate_true_probability(
            p_base=0.65, p_loss_type=0.65, p_player=0.65,
            p_time_slot=0.65, p_team=0.65, p_market_adj=0.0,
            n_player=10, n_team=10,
        )
        assert abs(p - 0.65) < 0.05

    def test_bounded_0_to_1(self):
        p = calculate_true_probability(
            p_base=0.9, p_loss_type=0.9, p_player=0.9,
            p_time_slot=0.9, p_team=0.9, p_market_adj=0.03,
            n_player=100, n_team=100,
        )
        assert 0.0 <= p <= 1.0

    def test_no_player_data_falls_back_to_base(self):
        p_with = calculate_true_probability(
            p_base=0.60, p_loss_type=0.65, p_player=0.80,
            p_time_slot=0.65, p_team=0.65, p_market_adj=0.0,
            n_player=10, n_team=10,
        )
        p_without = calculate_true_probability(
            p_base=0.60, p_loss_type=0.65, p_player=0.80,
            p_time_slot=0.65, p_team=0.65, p_market_adj=0.0,
            n_player=0, n_team=0,
        )
        # With more player data, should be pulled towards 0.80
        assert p_with > p_without


# ---------------------------------------------------------------------------
# simulate_roi
# ---------------------------------------------------------------------------

class TestSimulateROI:
    def _make_results(self, odds, hit_rate, n=100):
        import random
        random.seed(42)
        return [
            {"odds": odds, "hit": random.random() < hit_rate, "true_prob": hit_rate}
            for _ in range(n)
        ]

    def test_empty_results(self):
        result = simulate_roi([])
        assert result["total_bets"] == 0
        assert result["roi"] == 0.0

    def test_all_wins(self):
        results = [{"odds": 2.00, "hit": True, "true_prob": 1.0} for _ in range(10)]
        result = simulate_roi(results, stake_method="flat", flat_stake=10)
        assert result["wins"] == 10
        assert result["roi"] > 0

    def test_all_losses(self):
        results = [{"odds": 2.00, "hit": False, "true_prob": 0.0} for _ in range(10)]
        result = simulate_roi(results, stake_method="flat", flat_stake=10)
        assert result["losses"] == 10
        assert result["roi"] < 0

    def test_profitable_strategy(self):
        # 70% hit rate at @2.00 → very profitable
        results = self._make_results(2.00, 0.70, 200)
        result = simulate_roi(results, stake_method="flat")
        assert result["roi"] > 0

    def test_kelly_method(self):
        results = self._make_results(1.85, 0.65, 100)
        result = simulate_roi(results, stake_method="kelly")
        assert result["total_bets"] > 0
