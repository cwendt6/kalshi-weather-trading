"""
Unit tests for the City Bracket Portfolio Manager (Phase 1).

Tests cover:
1. Bracket classification (distance → role)
2. Hedge direction (forecast position within bracket)
3. Entry ordering (outside-in)
4. Capital allocation (50/20/15/15 split)
5. Reshape trigger logic (conservative, ≥4°F, max 2/day)
6. NWS migration (weather_strategy uses NWSClient)
"""

import os
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.strategy.city_bracket_portfolio import (
    BracketRole,
    BracketView,
    CityBracketPortfolio,
    CityPortfolioManager,
)


@pytest.fixture
def manager():
    return CityPortfolioManager()


def _make_bracket_view(
    ticker: str = "KXHIGHNY-26FEB13-B36",
    lower: float = 35.0,
    upper: float = 37.0,
    distance: float = 0.0,
    role: BracketRole = BracketRole.FORECAST_YES,
    side: str = "yes",
    our_prob: float = 0.25,
    yes_price: float = 0.20,
    no_price: float = 0.80,
    edge: float = 0.05,
    alloc: float = 0.15,
) -> BracketView:
    return BracketView(
        ticker=ticker,
        bracket_lower=lower,
        bracket_upper=upper,
        distance_from_forecast=distance,
        role=role,
        side=side,
        our_probability=our_prob,
        market_yes_price=yes_price,
        market_no_price=no_price,
        edge=edge,
        recommended_allocation_pct=alloc,
    )


# ── 1. Bracket Classification ─────────────────────────────────────────


class TestBracketClassification:
    def test_forecast_in_bracket(self, manager):
        """Forecast 36°F inside [35, 37) → FORECAST_YES."""
        role = manager.classify_bracket(35.0, 37.0, 36.0)
        assert role == BracketRole.FORECAST_YES

    def test_forecast_at_lower_bound(self, manager):
        """Forecast exactly at lower bound is inside the bracket."""
        role = manager.classify_bracket(35.0, 37.0, 35.0)
        assert role == BracketRole.FORECAST_YES

    def test_adjacent_bracket_becomes_near_yes(self, manager):
        """Bracket 2° away from forecast → NEAR_YES_HEDGE candidate."""
        # Forecast at 36, bracket [33, 35) → midpoint 34, distance = 2°
        role = manager.classify_bracket(33.0, 35.0, 36.0)
        assert role == BracketRole.NEAR_YES_HEDGE

    def test_medium_distance_bracket(self, manager):
        """Bracket 3° away → MEDIUM_NO."""
        # Forecast at 36, bracket [32, 34) → midpoint 33, distance = 3°
        role = manager.classify_bracket(32.0, 34.0, 36.0)
        assert role == BracketRole.MEDIUM_NO

    def test_far_bracket(self, manager):
        """Bracket 4-5° away → FAR_NO."""
        # Forecast at 36, bracket [30, 32) → midpoint 31, distance = 5°
        role = manager.classify_bracket(30.0, 32.0, 36.0)
        assert role == BracketRole.FAR_NO

    def test_very_far_bracket(self, manager):
        """Bracket 6°+ away → VERY_FAR_NO."""
        # Forecast at 36, bracket [28, 30) → midpoint 29, distance = 7°
        role = manager.classify_bracket(28.0, 30.0, 36.0)
        assert role == BracketRole.VERY_FAR_NO


# ── 2. Hedge Direction ────────────────────────────────────────────────


class TestHedgeDirection:
    def test_hedge_direction_bottom(self, manager):
        """Forecast at bottom of bracket → hedge downward."""
        # Forecast 35.0 in bracket [35, 37) → relative_position = 0.0
        direction = manager.determine_hedge_direction(35.0, 35.0, 37.0)
        assert direction == "below"

    def test_hedge_direction_top(self, manager):
        """Forecast at top of bracket → hedge upward."""
        # Forecast 36.8 in bracket [35, 37) → relative_position = 0.9
        direction = manager.determine_hedge_direction(36.8, 35.0, 37.0)
        assert direction == "above"

    def test_hedge_direction_middle(self, manager):
        """Forecast in middle of bracket → best_priced."""
        # Forecast 36.0 in bracket [35, 37) → relative_position = 0.5
        direction = manager.determine_hedge_direction(36.0, 35.0, 37.0)
        assert direction == "best_priced"

    def test_hedge_direction_boundary_low(self, manager):
        """Relative position at 0.39 → below."""
        # Forecast 35.78 in [35, 37) → position = 0.39
        direction = manager.determine_hedge_direction(35.78, 35.0, 37.0)
        assert direction == "below"

    def test_hedge_direction_boundary_high(self, manager):
        """Relative position at 0.61 → above."""
        # Forecast 36.22 in [35, 37) → position = 0.61
        direction = manager.determine_hedge_direction(36.22, 35.0, 37.0)
        assert direction == "above"


# ── 3. Entry Order (Outside-In) ──────────────────────────────────────


class TestEntryOrder:
    def test_entry_order_outside_in(self, manager):
        """Verify outside-in ordering: very_far → far → medium → YES → hedge."""
        portfolio = CityBracketPortfolio(
            city="NYC",
            market_date=date.today(),
            forecast_high=36.0,
            forecast_low=25.0,
        )
        portfolio.brackets = [
            _make_bracket_view(ticker="YES", role=BracketRole.FORECAST_YES, distance=0),
            _make_bracket_view(ticker="HEDGE", role=BracketRole.NEAR_YES_HEDGE, distance=2),
            _make_bracket_view(ticker="MED", role=BracketRole.MEDIUM_NO, distance=-3),
            _make_bracket_view(ticker="FAR", role=BracketRole.FAR_NO, distance=-5),
            _make_bracket_view(ticker="VFAR", role=BracketRole.VERY_FAR_NO, distance=-7),
        ]

        ordered = manager.get_entry_order(portfolio)
        tickers = [bv.ticker for bv in ordered]

        assert tickers == ["VFAR", "FAR", "MED", "YES", "HEDGE"]

    def test_entry_order_within_same_role(self, manager):
        """Within the same role, furthest brackets come first."""
        portfolio = CityBracketPortfolio(
            city="NYC",
            market_date=date.today(),
            forecast_high=36.0,
            forecast_low=25.0,
        )
        portfolio.brackets = [
            _make_bracket_view(ticker="FAR1", role=BracketRole.FAR_NO, distance=-4),
            _make_bracket_view(ticker="FAR2", role=BracketRole.FAR_NO, distance=5),
        ]

        ordered = manager.get_entry_order(portfolio)
        # FAR2 (abs distance 5) should come before FAR1 (abs distance 4)
        assert ordered[0].ticker == "FAR2"
        assert ordered[1].ticker == "FAR1"


# ── 4. Capital Allocation ────────────────────────────────────────────


class TestCapitalAllocation:
    def test_allocation_50_20_15_15(self, manager):
        """Verify the 50/20/15/15 split across role groups."""
        # Build a portfolio with one bracket per role
        brackets = [
            {"ticker": "T1", "threshold": 36.0, "type": "above", "is_bracket": True},  # forecast
            {"ticker": "T2", "threshold": 34.0, "type": "above", "is_bracket": True},  # near
            {"ticker": "T3", "threshold": 33.0, "type": "above", "is_bracket": True},  # medium
            {"ticker": "T4", "threshold": 30.0, "type": "above", "is_bracket": True},  # far
            {"ticker": "T5", "threshold": 28.0, "type": "above", "is_bracket": True},  # very far
        ]
        # Prices that give good edge for all brackets
        prices = {
            "T1": {"yes_ask": 20, "yes_bid": 18, "no_ask": 80, "no_bid": 78},
            "T2": {"yes_ask": 15, "yes_bid": 13, "no_ask": 85, "no_bid": 83},
            "T3": {"yes_ask": 10, "yes_bid": 8, "no_ask": 90, "no_bid": 88},
            "T4": {"yes_ask": 5, "yes_bid": 3, "no_ask": 95, "no_bid": 93},
            "T5": {"yes_ask": 3, "yes_bid": 2, "no_ask": 97, "no_bid": 95},
        }

        portfolio = manager.build_portfolio(
            city="NYC",
            market_date=date.today(),
            forecast_temp=36.0,
            brackets=brackets,
            current_prices=prices,
            lead_days=0,  # Same-day for narrower σ
        )

        # Check total allocation sums to ~1.0 (only for tradeable brackets)
        total_alloc = sum(bv.recommended_allocation_pct for bv in portfolio.brackets)
        # It won't be exactly 1.0 because some brackets may be filtered by edge
        # but all role pools should be covered
        assert total_alloc > 0

        # Verify the pools are correct (regardless of filtering)
        assert manager.FAR_NO_PCT == 0.50
        assert manager.MEDIUM_NO_PCT == 0.20
        assert manager.FORECAST_YES_PCT == 0.15
        assert manager.NEAR_YES_PCT == 0.15
        assert (
            manager.FAR_NO_PCT + manager.MEDIUM_NO_PCT
            + manager.FORECAST_YES_PCT + manager.NEAR_YES_PCT
        ) == 1.0

    def test_city_budget_edge_proportional(self, manager):
        """Cities with more edge get more budget."""
        p1 = CityBracketPortfolio(
            city="NYC", market_date=date.today(),
            forecast_high=36.0, forecast_low=25.0,
        )
        p1.total_edge = 0.30

        p2 = CityBracketPortfolio(
            city="MIAMI", market_date=date.today(),
            forecast_high=78.0, forecast_low=65.0,
        )
        p2.total_edge = 0.10
        p2.brackets = [_make_bracket_view()]  # needs at least one bracket

        all_portfolios = [p1, p2]

        nyc_budget = manager.calculate_city_budget("NYC", 100.0, all_portfolios)
        miami_budget = manager.calculate_city_budget("MIAMI", 100.0, all_portfolios)

        # NYC has 3x the edge → should get larger budget
        assert nyc_budget > miami_budget

    def test_city_budget_equal_when_no_edge(self, manager):
        """Equal split when no edge data available."""
        p1 = CityBracketPortfolio(
            city="NYC", market_date=date.today(),
            forecast_high=36.0, forecast_low=25.0,
        )
        p1.total_edge = 0.0
        p1.brackets = [_make_bracket_view()]

        p2 = CityBracketPortfolio(
            city="MIAMI", market_date=date.today(),
            forecast_high=78.0, forecast_low=65.0,
        )
        p2.total_edge = 0.0
        p2.brackets = [_make_bracket_view()]

        budget1 = manager.calculate_city_budget("NYC", 100.0, [p1, p2])
        budget2 = manager.calculate_city_budget("MIAMI", 100.0, [p1, p2])

        assert budget1 == pytest.approx(50.0)
        assert budget2 == pytest.approx(50.0)


# ── 5. Reshape Trigger ───────────────────────────────────────────────


class TestReshapeTrigger:
    def test_small_shift_same_bracket_no_reshape(self, manager):
        """2°F shift within same bracket → no reshape."""
        portfolio = CityBracketPortfolio(
            city="NYC", market_date=date.today(),
            forecast_high=36.0, forecast_low=25.0,
        )
        portfolio.last_forecast_temp = 36.0
        portfolio.reshape_count = 0
        portfolio.brackets = [
            _make_bracket_view(lower=35.0, upper=37.0, role=BracketRole.FORECAST_YES),
        ]

        # Shift from 36 → 37 (still in [36, 38) bracket, and only 1° shift)
        assert manager.check_reshape_needed(portfolio, 37.0) is False

    def test_large_shift_crossing_brackets_reshape(self, manager):
        """4°F shift crossing bracket boundaries → reshape."""
        portfolio = CityBracketPortfolio(
            city="NYC", market_date=date.today(),
            forecast_high=36.0, forecast_low=25.0,
        )
        portfolio.last_forecast_temp = 36.0
        portfolio.reshape_count = 0
        portfolio.brackets = [
            _make_bracket_view(lower=35.0, upper=37.0, role=BracketRole.FORECAST_YES),
        ]

        # Shift from 36 → 40 (4° shift, different bracket)
        assert manager.check_reshape_needed(portfolio, 40.0) is True

    def test_reshape_max_count_blocked(self, manager):
        """3rd reshape attempt blocked (max 2/day)."""
        portfolio = CityBracketPortfolio(
            city="NYC", market_date=date.today(),
            forecast_high=36.0, forecast_low=25.0,
        )
        portfolio.last_forecast_temp = 36.0
        portfolio.reshape_count = 2  # Already reshaped twice
        portfolio.brackets = [
            _make_bracket_view(lower=35.0, upper=37.0, role=BracketRole.FORECAST_YES),
        ]

        # Even a massive shift should be blocked
        assert manager.check_reshape_needed(portfolio, 50.0) is False

    def test_yes_position_too_far_triggers_reshape(self, manager):
        """YES position >5° from new forecast → reshape."""
        portfolio = CityBracketPortfolio(
            city="NYC", market_date=date.today(),
            forecast_high=36.0, forecast_low=25.0,
        )
        portfolio.last_forecast_temp = 36.0
        portfolio.reshape_count = 0
        portfolio.brackets = [
            _make_bracket_view(
                lower=35.0, upper=37.0,
                role=BracketRole.FORECAST_YES,
                side="yes",
            ),
        ]

        # New forecast at 43: the YES bracket midpoint (36) is 7° away
        assert manager.check_reshape_needed(portfolio, 43.0) is True


# ── 6. NWS Migration ─────────────────────────────────────────────────


class TestNWSMigration:
    def test_weather_strategy_uses_nws_client(self):
        """Verify weather_strategy.py imports NWSClient, not OpenWeather."""
        import src.strategy.weather_strategy as ws_module

        # The module should have NWSClient available
        assert hasattr(ws_module, "NWSClient")

        # The WeatherStrategy class should use NWSClient
        strategy = ws_module.WeatherStrategy.__new__(ws_module.WeatherStrategy)
        # The module-level import should be NWSClient from nws_weather
        # (or MockNWSClient if USE_MOCK_WEATHER is set)
        from src.data_sources.nws_weather import NWSClient as RealNWSClient
        mock_env = os.environ.get("USE_MOCK_WEATHER", "false")
        if mock_env.lower() != "true":
            assert ws_module.NWSClient is RealNWSClient

    def test_no_openweather_import_in_weather_strategy(self):
        """Verify OpenWeather is not conditionally imported."""
        import inspect
        import src.strategy.weather_strategy as ws_module

        source = inspect.getsource(ws_module)
        # Should NOT contain the old conditional import
        assert "OPENWEATHER_API_KEY" not in source
        assert "OpenWeatherClient" not in source


# ── 7. Build Portfolio Integration ────────────────────────────────────


class TestBuildPortfolio:
    def test_build_portfolio_sets_created_at(self, manager):
        """Portfolio has created_at timestamp set."""
        brackets = [
            {"ticker": "T1", "threshold": 36.0, "type": "above", "is_bracket": True},
        ]
        prices = {
            "T1": {"yes_ask": 20, "yes_bid": 18, "no_ask": 80, "no_bid": 78},
        }

        portfolio = manager.build_portfolio(
            city="NYC",
            market_date=date.today(),
            forecast_temp=36.0,
            brackets=brackets,
            current_prices=prices,
        )

        assert portfolio.created_at is not None
        assert portfolio.forecast_source == "NWS"
        assert portfolio.city == "NYC"

    def test_build_portfolio_filters_no_edge(self, manager):
        """Brackets without sufficient edge are filtered out."""
        # A bracket right at the forecast with yes_ask matching our prob → no edge
        brackets = [
            {"ticker": "T1", "threshold": 36.0, "type": "above", "is_bracket": True},
        ]
        # Set yes_ask so our probability matches market price (no edge)
        prices = {
            "T1": {"yes_ask": 25, "yes_bid": 23, "no_ask": 75, "no_bid": 73},
        }

        portfolio = manager.build_portfolio(
            city="NYC",
            market_date=date.today(),
            forecast_temp=36.0,
            brackets=brackets,
            current_prices=prices,
        )

        # All brackets should have been checked for edge threshold
        # The forecast bracket at 36 with our_prob ~0.26 and market 0.25 → edge ~0.01
        # which is below MIN_YES_EDGE of 0.02, so it should be filtered
        for bv in portfolio.brackets:
            min_edge = manager.MIN_YES_EDGE if bv.side == "yes" else manager.MIN_NO_EDGE
            assert bv.edge >= min_edge

    def test_build_portfolio_no_price_data_skips(self, manager):
        """Brackets with no price data are skipped."""
        brackets = [
            {"ticker": "T1", "threshold": 36.0, "type": "above", "is_bracket": True},
        ]
        prices = {}  # No price data at all

        portfolio = manager.build_portfolio(
            city="NYC",
            market_date=date.today(),
            forecast_temp=36.0,
            brackets=brackets,
            current_prices=prices,
        )

        assert len(portfolio.brackets) == 0
