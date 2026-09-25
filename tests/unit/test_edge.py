"""
Unit tests for edge calculator.
"""
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.analysis.edge import (
    EdgeCalculator,
    OpportunityType,
    TradingOpportunity,
    evaluate_market,
    get_edge_calculator,
    scan_for_opportunities,
)


class TestTradingOpportunity:
    """Tests for TradingOpportunity dataclass."""

    def test_opportunity_creation(self):
        """Test TradingOpportunity creation."""
        opp = TradingOpportunity(
            ticker="BTC-100K",
            market_title="Will Bitcoin hit $100,000?",
            opportunity_type=OpportunityType.EDGE,
            model_probability=0.65,
            market_probability=0.55,
            edge=0.10,
            expected_value=5.0,
            ev_pct=0.10,
            side="yes",
            action="buy",
            price=55,
            volume=5000,
            liquidity_score=0.7,
            opportunity_score=0.07,
        )

        assert opp.ticker == "BTC-100K"
        assert opp.edge == 0.10
        assert opp.side == "yes"
        assert opp.opportunity_type == OpportunityType.EDGE

    def test_opportunity_with_arbitrage(self):
        """Test TradingOpportunity with arbitrage fields."""
        opp = TradingOpportunity(
            ticker="TEST",
            market_title="Test Market",
            opportunity_type=OpportunityType.ARBITRAGE,
            model_probability=0.50,
            market_probability=0.55,
            edge=-0.05,
            expected_value=3.0,
            ev_pct=0.05,
            side="no",
            action="buy",
            price=55,
            volume=1000,
            liquidity_score=0.5,
            opportunity_score=0.025,
            polymarket_price=0.60,
            polymarket_edge=0.05,
            is_arbitrage=True,
        )

        assert opp.is_arbitrage is True
        assert opp.polymarket_price == 0.60
        assert opp.opportunity_type == OpportunityType.ARBITRAGE


class TestEdgeCalculator:
    """Tests for EdgeCalculator."""

    def test_initialization_defaults(self):
        """Test calculator initialization with defaults."""
        calc = EdgeCalculator()

        assert calc.min_edge == 0.02
        assert calc.min_arbitrage == 0.01
        assert calc.fee_rate == 0.02

    def test_initialization_custom(self):
        """Test calculator initialization with custom params."""
        calc = EdgeCalculator(
            min_edge=0.10,
            min_arbitrage=0.05,
            fee_rate=0.03,
        )

        assert calc.min_edge == 0.10
        assert calc.min_arbitrage == 0.05
        assert calc.fee_rate == 0.03

    def test_price_to_probability(self):
        """Test price to probability conversion."""
        calc = EdgeCalculator()

        assert calc.price_to_probability(50) == 0.50
        assert calc.price_to_probability(75) == 0.75
        assert calc.price_to_probability(1) == 0.01
        assert calc.price_to_probability(99) == 0.99

    def test_probability_to_price(self):
        """Test probability to price conversion."""
        calc = EdgeCalculator()

        assert calc.probability_to_price(0.50) == 50
        assert calc.probability_to_price(0.75) == 75
        assert calc.probability_to_price(0.01) == 1
        assert calc.probability_to_price(0.99) == 99

    def test_probability_to_price_clamped(self):
        """Test probability to price clamping."""
        calc = EdgeCalculator()

        assert calc.probability_to_price(0.0) == 1  # Clamped to 1
        assert calc.probability_to_price(1.0) == 99  # Clamped to 99
        assert calc.probability_to_price(1.5) == 99  # Clamped

    def test_calculate_edge_positive(self):
        """Test edge calculation when model > market."""
        calc = EdgeCalculator()

        edge = calc.calculate_edge(model_prob=0.65, market_prob=0.55)
        assert abs(edge - 0.10) < 0.001

    def test_calculate_edge_negative(self):
        """Test edge calculation when model < market."""
        calc = EdgeCalculator()

        edge = calc.calculate_edge(model_prob=0.45, market_prob=0.55)
        assert abs(edge - (-0.10)) < 0.001

    def test_calculate_edge_zero(self):
        """Test edge calculation when model == market."""
        calc = EdgeCalculator()

        edge = calc.calculate_edge(model_prob=0.55, market_prob=0.55)
        assert edge == 0.0

    def test_calculate_expected_value_yes_winning(self):
        """Test EV calculation for YES side (likely to win)."""
        calc = EdgeCalculator()

        # Model says 70% YES, buying YES at 55 cents
        ev, ev_pct = calc.calculate_expected_value(
            model_prob=0.70,
            entry_price=55,
            side="yes",
        )

        # Win: (100-55) * 0.98 = 44.1, Lose: 55
        # EV = 0.70 * 44.1 - 0.30 * 55 = 30.87 - 16.5 = 14.37
        assert ev > 0
        assert ev_pct > 0

    def test_calculate_expected_value_yes_losing(self):
        """Test EV calculation for YES side (likely to lose)."""
        calc = EdgeCalculator()

        # Model says 30% YES, buying YES at 55 cents
        ev, ev_pct = calc.calculate_expected_value(
            model_prob=0.30,
            entry_price=55,
            side="yes",
        )

        # Likely negative EV
        assert ev < 0

    def test_calculate_expected_value_no_winning(self):
        """Test EV calculation for NO side (likely to win)."""
        calc = EdgeCalculator()

        # Model says 30% YES (70% NO), buying NO at implied 45
        ev, ev_pct = calc.calculate_expected_value(
            model_prob=0.30,
            entry_price=55,  # YES price, NO price = 45
            side="no",
        )

        # Should have positive EV
        assert ev > 0

    def test_calculate_liquidity_score_high_volume_tight_spread(self):
        """Test liquidity score with high volume and tight spread."""
        calc = EdgeCalculator()

        score = calc.calculate_liquidity_score(volume=15000, spread=1)
        # Should be near max
        assert score >= 0.9

    def test_calculate_liquidity_score_low_volume_wide_spread(self):
        """Test liquidity score with low volume and wide spread."""
        calc = EdgeCalculator()

        score = calc.calculate_liquidity_score(volume=100, spread=10)
        # Should be low
        assert score < 0.3

    def test_calculate_liquidity_score_zero_volume(self):
        """Test liquidity score with zero volume."""
        calc = EdgeCalculator()

        score = calc.calculate_liquidity_score(volume=0, spread=3)
        # Only spread component
        assert 0 < score < 0.4

    def test_evaluate_market_no_price(self):
        """Test evaluate_market when no price data."""
        calc = EdgeCalculator()

        with patch.object(calc, "_get_latest_price", return_value=None):
            result = calc.evaluate_market("TEST", "Test Market")
            assert result is None

    def test_evaluate_market_no_edge(self):
        """Test evaluate_market when edge below threshold."""
        calc = EdgeCalculator()

        # Price data with 55 bid/ask
        price_data = {
            "yes_bid": 55,
            "yes_ask": 56,
            "no_bid": 44,
            "no_ask": 45,
            "volume": 1000,
            "timestamp": datetime.utcnow(),
        }

        # Create a forecast that nearly matches market (no edge)
        from src.analysis.forecaster import Forecast

        mock_forecast = Forecast(
            market_ticker="TEST",
            probability_yes=0.55,  # Model says 55%, market asks 56%
            probability_no=0.45,
            confidence=0.7,
            factors={},
            method="rule_based",
            timestamp=datetime.utcnow(),
        )

        with patch.object(calc, "_get_latest_price", return_value=price_data):
            with patch.object(calc, "_get_latest_forecast", return_value=mock_forecast):
                with patch.object(calc, "_get_polymarket_price", return_value=None):
                    # Model prob ~ 55%, market prob = 56%
                    # Edge = -1%, below threshold
                    result = calc.evaluate_market("TEST", "Test Market")
                    assert result is None

    def test_evaluate_market_positive_edge(self):
        """Test evaluate_market with positive edge."""
        calc = EdgeCalculator(min_edge=0.05)

        price_data = {
            "yes_bid": 49,
            "yes_ask": 50,
            "no_bid": 49,
            "no_ask": 51,
            "volume": 5000,
            "timestamp": datetime.utcnow(),
        }

        # Create a forecast that disagrees with market
        from src.analysis.forecaster import Forecast

        mock_forecast = Forecast(
            market_ticker="TEST",
            probability_yes=0.60,  # Model says 60%
            probability_no=0.40,
            confidence=0.7,
            factors={},
            method="rule_based",
            timestamp=datetime.utcnow(),
        )

        with patch.object(calc, "_get_latest_price", return_value=price_data):
            with patch.object(calc, "_get_latest_forecast", return_value=mock_forecast):
                with patch.object(calc, "_get_polymarket_price", return_value=None):
                    result = calc.evaluate_market("TEST", "Test Market")

                    assert result is not None
                    assert result.ticker == "TEST"
                    assert result.edge > 0  # Positive edge (60% vs 50%)
                    assert result.side == "yes"
                    assert result.action == "buy"

    def test_evaluate_market_negative_edge(self):
        """Test evaluate_market with negative edge (buy NO)."""
        calc = EdgeCalculator(min_edge=0.05)

        price_data = {
            "yes_bid": 69,
            "yes_ask": 70,
            "no_bid": 29,
            "no_ask": 31,
            "volume": 5000,
            "timestamp": datetime.utcnow(),
        }

        from src.analysis.forecaster import Forecast

        mock_forecast = Forecast(
            market_ticker="TEST",
            probability_yes=0.55,  # Model says 55%, market says 70%
            probability_no=0.45,
            confidence=0.7,
            factors={},
            method="rule_based",
            timestamp=datetime.utcnow(),
        )

        with patch.object(calc, "_get_latest_price", return_value=price_data):
            with patch.object(calc, "_get_latest_forecast", return_value=mock_forecast):
                with patch.object(calc, "_get_polymarket_price", return_value=None):
                    result = calc.evaluate_market("TEST", "Test Market")

                    assert result is not None
                    assert result.edge < 0  # Negative edge (55% vs 70%)
                    assert result.side == "no"
                    assert result.action == "buy"

    def test_evaluate_market_with_arbitrage(self):
        """Test evaluate_market with Polymarket arbitrage signal."""
        calc = EdgeCalculator(min_edge=0.05, min_arbitrage=0.03)

        price_data = {
            "yes_bid": 49,
            "yes_ask": 50,
            "no_bid": 49,
            "no_ask": 51,
            "volume": 5000,
            "timestamp": datetime.utcnow(),
        }

        from src.analysis.forecaster import Forecast

        mock_forecast = Forecast(
            market_ticker="TEST",
            probability_yes=0.60,
            probability_no=0.40,
            confidence=0.7,
            factors={},
            method="rule_based",
            timestamp=datetime.utcnow(),
        )

        poly_data = {
            "yes_price": 0.55,  # Polymarket at 55%
            "no_price": 0.45,
            "volume": 10000,
            "timestamp": datetime.utcnow(),
        }

        with patch.object(calc, "_get_latest_price", return_value=price_data):
            with patch.object(calc, "_get_latest_forecast", return_value=mock_forecast):
                with patch.object(calc, "_get_polymarket_price", return_value=poly_data):
                    result = calc.evaluate_market("TEST", "Test Market")

                    assert result is not None
                    assert result.polymarket_price == 0.55
                    assert result.is_arbitrage is True
                    assert result.opportunity_type == OpportunityType.COMBINED

    def test_scan_markets(self):
        """Test scanning markets for opportunities."""
        calc = EdgeCalculator()

        # Mock database markets
        mock_market1 = MagicMock()
        mock_market1.ticker = "MKT1"
        mock_market1.title = "Market 1"

        mock_market2 = MagicMock()
        mock_market2.ticker = "MKT2"
        mock_market2.title = "Market 2"

        mock_opp = TradingOpportunity(
            ticker="MKT1",
            market_title="Market 1",
            opportunity_type=OpportunityType.EDGE,
            model_probability=0.65,
            market_probability=0.55,
            edge=0.10,
            expected_value=5.0,
            ev_pct=0.10,
            side="yes",
            action="buy",
            price=55,
            volume=5000,
            liquidity_score=0.7,
            opportunity_score=0.07,
        )

        with patch("src.analysis.edge.get_db_session") as mock_session_gen:
            mock_session = MagicMock()
            mock_context = MagicMock()
            mock_context.__enter__ = MagicMock(return_value=mock_session)
            mock_context.__exit__ = MagicMock(return_value=False)

            mock_query = MagicMock()
            mock_query.filter.return_value.limit.return_value.all.return_value = [
                mock_market1,
                mock_market2,
            ]
            mock_session.query.return_value = mock_query

            mock_session_gen.return_value = iter([mock_context])

            with patch.object(calc, "evaluate_market") as mock_eval:
                mock_eval.side_effect = [mock_opp, None]  # First market has opp

                results = calc.scan_markets(limit=10)

                assert len(results) == 1
                assert results[0].ticker == "MKT1"

    def test_calculate_portfolio_edge_empty(self):
        """Test portfolio metrics with no opportunities."""
        calc = EdgeCalculator()

        metrics = calc.calculate_portfolio_edge([])

        assert metrics["total_opportunities"] == 0
        assert metrics["avg_edge"] == 0.0
        assert metrics["total_ev"] == 0.0

    def test_calculate_portfolio_edge_with_opportunities(self):
        """Test portfolio metrics with opportunities."""
        calc = EdgeCalculator()

        opps = [
            TradingOpportunity(
                ticker="MKT1",
                market_title="Market 1",
                opportunity_type=OpportunityType.EDGE,
                model_probability=0.65,
                market_probability=0.55,
                edge=0.10,
                expected_value=5.0,
                ev_pct=0.10,
                side="yes",
                action="buy",
                price=55,
                volume=5000,
                liquidity_score=0.7,
                opportunity_score=0.07,
            ),
            TradingOpportunity(
                ticker="MKT2",
                market_title="Market 2",
                opportunity_type=OpportunityType.ARBITRAGE,
                model_probability=0.50,
                market_probability=0.55,
                edge=-0.05,
                expected_value=3.0,
                ev_pct=0.05,
                side="no",
                action="buy",
                price=55,
                volume=3000,
                liquidity_score=0.5,
                opportunity_score=0.025,
                is_arbitrage=True,
            ),
        ]

        metrics = calc.calculate_portfolio_edge(opps)

        assert metrics["total_opportunities"] == 2
        assert metrics["edge_opportunities"] == 1
        assert metrics["arbitrage_opportunities"] == 1
        assert abs(metrics["avg_edge"] - 0.075) < 0.001  # (0.10 + 0.05) / 2
        assert abs(metrics["total_ev"] - 8.0) < 0.001  # 5 + 3
        assert metrics["top_opportunity"] == "MKT1"


class TestConvenienceFunctions:
    """Tests for module-level convenience functions."""

    def test_get_edge_calculator_singleton(self):
        """Test get_edge_calculator returns singleton."""
        import src.analysis.edge as edge_module

        edge_module._calculator = None

        calc1 = get_edge_calculator()
        calc2 = get_edge_calculator()

        assert calc1 is calc2

    def test_scan_for_opportunities(self):
        """Test scan_for_opportunities convenience function."""
        import src.analysis.edge as edge_module

        edge_module._calculator = None

        with patch.object(EdgeCalculator, "scan_markets", return_value=[]):
            results = scan_for_opportunities()
            assert results == []

    def test_evaluate_market_convenience(self):
        """Test evaluate_market convenience function."""
        import src.analysis.edge as edge_module

        edge_module._calculator = None

        with patch.object(EdgeCalculator, "evaluate_market", return_value=None):
            result = evaluate_market("TEST")
            assert result is None


class TestOpportunityType:
    """Tests for OpportunityType enum."""

    def test_opportunity_types(self):
        """Test all opportunity types exist."""
        assert OpportunityType.EDGE.value == "edge"
        assert OpportunityType.ARBITRAGE.value == "arbitrage"
        assert OpportunityType.COMBINED.value == "combined"
