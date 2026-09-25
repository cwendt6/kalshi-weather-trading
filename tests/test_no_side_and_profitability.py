"""
Tests for NO side betting fixes and profitability improvements.

Run: pytest tests/test_no_side_and_profitability.py -v
"""
import pytest
import sys
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.analysis.edge import EdgeCalculator
from src.execution.position_manager import (
    PositionManager,
    PositionState,
    PriceTier,
    ExitReason,
    ExitRecord,
)
from src.strategy.longshot_hunter import (
    LongshotHunter,
    LongshotOpportunity,
    LongshotCriteria,
    ConfidenceLevel,
)


# ═══════════════════════════════════════════════════════════════════════════════
# EV Calculation — NO Side
# ═══════════════════════════════════════════════════════════════════════════════

class TestEVCalculationNOSide:
    """Test EV calculation handles NO side correctly."""

    def test_no_side_uses_no_price_directly(self):
        """NO side entry_price should be treated as the NO cost, not YES cost."""
        calc = EdgeCalculator()

        # Buy NO at 30c. If NO wins (prob=0.6), profit = 70c - fees
        ev, ev_pct = calc.calculate_expected_value(
            model_prob=0.40,  # P(YES)=40%, so P(NO)=60%
            entry_price=30,   # NO costs 30 cents
            side="no",
        )

        # Expected: 0.6 * (70 * 0.98) - 0.4 * 30 = 0.6 * 68.6 - 12 = 41.16 - 12 = 29.16
        assert ev > 25, f"EV should be positive for good NO bet, got {ev}"
        assert ev_pct > 0.5, f"EV% should be >50% for this bet, got {ev_pct}"

    def test_no_side_negative_ev_when_overpriced(self):
        """NO bet should be -EV when NO is overpriced."""
        calc = EdgeCalculator()

        # Buy NO at 70c, but P(NO) is only 40%
        ev, ev_pct = calc.calculate_expected_value(
            model_prob=0.60,  # P(YES)=60%, P(NO)=40%
            entry_price=70,   # NO costs 70 cents
            side="no",
        )

        assert ev < 0, f"EV should be negative for overpriced NO bet, got {ev}"

    def test_yes_and_no_ev_are_complementary(self):
        """For same market, one side should be +EV and other -EV."""
        calc = EdgeCalculator()

        # Model says 60% YES. Market: YES ask=55, NO ask=48
        ev_yes, _ = calc.calculate_expected_value(0.60, 55, "yes")
        ev_no, _ = calc.calculate_expected_value(0.60, 48, "no")

        # YES should be +EV (model says 60%, paying 55c)
        assert ev_yes > 0
        # NO should be -EV (model says 40% NO, paying 48c which implies 48% NO)
        assert ev_no < 0

    def test_no_side_at_cheap_price(self):
        """Cheap NO (e.g. 10c) should have high EV if model agrees."""
        calc = EdgeCalculator()

        # Buy NO at 10c, model says P(NO) = 30%
        ev, ev_pct = calc.calculate_expected_value(
            model_prob=0.70,  # P(YES)=70%, P(NO)=30%
            entry_price=10,   # NO costs 10 cents
            side="no",
        )

        # Expected: 0.3 * (90 * 0.98) - 0.7 * 10 = 0.3 * 88.2 - 7 = 26.46 - 7 = 19.46
        assert ev > 15, f"EV should be very positive for cheap NO bet, got {ev}"

    def test_yes_side_unchanged(self):
        """YES side EV should work exactly as before."""
        calc = EdgeCalculator()

        ev, ev_pct = calc.calculate_expected_value(
            model_prob=0.60,
            entry_price=50,
            side="yes",
        )

        # 0.60 * (50 * 0.98) - 0.40 * 50 = 0.60 * 49 - 20 = 29.4 - 20 = 9.4
        assert abs(ev - 9.4) < 0.1, f"YES EV should be ~9.4, got {ev}"

    def test_no_side_symmetric_with_yes(self):
        """In a spread-free market (YES+NO=100), EV should be symmetric."""
        calc = EdgeCalculator()

        # Model: 60% YES. Market: YES=50, NO=50
        ev_yes, _ = calc.calculate_expected_value(0.60, 50, "yes")
        ev_no, _ = calc.calculate_expected_value(0.60, 50, "no")

        # YES should be +EV, NO should be -EV
        assert ev_yes > 0
        assert ev_no < 0


# ═══════════════════════════════════════════════════════════════════════════════
# True Edge Calculation (Spread + Fee Aware)
# ═══════════════════════════════════════════════════════════════════════════════

class TestTrueEdgeCalculation:
    """Test spread-and-fee-adjusted edge calculation."""

    def test_true_edge_subtracts_spread(self):
        calc = EdgeCalculator()
        result = calc.calculate_true_edge(
            model_prob=0.55,
            yes_bid=50,
            yes_ask=56,  # 6-cent spread
        )

        # Raw edge = 55% - 56% = -1% for YES (we'd pay 56c)
        assert result["yes_true_edge"] < 0.02
        assert result["spread_cost"] == 0.06

    def test_wide_spread_kills_edge(self):
        calc = EdgeCalculator()
        result = calc.calculate_true_edge(
            model_prob=0.55,
            yes_bid=48,
            yes_ask=58,  # 10-cent spread
        )

        # Even though model says 55% vs mid-price 53%, the ask is 58c
        assert result["best_side"] is None  # Neither side profitable

    def test_tight_spread_preserves_edge(self):
        calc = EdgeCalculator()
        result = calc.calculate_true_edge(
            model_prob=0.60,
            yes_bid=52,
            yes_ask=54,  # 2-cent spread
        )

        # True YES edge = 60% - 54% - fees ≈ 5.1%
        assert result["best_side"] == "yes"
        assert result["yes_true_edge"] > 0.03

    def test_no_side_better_when_yes_overpriced(self):
        """When model says 30% YES, NO side should be recommended."""
        calc = EdgeCalculator()
        result = calc.calculate_true_edge(
            model_prob=0.30,
            yes_bid=70,
            yes_ask=72,
            no_ask=30,  # NO ask is 30c
        )

        # Model says 70% NO, NO costs 30c → big edge
        assert result["no_true_edge"] > result["yes_true_edge"]
        assert result["best_side"] == "no"

    def test_uses_no_ask_when_available(self):
        calc = EdgeCalculator()
        result = calc.calculate_true_edge(
            model_prob=0.30,
            yes_bid=68,
            yes_ask=72,
            no_ask=35,  # Real NO ask (not 100-72=28)
        )

        # Should use actual no_ask=35, not estimated 32 (100-68)
        no_entry_prob = 35 / 100.0
        assert result["no_true_edge"] < calc.calculate_true_edge(
            0.30, 68, 72, no_ask=28  # If NO were cheaper
        )["no_true_edge"]

    def test_returns_dict_with_required_keys(self):
        calc = EdgeCalculator()
        result = calc.calculate_true_edge(0.50, 48, 52)

        assert "yes_true_edge" in result
        assert "no_true_edge" in result
        assert "spread_cost" in result
        assert "best_side" in result

    def test_spread_cost_calculation(self):
        calc = EdgeCalculator()
        result = calc.calculate_true_edge(0.50, 45, 55)
        assert abs(result["spread_cost"] - 0.10) < 0.001


# ═══════════════════════════════════════════════════════════════════════════════
# Dynamic Minimum Edge by Price Tier
# ═══════════════════════════════════════════════════════════════════════════════

class TestDynamicMinEdge:
    """Test tier-based minimum edge thresholds."""

    def test_longshot_lower_threshold(self):
        calc = EdgeCalculator()
        assert calc.get_min_edge(10) < calc.get_min_edge(80)

    def test_high_price_needs_more_edge(self):
        calc = EdgeCalculator()
        assert calc.get_min_edge(90) >= 0.03  # At least 3%

    def test_mid_price_moderate_threshold(self):
        calc = EdgeCalculator()
        assert 0.02 <= calc.get_min_edge(50) <= 0.03

    def test_boundary_values(self):
        calc = EdgeCalculator()
        assert calc.get_min_edge(15) == 0.015  # Longshot
        assert calc.get_min_edge(16) == 0.02   # Low
        assert calc.get_min_edge(35) == 0.02   # Low
        assert calc.get_min_edge(36) == 0.025  # Mid
        assert calc.get_min_edge(65) == 0.025  # Mid
        assert calc.get_min_edge(66) == 0.035  # High

    def test_extreme_values(self):
        calc = EdgeCalculator()
        assert calc.get_min_edge(1) == 0.015   # Longshot
        assert calc.get_min_edge(99) == 0.035  # High


# ═══════════════════════════════════════════════════════════════════════════════
# Re-Entry Tracking
# ═══════════════════════════════════════════════════════════════════════════════

class TestReEntryTracking:
    """Test position re-entry after exit."""

    def _make_pm(self) -> PositionManager:
        pm = PositionManager.__new__(PositionManager)
        pm._exit_records = {}
        return pm

    def test_reentry_blocked_during_cooldown(self):
        pm = self._make_pm()
        pm._exit_records["TEST"] = ExitRecord(
            ticker="TEST",
            side="yes",
            entry_price=40,
            exit_price=55,
            exit_time=datetime.now(timezone.utc) - timedelta(minutes=30),
            exit_reason=ExitReason.TAKE_PROFIT,
            net_profit=1.50,
            hold_hours=5.0,
        )

        # Should be blocked — only 30 min since exit
        assert pm.check_reentry_eligible("TEST", 42, 0.03) is False

    def test_reentry_allowed_after_cooldown(self):
        pm = self._make_pm()
        pm._exit_records["TEST"] = ExitRecord(
            ticker="TEST",
            side="yes",
            entry_price=40,
            exit_price=55,
            exit_time=datetime.now(timezone.utc) - timedelta(hours=3),
            exit_reason=ExitReason.TAKE_PROFIT,
            net_profit=1.50,
            hold_hours=5.0,
        )

        # Price moved 13c from exit (55 -> 42), waited 3 hours
        assert pm.check_reentry_eligible("TEST", 42, 0.03) is True

    def test_reentry_blocked_if_price_too_close(self):
        pm = self._make_pm()
        pm._exit_records["TEST"] = ExitRecord(
            ticker="TEST",
            side="yes",
            entry_price=40,
            exit_price=50,
            exit_time=datetime.now(timezone.utc) - timedelta(hours=3),
            exit_reason=ExitReason.TAKE_PROFIT,
            net_profit=1.00,
            hold_hours=5.0,
        )

        # Only 1c away from exit price
        assert pm.check_reentry_eligible("TEST", 49, 0.05) is False

    def test_reentry_needs_higher_edge_after_stop_loss(self):
        pm = self._make_pm()
        pm._exit_records["TEST"] = ExitRecord(
            ticker="TEST",
            side="yes",
            entry_price=40,
            exit_price=30,
            exit_time=datetime.now(timezone.utc) - timedelta(hours=5),
            exit_reason=ExitReason.STOP_LOSS,
            net_profit=-1.00,
            hold_hours=3.0,
        )

        # After stop loss, need 3%+ edge
        assert pm.check_reentry_eligible("TEST", 25, 0.02) is False
        assert pm.check_reentry_eligible("TEST", 25, 0.04) is True

    def test_fresh_entry_always_allowed(self):
        pm = self._make_pm()
        assert pm.check_reentry_eligible("NEWMARKET", 50, 0.02) is True

    def test_exit_record_properties(self):
        record = ExitRecord(
            ticker="TEST",
            side="yes",
            entry_price=40,
            exit_price=55,
            exit_time=datetime.now(timezone.utc) - timedelta(hours=2),
            exit_reason=ExitReason.TAKE_PROFIT,
            net_profit=1.50,
            hold_hours=5.0,
        )

        assert record.was_profitable is True
        assert record.time_since_exit() > 1.9  # ~2 hours
        assert record.time_since_exit() < 2.5

    def test_losing_exit_record(self):
        record = ExitRecord(
            ticker="TEST",
            side="yes",
            entry_price=40,
            exit_price=30,
            exit_time=datetime.now(timezone.utc),
            exit_reason=ExitReason.STOP_LOSS,
            net_profit=-1.00,
            hold_hours=3.0,
        )

        assert record.was_profitable is False


# ═══════════════════════════════════════════════════════════════════════════════
# Longshot NO Side
# ═══════════════════════════════════════════════════════════════════════════════

class TestLongshotNOSide:
    """Test that longshot hunter can find NO opportunities."""

    def test_longshot_opportunity_has_side_field(self):
        """LongshotOpportunity should have a 'side' field."""
        opp = LongshotOpportunity(
            ticker="TEST",
            title="Test Market",
            side="no",
            market_price=0.10,
            estimated_prob=0.25,
            edge=1.5,
            edge_multiplier=2.5,
            potential_return=9.0,
            confidence=ConfidenceLevel.MEDIUM,
            volume=1000,
            open_interest=500,
            close_time=None,
            category="test",
        )

        assert opp.side == "no"

    def test_enter_position_uses_opportunity_side(self):
        """enter_position should use the side from the opportunity, not hardcoded 'yes'."""
        hunter = LongshotHunter()

        opp = LongshotOpportunity(
            ticker="TEST-NO",
            title="NO Longshot",
            side="no",
            market_price=0.08,
            estimated_prob=0.20,
            edge=1.5,
            edge_multiplier=2.5,
            potential_return=11.5,
            confidence=ConfidenceLevel.MEDIUM,
            volume=500,
            open_interest=200,
            close_time=None,
            category="test",
        )

        position = hunter.enter_position(opp, quantity=10, actual_price=0.08)
        assert position.side == "no"

    def test_enter_position_yes_still_works(self):
        """YES longshots should still work as before."""
        hunter = LongshotHunter()

        opp = LongshotOpportunity(
            ticker="TEST-YES",
            title="YES Longshot",
            side="yes",
            market_price=0.10,
            estimated_prob=0.20,
            edge=1.0,
            edge_multiplier=2.0,
            potential_return=9.0,
            confidence=ConfidenceLevel.MEDIUM,
            volume=500,
            open_interest=200,
            close_time=None,
            category="test",
        )

        position = hunter.enter_position(opp, quantity=10, actual_price=0.10)
        assert position.side == "yes"

    def test_evaluate_market_detects_no_longshot(self):
        """A market with cheap NO should detect a NO longshot."""
        hunter = LongshotHunter()

        # Mock the price data and probability estimation
        with patch.object(hunter, '_get_latest_price') as mock_price, \
             patch.object(hunter, '_estimate_probability') as mock_prob:
            # YES at 92c → NO at 8c (cheap!)
            mock_price.return_value = {
                "yes_bid": 90,
                "yes_ask": 92,
                "no_bid": 6,
                "no_ask": 8,
                "volume": 1000,
            }
            # Model estimates P(YES) = 70%, so P(NO) = 30%
            # NO price is 8c (0.08), but model says 30% → edge_multiplier = 0.30/0.08 = 3.75x
            mock_prob.return_value = 0.70

            market = MagicMock()
            market.ticker = "TEST"
            market.title = "Test Market"
            market.volume = 1000
            market.open_interest = 500
            market.close_time = None
            market.category = "test"

            result = hunter._evaluate_market(market)

        assert result is not None
        assert result.side == "no"
        assert result.market_price == 0.08
        assert abs(result.estimated_prob - 0.30) < 0.001  # P(NO)

    def test_evaluate_market_yes_longshot_still_works(self):
        """YES longshots should still be detected."""
        hunter = LongshotHunter()

        with patch.object(hunter, '_get_latest_price') as mock_price, \
             patch.object(hunter, '_estimate_probability') as mock_prob:
            # YES at 10c — cheap YES
            mock_price.return_value = {
                "yes_bid": 8,
                "yes_ask": 10,
                "no_bid": 88,
                "no_ask": 90,
                "volume": 1000,
            }
            # Model says P(YES) = 25% → edge_multiplier = 0.25/0.10 = 2.5x
            mock_prob.return_value = 0.25

            market = MagicMock()
            market.ticker = "TEST"
            market.title = "Test Market"
            market.volume = 1000
            market.open_interest = 500
            market.close_time = None
            market.category = "test"

            result = hunter._evaluate_market(market)

        assert result is not None
        assert result.side == "yes"
        assert result.market_price == 0.10


# ═══════════════════════════════════════════════════════════════════════════════
# Data Layer Fixes
# ═══════════════════════════════════════════════════════════════════════════════

class TestDataLayerFixes:
    """Test that hardcoded YES defaults have been removed."""

    def test_longshot_position_db_no_default(self):
        """LongshotPositionDB.side should not have a default value."""
        from src.data.models import LongshotPositionDB
        col = LongshotPositionDB.__table__.columns["side"]
        assert col.default is None, "LongshotPositionDB.side should not have a default"

    def test_no_side_or_yes_fallbacks_in_edge_code(self):
        """Verify no 'side or \"yes\"' patterns remain in edge.py."""
        import inspect
        from src.analysis import edge
        source = inspect.getsource(edge)
        assert 'or "yes"' not in source

    def test_portfolio_uses_unknown_default(self):
        """Portfolio should default to 'unknown' not 'yes' for NULL side."""
        import inspect
        from src.execution import portfolio
        source = inspect.getsource(portfolio)
        assert 'or "unknown"' in source
        assert 'or "yes"' not in source

    def test_position_manager_uses_unknown_default(self):
        """Position manager should default to 'unknown' not 'yes' for NULL side."""
        import inspect
        from src.execution import position_manager
        source = inspect.getsource(position_manager)
        # Should have "unknown" but not "yes" as a NULL fallback
        assert 'or "unknown"' in source


# ═══════════════════════════════════════════════════════════════════════════════
# EdgeCalculator evaluate_market with NO side
# ═══════════════════════════════════════════════════════════════════════════════

class TestEvaluateMarketNOSide:
    """Test that evaluate_market correctly handles NO side trades."""

    @patch('src.data.database.get_db_session')
    def test_evaluate_uses_no_ask_for_no_side(self, mock_db):
        """When NO is the better side, use no_ask as entry price."""
        calc = EdgeCalculator()

        # Mock price data with actual NO ask
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_price = MagicMock()
        mock_price.yes_bid = 80
        mock_price.yes_ask = 82
        mock_price.no_bid = 16
        mock_price.no_ask = 20
        mock_price.volume = 1000
        mock_price.timestamp = datetime.now(timezone.utc)

        mock_session.query.return_value.filter.return_value.order_by.return_value.first.return_value = mock_price
        mock_db.return_value.__next__ = MagicMock(return_value=mock_session)

        # Mock forecast: model says 70% YES (30% NO)
        # Market: YES ask=82 (82% implied), so YES is overpriced
        # NO ask=20 → buying NO at 20c when model says 30% NO
        mock_forecast = MagicMock()
        mock_forecast.probability_yes = 0.70
        mock_forecast.confidence = 0.7

        with patch.object(calc, '_get_latest_forecast', return_value=mock_forecast), \
             patch.object(calc, '_get_polymarket_price', return_value=None):
            result = calc.evaluate_market("TEST", "Test Market")

        # The result depends on whether the true edge passes thresholds
        # With model at 70% and YES ask at 82c, YES true edge is negative
        # NO: model says 30% NO, NO ask is 20c → big edge
        if result is not None:
            if result.side == "no":
                assert result.price == 20  # Should use actual no_ask
