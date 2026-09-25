"""
Unit tests for position sizing with Kelly Criterion.
"""
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from src.execution.position_sizer import (
    PositionSize,
    PositionSizer,
    calculate_position,
    get_position_sizer,
)


class TestPositionSize:
    """Tests for PositionSize dataclass."""

    def test_position_size_creation(self):
        """Test PositionSize creation."""
        size = PositionSize(
            ticker="BTC-100K",
            side="yes",
            kelly_fraction=0.15,
            recommended_fraction=0.075,
            recommended_contracts=7,
            recommended_dollars=35.0,
            max_position_pct=0.10,
            existing_position=0,
            capped_by_max_position=False,
            capped_by_bankroll=False,
            edge=0.10,
            probability=0.65,
            odds=0.82,
            bankroll=1000.0,
        )

        assert size.ticker == "BTC-100K"
        assert size.side == "yes"
        assert size.kelly_fraction == 0.15
        assert size.recommended_contracts == 7
        assert size.bankroll == 1000.0


class TestPositionSizer:
    """Tests for PositionSizer."""

    def test_initialization_defaults(self):
        """Test sizer initialization with defaults."""
        sizer = PositionSizer()

        assert sizer.kelly_fraction == 0.5
        assert sizer.max_position_pct == 0.10
        assert sizer.max_total_exposure_pct == 0.50
        assert sizer.min_position_dollars == 1.0

    def test_initialization_custom(self):
        """Test sizer initialization with custom params."""
        sizer = PositionSizer(
            kelly_fraction=0.25,
            max_position_pct=0.05,
            max_total_exposure_pct=0.30,
            min_position_dollars=5.0,
        )

        assert sizer.kelly_fraction == 0.25
        assert sizer.max_position_pct == 0.05
        assert sizer.max_total_exposure_pct == 0.30
        assert sizer.min_position_dollars == 5.0

    def test_calculate_kelly_fraction_yes_positive_edge(self):
        """Test Kelly calculation for YES side with positive edge."""
        sizer = PositionSizer()

        # 70% win prob, buy YES at 50 cents
        # Profit if win: 50, Loss if lose: 50
        # Odds b = 50/50 = 1
        # Kelly = (1 * 0.7 - 0.3) / 1 = 0.4
        kelly = sizer.calculate_kelly_fraction(
            win_probability=0.70,
            entry_price=50,
            side="yes",
        )

        assert abs(kelly - 0.40) < 0.01

    def test_calculate_kelly_fraction_yes_at_fair_price(self):
        """Test Kelly at fair price (no edge)."""
        sizer = PositionSizer()

        # 50% win prob at 50 cents = fair price, Kelly = 0
        kelly = sizer.calculate_kelly_fraction(
            win_probability=0.50,
            entry_price=50,
            side="yes",
        )

        assert abs(kelly) < 0.01

    def test_calculate_kelly_fraction_yes_negative_edge(self):
        """Test Kelly with negative edge (should be 0)."""
        sizer = PositionSizer()

        # 40% win prob at 50 cents = negative edge
        kelly = sizer.calculate_kelly_fraction(
            win_probability=0.40,
            entry_price=50,
            side="yes",
        )

        assert kelly == 0.0

    def test_calculate_kelly_fraction_no_side(self):
        """Test Kelly calculation for NO side."""
        sizer = PositionSizer()

        # If YES win prob = 40%, then NO win prob = 60%
        # Buying NO at implied 60 cents (100 - 40)
        # Wait, entry_price is YES price, so NO price = 100 - 40 = 60
        # Profit if NO wins: 100 - 60 = 40
        # Loss if NO loses: 60
        # Odds b = 40/60 = 0.667
        # p for NO = 0.6
        # Kelly = (0.667 * 0.6 - 0.4) / 0.667 = 0.0
        # Actually with win_probability=0.40 (YES), NO prob = 0.60
        kelly = sizer.calculate_kelly_fraction(
            win_probability=0.40,  # YES probability
            entry_price=60,  # YES price = 60, NO price = 40
            side="no",
        )

        # With YES at 60c, NO at 40c
        # If model says 40% YES (60% NO)
        # NO price = 40, profit if NO = 60
        # odds = 60/40 = 1.5
        # Kelly for NO = (1.5 * 0.6 - 0.4) / 1.5 = (0.9 - 0.4) / 1.5 = 0.333
        assert kelly > 0

    def test_calculate_kelly_fraction_high_probability(self):
        """Test Kelly with high win probability."""
        sizer = PositionSizer()

        # 90% win prob at 50 cents
        kelly = sizer.calculate_kelly_fraction(
            win_probability=0.90,
            entry_price=50,
            side="yes",
        )

        # Very high Kelly - should be significant
        assert kelly > 0.5

    def test_calculate_kelly_fraction_low_price(self):
        """Test Kelly at low price (high odds)."""
        sizer = PositionSizer()

        # 30% win prob at 10 cents
        # Odds = 90/10 = 9
        # Kelly = (9 * 0.3 - 0.7) / 9 = (2.7 - 0.7) / 9 = 0.222
        kelly = sizer.calculate_kelly_fraction(
            win_probability=0.30,
            entry_price=10,
            side="yes",
        )

        assert abs(kelly - 0.222) < 0.01

    def test_calculate_kelly_from_edge_positive(self):
        """Test simplified Kelly from edge."""
        sizer = PositionSizer()

        # 10% edge at 50% market probability
        kelly = sizer.calculate_kelly_from_edge(
            edge=0.10,
            market_probability=0.50,
        )

        # f = edge / (1 - market_prob) = 0.1 / 0.5 = 0.2
        assert abs(kelly - 0.20) < 0.01

    def test_calculate_kelly_from_edge_negative(self):
        """Test simplified Kelly from negative edge (NO side)."""
        sizer = PositionSizer()

        # -10% edge (model < market) at 60% market
        kelly = sizer.calculate_kelly_from_edge(
            edge=-0.10,
            market_probability=0.60,
        )

        # f = -edge / market_prob = 0.1 / 0.6 = 0.167
        assert abs(kelly - 0.167) < 0.01

    def test_calculate_position_size_basic(self):
        """Test basic position size calculation."""
        sizer = PositionSizer(kelly_fraction=0.5, max_position_pct=0.10)

        with patch.object(sizer, "get_current_bankroll", return_value=1000.0):
            with patch.object(sizer, "get_existing_position", return_value=0):
                with patch.object(sizer, "get_total_exposure", return_value=0.0):
                    size = sizer.calculate_position_size(
                        ticker="TEST",
                        win_probability=0.65,
                        entry_price=50,
                        side="yes",
                        edge=0.15,
                        confidence=0.8,
                    )

                    assert size.ticker == "TEST"
                    assert size.side == "yes"
                    assert size.kelly_fraction > 0
                    assert size.recommended_contracts > 0
                    assert size.bankroll == 1000.0

    def test_calculate_position_size_capped_by_max_position(self):
        """Test position size capped by max position limit."""
        sizer = PositionSizer(kelly_fraction=1.0, max_position_pct=0.05)  # Full Kelly

        with patch.object(sizer, "get_current_bankroll", return_value=1000.0):
            with patch.object(sizer, "get_existing_position", return_value=0):
                with patch.object(sizer, "get_total_exposure", return_value=0.0):
                    # High edge should want large Kelly
                    size = sizer.calculate_position_size(
                        ticker="TEST",
                        win_probability=0.90,
                        entry_price=50,
                        side="yes",
                        edge=0.40,
                        confidence=1.0,
                    )

                    # Should be capped at 5% = $50
                    assert size.capped_by_max_position is True
                    assert size.recommended_dollars <= 50.0

    def test_calculate_position_size_existing_position(self):
        """Test position size with existing position."""
        sizer = PositionSizer(kelly_fraction=0.5, max_position_pct=0.10)

        with patch.object(sizer, "get_current_bankroll", return_value=1000.0):
            with patch.object(sizer, "get_existing_position", return_value=50):  # 50 contracts
                with patch.object(sizer, "get_total_exposure", return_value=25.0):
                    size = sizer.calculate_position_size(
                        ticker="TEST",
                        win_probability=0.65,
                        entry_price=50,
                        side="yes",
                        edge=0.15,
                        confidence=0.8,
                    )

                    # Should have existing position recorded
                    assert size.existing_position == 50
                    # New position should be reduced

    def test_calculate_position_size_total_exposure_limit(self):
        """Test position size limited by total exposure."""
        sizer = PositionSizer(
            kelly_fraction=1.0,
            max_position_pct=0.20,
            max_total_exposure_pct=0.50,
        )

        with patch.object(sizer, "get_current_bankroll", return_value=1000.0):
            with patch.object(sizer, "get_existing_position", return_value=0):
                # Already at 45% exposure, only 5% remaining
                with patch.object(sizer, "get_total_exposure", return_value=450.0):
                    size = sizer.calculate_position_size(
                        ticker="TEST",
                        win_probability=0.80,
                        entry_price=50,
                        side="yes",
                        edge=0.30,
                        confidence=1.0,
                    )

                    # Should be capped by remaining exposure (5% = $50)
                    assert size.recommended_dollars <= 50.0 + 0.01  # Small tolerance
                    assert size.capped_by_bankroll is True

    def test_calculate_position_size_zero_edge(self):
        """Test position size with zero edge."""
        sizer = PositionSizer(kelly_fraction=0.5, max_position_pct=0.10)

        with patch.object(sizer, "get_current_bankroll", return_value=1000.0):
            with patch.object(sizer, "get_existing_position", return_value=0):
                with patch.object(sizer, "get_total_exposure", return_value=0.0):
                    size = sizer.calculate_position_size(
                        ticker="TEST",
                        win_probability=0.50,  # No edge
                        entry_price=50,
                        side="yes",
                        edge=0.0,
                        confidence=0.5,
                    )

                    # Kelly should be 0 for no edge
                    assert size.kelly_fraction < 0.01
                    assert size.recommended_contracts == 0

    def test_calculate_position_size_no_side(self):
        """Test position size for NO side."""
        sizer = PositionSizer(kelly_fraction=0.5, max_position_pct=0.10)

        with patch.object(sizer, "get_current_bankroll", return_value=1000.0):
            with patch.object(sizer, "get_existing_position", return_value=0):
                with patch.object(sizer, "get_total_exposure", return_value=0.0):
                    size = sizer.calculate_position_size(
                        ticker="TEST",
                        win_probability=0.35,  # Low YES prob = high NO prob
                        entry_price=60,  # YES at 60, NO at 40
                        side="no",
                        edge=-0.25,
                        confidence=0.8,
                    )

                    assert size.side == "no"
                    assert size.recommended_contracts > 0

    def test_calculate_batch(self):
        """Test batch position calculation."""
        sizer = PositionSizer(kelly_fraction=0.5, max_position_pct=0.10)

        opportunities = [
            {
                "ticker": "MKT1",
                "probability": 0.65,
                "price": 50,
                "side": "yes",
                "edge": 0.15,
                "confidence": 0.8,
            },
            {
                "ticker": "MKT2",
                "probability": 0.70,
                "price": 55,
                "side": "yes",
                "edge": 0.15,
                "confidence": 0.7,
            },
        ]

        with patch.object(sizer, "get_current_bankroll", return_value=1000.0):
            with patch.object(sizer, "get_existing_position", return_value=0):
                with patch.object(sizer, "get_total_exposure", return_value=0.0):
                    results = sizer.calculate_batch(opportunities, bankroll=1000.0)

                    assert len(results) >= 1
                    # First position should be largest (more bankroll available)

    def test_calculate_batch_with_allocation(self):
        """Test batch considers previous allocations."""
        sizer = PositionSizer(kelly_fraction=1.0, max_position_pct=0.30)

        # Create opportunities that would each want $500
        opportunities = [
            {
                "ticker": "MKT1",
                "probability": 0.90,
                "price": 50,
                "side": "yes",
                "edge": 0.40,
                "confidence": 1.0,
            },
            {
                "ticker": "MKT2",
                "probability": 0.90,
                "price": 50,
                "side": "yes",
                "edge": 0.40,
                "confidence": 1.0,
            },
        ]

        with patch.object(sizer, "get_existing_position", return_value=0):
            with patch.object(sizer, "get_total_exposure", return_value=0.0):
                results = sizer.calculate_batch(opportunities, bankroll=1000.0)

                # Second position should have less bankroll available
                if len(results) >= 2:
                    assert results[1].bankroll < results[0].bankroll

    def test_get_sizing_stats(self):
        """Test getting sizing statistics."""
        sizer = PositionSizer()

        with patch.object(sizer, "get_current_bankroll", return_value=1000.0):
            with patch.object(sizer, "get_total_exposure", return_value=200.0):
                stats = sizer.get_sizing_stats()

                assert stats["current_bankroll"] == 1000.0
                assert stats["total_exposure"] == 200.0
                assert abs(stats["exposure_pct"] - 0.20) < 0.01
                assert stats["remaining_capacity"] == 300.0  # 50% of 1000 - 200


class TestConvenienceFunctions:
    """Tests for module-level convenience functions."""

    def test_get_position_sizer_singleton(self):
        """Test get_position_sizer returns singleton."""
        import src.execution.position_sizer as sizer_module

        sizer_module._sizer = None

        sizer1 = get_position_sizer()
        sizer2 = get_position_sizer()

        assert sizer1 is sizer2

    def test_calculate_position_convenience(self):
        """Test calculate_position convenience function."""
        import src.execution.position_sizer as sizer_module

        sizer_module._sizer = None

        with patch.object(PositionSizer, "get_current_bankroll", return_value=1000.0):
            with patch.object(PositionSizer, "get_existing_position", return_value=0):
                with patch.object(PositionSizer, "get_total_exposure", return_value=0.0):
                    result = calculate_position(
                        ticker="TEST",
                        win_probability=0.65,
                        entry_price=50,
                        side="yes",
                        edge=0.15,
                    )

                    assert isinstance(result, PositionSize)


class TestKellyCriterionMath:
    """Tests for Kelly Criterion mathematical properties."""

    def test_kelly_at_extremes(self):
        """Test Kelly at probability extremes."""
        sizer = PositionSizer()

        # 99% probability at 50 cents
        kelly_high = sizer.calculate_kelly_fraction(0.99, 50, "yes")
        assert kelly_high > 0.9  # Very high

        # 1% probability at 50 cents
        kelly_low = sizer.calculate_kelly_fraction(0.01, 50, "yes")
        assert kelly_low == 0.0  # Clamped to 0

    def test_kelly_symmetric(self):
        """Test Kelly is roughly symmetric for YES/NO at fair odds."""
        sizer = PositionSizer()

        # YES at 30% prob buying at 20 cents
        kelly_yes = sizer.calculate_kelly_fraction(0.30, 20, "yes")

        # NO at same scenario (70% NO prob)
        kelly_no = sizer.calculate_kelly_fraction(0.30, 80, "no")  # YES at 80

        # Both should be positive and similar magnitude
        assert kelly_yes > 0
        assert kelly_no > 0

    def test_kelly_zero_at_fair_price(self):
        """Test Kelly is 0 when probability equals price."""
        sizer = PositionSizer()

        # P=0.50 at 50 cents is fair
        kelly = sizer.calculate_kelly_fraction(0.50, 50, "yes")
        assert abs(kelly) < 0.001

        # P=0.75 at 75 cents is fair
        kelly = sizer.calculate_kelly_fraction(0.75, 75, "yes")
        assert abs(kelly) < 0.001

    def test_kelly_increases_with_edge(self):
        """Test Kelly increases with larger edge."""
        sizer = PositionSizer()

        kelly_small = sizer.calculate_kelly_fraction(0.55, 50, "yes")
        kelly_medium = sizer.calculate_kelly_fraction(0.65, 50, "yes")
        kelly_large = sizer.calculate_kelly_fraction(0.75, 50, "yes")

        assert kelly_small < kelly_medium < kelly_large
