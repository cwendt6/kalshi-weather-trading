"""
Tests for Phase 2 dynamic YES exit logic in PositionReEvaluator._evaluate_yes_dynamic.

Covers the 3-stage cost-basis exit system, obs-confirmation holds, edge-based exits,
time-pressure exits, hard exits, and the backward-compat alias.
"""
import math
from unittest.mock import patch

import pytest

from src.execution.position_reevaluator import (
    ExitDecision,
    ExitReason,
    PositionReEvaluator,
)


@pytest.fixture
def reeval() -> PositionReEvaluator:
    """Create a fresh PositionReEvaluator for each test."""
    return PositionReEvaluator()


# ── Shared helpers for mocking ───────────────────────────────────────────


def _base_patches(reeval_obj, obs_confirmed=False, edge=0.10, city="NYC"):
    """
    Return a dict of patch.object context managers for the most common mocks.

    Usage:
        with _base_patches(reeval, obs_confirmed=False, edge=0.05):
            decision = reeval._evaluate_yes_dynamic(...)
    """

    class _Combined:
        """Context manager that stacks multiple patch.object calls."""

        def __init__(self, reeval_obj, obs_confirmed, edge, city):
            self._patches = [
                patch.object(reeval_obj, "_is_obs_confirmed", return_value=obs_confirmed),
                patch.object(reeval_obj, "_get_weather_edge_estimate", return_value=edge),
                patch.object(reeval_obj, "_record_edge"),
                patch.object(reeval_obj, "_calculate_net_profit", return_value=1.50),
                patch.object(reeval_obj, "_get_weather_city", return_value=city),
            ]
            self._mocks = []

        def __enter__(self):
            self._mocks = [p.__enter__() for p in self._patches]
            return self._mocks

        def __exit__(self, *args):
            for p in reversed(self._patches):
                p.__exit__(*args)

    return _Combined(reeval_obj, obs_confirmed, edge, city)


# ── 1. Obs-confirmed overrides everything ────────────────────────────────


def test_obs_confirmed_overrides_everything(reeval: PositionReEvaluator):
    """When _is_obs_confirmed returns True, should_exit must be False."""
    with _base_patches(reeval, obs_confirmed=True, edge=-0.10):
        decision = reeval._evaluate_yes_dynamic(
            ticker="KXHIGHNY-26FEB12-T32",
            side="yes",
            quantity=10,
            entry_price=20,
            current_price=80,
            pnl_cents=60.0,
            pnl_dollars=6.00,
            pnl_pct=3.0,
            hours_held=14.0,
        )

    assert decision.should_exit is False
    assert "OBS CONFIRMED" in decision.reasoning


# ── 2. Edge flipped exits ────────────────────────────────────────────────


def test_edge_flipped_exits(reeval: PositionReEvaluator):
    """When edge < -0.02, should exit with EDGE_FLIPPED."""
    with _base_patches(reeval, obs_confirmed=False, edge=-0.05):
        decision = reeval._evaluate_yes_dynamic(
            ticker="KXHIGHNY-26FEB12-T32",
            side="yes",
            quantity=10,
            entry_price=20,
            current_price=18,
            pnl_cents=-2.0,
            pnl_dollars=-0.20,
            pnl_pct=-0.10,
            hours_held=2.0,
        )

    assert decision.should_exit is True
    assert decision.reason == ExitReason.EDGE_FLIPPED
    assert decision.quantity == 10  # sell all


# ── 3. Edge gone exits ──────────────────────────────────────────────────


def test_edge_gone_exits(reeval: PositionReEvaluator):
    """When 0 <= edge < 0.02, should exit with WEATHER_YES_EDGE_GONE."""
    with _base_patches(reeval, obs_confirmed=False, edge=0.01):
        decision = reeval._evaluate_yes_dynamic(
            ticker="KXHIGHNY-26FEB12-T32",
            side="yes",
            quantity=10,
            entry_price=20,
            current_price=25,
            pnl_cents=5.0,
            pnl_dollars=0.50,
            pnl_pct=0.25,
            hours_held=3.0,
        )

    assert decision.should_exit is True
    assert decision.reason == ExitReason.WEATHER_YES_EDGE_GONE
    assert decision.quantity == 10  # sell all


# ── 4. Stage 1: cost recovery ───────────────────────────────────────────


def test_stage1_cost_recovery(reeval: PositionReEvaluator):
    """
    Price >= 1.5x entry should trigger Stage 1: sell enough to recover cost.

    entry_price=20, quantity=10 => total_cost=200c
    current_price=30 (1.5x) => contracts_to_cover=ceil(200/30)=7
    (7 * 30 = 210 >= 200, cost recovered)
    """
    with _base_patches(reeval, obs_confirmed=False, edge=0.10):
        decision = reeval._evaluate_yes_dynamic(
            ticker="KXHIGHNY-26FEB12-T32",
            side="yes",
            quantity=10,
            entry_price=20,
            current_price=30,
            pnl_cents=10.0,
            pnl_dollars=1.00,
            pnl_pct=0.50,
            hours_held=2.0,
        )

    assert decision.should_exit is True
    assert decision.reason == ExitReason.WEATHER_YES_COST_BASIS

    # Verify correct quantity: ceil(200/30) = 7
    expected_sell = math.ceil((20 * 10) / 30)
    assert decision.quantity == expected_sell


# ── 5. Stage 1 sets _exit_stages to 1 ───────────────────────────────────


def test_stage1_sets_stage_1(reeval: PositionReEvaluator):
    """After Stage 1 fires, _exit_stages[ticker] should be 1."""
    ticker = "KXHIGHNY-26FEB12-T32"
    assert reeval._exit_stages.get(ticker, 0) == 0

    with _base_patches(reeval, obs_confirmed=False, edge=0.10):
        reeval._evaluate_yes_dynamic(
            ticker=ticker,
            side="yes",
            quantity=10,
            entry_price=20,
            current_price=30,
            pnl_cents=10.0,
            pnl_dollars=1.00,
            pnl_pct=0.50,
            hours_held=2.0,
        )

    assert reeval._exit_stages[ticker] == 1


# ── 6. Stage 1 keeps at least one contract ──────────────────────────────


def test_stage1_keeps_at_least_one(reeval: PositionReEvaluator):
    """
    Stage 1 should never sell the entire position; always keep at least 1.

    entry_price=10, quantity=2, current_price=20 (2x)
    total_cost=20c, contracts_to_cover=ceil(20/20)=1
    min(1, 2-1)=1, keeps 1 contract.

    Edge case: entry=10, qty=1, price=20 => contracts_to_cover=ceil(10/20)=1
    But min(1, 1-1)=0, so Stage 1 should NOT fire (no exit) because 0 contracts.
    """
    # Case 1: qty=2, should sell 1 keep 1
    with _base_patches(reeval, obs_confirmed=False, edge=0.10):
        decision = reeval._evaluate_yes_dynamic(
            ticker="KXHIGHNY-26FEB12-T32",
            side="yes",
            quantity=2,
            entry_price=10,
            current_price=20,
            pnl_cents=10.0,
            pnl_dollars=0.20,
            pnl_pct=1.0,
            hours_held=2.0,
        )

    assert decision.should_exit is True
    assert decision.reason == ExitReason.WEATHER_YES_COST_BASIS
    assert decision.quantity == 1  # sell 1, keep 1

    # Case 2: qty=1, contracts_to_cover would be min(1, 1-1)=0, so no exit
    reeval2 = PositionReEvaluator()
    with _base_patches(reeval2, obs_confirmed=False, edge=0.10):
        decision2 = reeval2._evaluate_yes_dynamic(
            ticker="KXHIGHNY-26FEB12-T33",
            side="yes",
            quantity=1,
            entry_price=10,
            current_price=20,
            pnl_cents=10.0,
            pnl_dollars=0.10,
            pnl_pct=1.0,
            hours_held=2.0,
        )

    # contracts_to_cover = ceil(10/20) = 1, min(1, 1-1) = 0 => should NOT fire
    assert decision2.should_exit is False


# ── 7. Stage 2: pure profit (price trigger) ─────────────────────────────


def test_stage2_pure_profit(reeval: PositionReEvaluator):
    """
    Stage=1, price >= 3x entry should trigger Stage 2: sell 50% remainder.

    entry_price=20, quantity=6, current_price=60 (3x entry)
    sell_qty = max(1, int(6 * 0.50)) = 3
    """
    ticker = "KXHIGHNY-26FEB12-T32"
    reeval._exit_stages[ticker] = 1  # Stage 1 already done

    with _base_patches(reeval, obs_confirmed=False, edge=0.10):
        decision = reeval._evaluate_yes_dynamic(
            ticker=ticker,
            side="yes",
            quantity=6,
            entry_price=20,
            current_price=60,
            pnl_cents=40.0,
            pnl_dollars=2.40,
            pnl_pct=2.0,
            hours_held=4.0,
        )

    assert decision.should_exit is True
    assert decision.reason == ExitReason.WEATHER_YES_PURE_PROFIT
    assert decision.quantity == 3  # 50% of 6
    assert reeval._exit_stages[ticker] == 2


# ── 8. Stage 2: edge trigger ────────────────────────────────────────────


def test_stage2_edge_trigger(reeval: PositionReEvaluator):
    """
    Stage=1, edge < 2% should trigger Stage 2 even if price < 3x entry.

    Edge=0.015 < WEATHER_YES_MIN_EDGE_STAGE2 (0.02) triggers profit take.
    Note: edge=0.015 is >= 0 and >= 0.02 is False, so edge-gone (step before stage2)
    would NOT fire because edge >= 0.02 check is `edge < self.WEATHER_YES_MIN_EDGE_STAGE2`.
    Actually 0.015 < 0.02 IS true, so edge-gone WOULD fire first.

    We need edge that is exactly at the boundary: edge=0.019 < 0.02 triggers edge_gone.
    So to test stage2 edge trigger specifically, we need edge=None for the edge-gone check
    to be skipped, then stage2 uses its own edge check.

    Actually looking at the code: edge-gone fires when `edge >= 0 and edge < 0.02`.
    Stage2 edge trigger fires when `edge is not None and edge < 0.02`.

    These overlap! So if stage==1 and edge=0.015, edge-gone fires FIRST (before stage2).
    To test stage2's edge trigger, we need the edge from _get_weather_edge_estimate
    to return None initially (skip edge checks), then return a value for stage2.

    Wait, re-reading the code: the edge variable is set ONCE and used throughout.
    If edge=0.015, edge-gone fires first regardless of stage. So stage2's edge trigger
    only matters when edge was None during the edge checks (edge is not None check fails)
    but then... no, edge is the same variable.

    Actually the stage2 check is:
        elif edge is not None and edge < self.WEATHER_YES_MIN_EDGE_STAGE2:
    This shares the same edge variable. So if edge=0.015, the earlier edge-gone block
    (edge >= 0 and edge < 0.02) fires first and returns.

    The only way stage2 edge trigger fires is if edge is negative but not < -0.02
    (so edge_flipped doesn't fire, edge_gone doesn't fire because edge < 0),
    then... actually if edge=-0.01, edge_flipped check is edge < -0.02 = False,
    edge_gone check is edge >= 0 = False. So neither fires.
    Then stage2: edge is not None and edge < 0.02 => True => fires!

    Let's use edge=-0.01 (negative but not flipped enough).
    """
    ticker = "KXHIGHNY-26FEB12-T32"
    reeval._exit_stages[ticker] = 1  # Stage 1 already done

    # edge=-0.01: not < -0.02 (no flip), not >= 0 (no edge-gone), but < 0.02 (stage2 trigger)
    with _base_patches(reeval, obs_confirmed=False, edge=-0.01):
        decision = reeval._evaluate_yes_dynamic(
            ticker=ticker,
            side="yes",
            quantity=10,
            entry_price=20,
            current_price=25,  # less than 3x, so price trigger doesn't fire
            pnl_cents=5.0,
            pnl_dollars=0.50,
            pnl_pct=0.25,
            hours_held=4.0,
        )

    assert decision.should_exit is True
    assert decision.reason == ExitReason.WEATHER_YES_PURE_PROFIT
    assert decision.quantity == max(1, int(10 * 0.50))  # 5
    assert reeval._exit_stages[ticker] == 2


# ── 9. Stage 3: time pressure + price decline ───────────────────────────


def test_stage3_time_pressure(reeval: PositionReEvaluator):
    """
    Stage=2, hours_held > 10, price declining >= 15% from peak => exit all.

    peak_price=50, current_price=40 => decline = (50-40)/50 = 20% >= 15%
    """
    ticker = "KXHIGHNY-26FEB12-T32"
    reeval._exit_stages[ticker] = 2  # Stage 2 already done

    with _base_patches(reeval, obs_confirmed=False, edge=0.10):
        with patch.object(reeval, "_get_peak_price", return_value=50):
            decision = reeval._evaluate_yes_dynamic(
                ticker=ticker,
                side="yes",
                quantity=3,
                entry_price=20,
                current_price=40,
                pnl_cents=20.0,
                pnl_dollars=0.60,
                pnl_pct=1.0,
                hours_held=11.0,
            )

    assert decision.should_exit is True
    assert decision.reason == ExitReason.WEATHER_YES_REMAINDER_EXIT
    assert decision.quantity == 3  # exit all remaining


# ── 10. Hard exit after 12h ──────────────────────────────────────────────


def test_hard_exit_12h(reeval: PositionReEvaluator):
    """hours_held > 12, no obs confirmation => exit with WEATHER_YES_HARD_EXIT."""
    with _base_patches(reeval, obs_confirmed=False, edge=0.10):
        decision = reeval._evaluate_yes_dynamic(
            ticker="KXHIGHNY-26FEB12-T32",
            side="yes",
            quantity=5,
            entry_price=20,
            current_price=22,  # small gain, not enough for stage1 (1.1x < 1.5x)
            pnl_cents=2.0,
            pnl_dollars=0.10,
            pnl_pct=0.10,
            hours_held=13.0,
        )

    assert decision.should_exit is True
    assert decision.reason == ExitReason.WEATHER_YES_HARD_EXIT
    assert decision.quantity == 5


# ── 11. Hold when no trigger met ────────────────────────────────────────


def test_hold_when_no_trigger(reeval: PositionReEvaluator):
    """
    Stage=0, healthy edge, price below 1.5x entry, hours < 12 => should_exit=False.
    """
    with _base_patches(reeval, obs_confirmed=False, edge=0.10):
        decision = reeval._evaluate_yes_dynamic(
            ticker="KXHIGHNY-26FEB12-T32",
            side="yes",
            quantity=10,
            entry_price=20,
            current_price=25,  # 1.25x, below 1.5x threshold
            pnl_cents=5.0,
            pnl_dollars=0.50,
            pnl_pct=0.25,
            hours_held=3.0,
        )

    assert decision.should_exit is False
    assert "holding" in decision.reasoning.lower()


# ── 12. Backward compatibility alias ────────────────────────────────────


def test_backward_compat_alias(reeval: PositionReEvaluator):
    """_evaluate_yes_convergence should be the same method as _evaluate_yes_dynamic."""
    # Bound methods create new objects per access, so compare the underlying function.
    assert (
        reeval._evaluate_yes_convergence.__func__
        is reeval._evaluate_yes_dynamic.__func__
    )
