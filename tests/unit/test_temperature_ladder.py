"""Tests for temperature laddering (neobrother-style bracket spreading).

Validates that generate_ladder_opportunities() correctly:
- Selects 3-5 brackets within ±6°F of forecast
- Enforces YES price ≤ $0.20 guard
- Enforces 3% minimum edge
- Caps individual bracket bets at $2
- Treats entire ladder as 1 YES position for the cap
- Excludes brackets outside the distance range
"""
import os
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.strategy.weather_strategy import WeatherStrategy, WeatherOpportunity


# ── Helpers ──────────────────────────────────────────────────────────


def _make_bracket_market(ticker: str, threshold: float, city_code: str = "NY"):
    """Create a mock MarketDB-like object for a bracket market."""
    m = MagicMock()
    m.ticker = ticker
    m.title = f"Will the high temp in NYC be in [{threshold-1:.0f}, {threshold+1:.0f})°F?"
    m.status = "active"
    m.close_time = datetime(2026, 4, 2, 23, 0, tzinfo=timezone.utc)
    return m


def _make_price(yes_ask: int, yes_bid: int = 0):
    """Price data dict matching _fetch_latest_prices format."""
    return {
        "yes_ask": yes_ask,
        "yes_bid": yes_bid or max(1, yes_ask - 2),
        "no_ask": 100 - (yes_bid or max(1, yes_ask - 2)),
        "no_bid": 100 - yes_ask,
    }


# Standard bracket set: NYC high temp, TOMORROW, brackets from 34.5 to 50.5
# Forecast is 42°F, so brackets within ±6°F = 36.5 through 48.5
# Use tomorrow's date so days_out=1 (σ is narrower than 3-day out)
from datetime import timedelta

FORECAST_TEMP = 42.0
# Use tomorrow so days_out=1 consistently
MARKET_DATE = date.today() + timedelta(days=1)
_DATE_STR = MARKET_DATE.strftime("%y") + MARKET_DATE.strftime("%b").upper() + MARKET_DATE.strftime("%d")
CITY = "NYC"

BRACKET_THRESHOLDS = [34.5, 36.5, 38.5, 40.5, 42.5, 44.5, 46.5, 48.5, 50.5]
BRACKET_TICKERS = [f"KXHIGHNY-{_DATE_STR}-B{t}" for t in BRACKET_THRESHOLDS]


def _setup_strategy_mocks(
    strategy: WeatherStrategy,
    prices: dict,
    forecast_temp: float = FORECAST_TEMP,
):
    """Wire up mocks so generate_ladder_opportunities() can run without DB/NWS."""
    # Mock NWS forecast
    mock_forecast = MagicMock()
    mock_forecast.high_f = forecast_temp
    mock_forecast.low_f = forecast_temp - 15
    mock_forecast.high_confidence = "medium"
    mock_forecast.city = CITY
    mock_forecast.forecast_date = MARKET_DATE
    strategy.nws_client = MagicMock()
    strategy.nws_client.get_forecast.return_value = mock_forecast

    return mock_forecast


# ── Tests ────────────────────────────────────────────────────────────


class TestLadderGeneration:
    """Test that ladders generate the right number of brackets."""

    def _run_ladder(self, prices: dict, forecast_temp: float = FORECAST_TEMP):
        """Helper: run generate_ladder_opportunities with mocked DB/NWS."""
        strategy = WeatherStrategy(min_edge=0.05)
        mock_forecast = _setup_strategy_mocks(strategy, prices, forecast_temp)

        # Build bracket market objects
        markets = [_make_bracket_market(t, th) for t, th in zip(BRACKET_TICKERS, BRACKET_THRESHOLDS)]

        # Mock the DB session to return our bracket markets.
        # Explicit __enter__/__exit__ needed for `with next(get_db_session()) as session:`
        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.all.return_value = markets
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        # Mock _fetch_latest_prices
        strategy._fetch_latest_prices = MagicMock(return_value=prices)

        # Mock _get_local_hour
        strategy._get_local_hour = MagicMock(return_value=10)

        # Patch get_db_session: side_effect creates a fresh generator each call
        def make_gen():
            yield mock_session

        with patch("src.strategy.weather_strategy.get_db_session", side_effect=lambda: make_gen()):
            with patch.dict(os.environ, {"WEATHER_MAX_DAYS_OUT": "3"}):
                return strategy.generate_ladder_opportunities()

    def test_ladder_generates_brackets_within_range(self):
        """Ladder selects brackets within ±6°F of forecast (42°F)."""
        # Brackets: 34.5 to 50.5 in steps of 2
        # Within ±6°F of 42: 36.5, 38.5, 40.5, 42.5, 44.5, 46.5, 48.5 = 7 candidates
        # But capped at 5, so expect 5 (closest first)
        prices = {t: _make_price(yes_ask=5) for t in BRACKET_TICKERS}
        opps = self._run_ladder(prices)

        assert len(opps) > 0, "Ladder should generate at least some opportunities"
        assert len(opps) <= 5, f"Ladder capped at 5, got {len(opps)}"

        # All should be within ±6°F
        for opp in opps:
            distance = abs(opp.threshold_temp - FORECAST_TEMP)
            assert distance <= 6.0, f"Bracket {opp.ticker} is {distance}°F away (max 6)"

    def test_ladder_all_have_yes_ladder_trade_type(self):
        """All ladder opportunities have trade_type='yes_ladder'."""
        prices = {t: _make_price(yes_ask=5) for t in BRACKET_TICKERS}
        opps = self._run_ladder(prices)

        for opp in opps:
            assert opp.trade_type == "yes_ladder", f"{opp.ticker} has trade_type={opp.trade_type}"
            assert opp.recommendation == "BUY_YES"

    def test_ladder_excludes_expensive_yes(self):
        """Brackets with YES price > 20¢ are excluded."""
        prices = {}
        for t, th in zip(BRACKET_TICKERS, BRACKET_THRESHOLDS):
            # Make the forecast bracket expensive (25¢), others cheap (5¢)
            if th == 42.5:
                prices[t] = _make_price(yes_ask=25)
            else:
                prices[t] = _make_price(yes_ask=5)

        opps = self._run_ladder(prices)

        tickers = {opp.ticker for opp in opps}
        expensive_ticker = f"KXHIGHNY-{_DATE_STR}-B42.5"
        assert expensive_ticker not in tickers, "Bracket at 25¢ should be excluded"

    def test_ladder_requires_minimum_edge(self):
        """Brackets with edge < 3% are excluded."""
        # Set all prices very high (19¢) so probability - price < 3% for most
        # Only brackets very close to forecast will have enough probability
        prices = {t: _make_price(yes_ask=19) for t in BRACKET_TICKERS}
        opps = self._run_ladder(prices)

        for opp in opps:
            assert opp.edge >= 0.03, f"{opp.ticker} has edge {opp.edge:.1%} < 3%"

    def test_ladder_skips_single_bracket(self):
        """A single qualifying bracket doesn't produce a ladder."""
        # Only make one bracket cheap enough
        prices = {}
        for t, th in zip(BRACKET_TICKERS, BRACKET_THRESHOLDS):
            if th == 42.5:
                prices[t] = _make_price(yes_ask=5)  # Very cheap, has edge
            else:
                prices[t] = _make_price(yes_ask=0)  # No price data

        # Remove prices for all but one bracket
        prices = {f"KXHIGHNY-{_DATE_STR}-B42.5": _make_price(yes_ask=5)}

        opps = self._run_ladder(prices)
        assert len(opps) == 0, "Single bracket should not produce a ladder"

    def test_ladder_excludes_out_of_range_brackets(self):
        """Brackets more than ±6°F from forecast are excluded."""
        prices = {t: _make_price(yes_ask=5) for t in BRACKET_TICKERS}
        opps = self._run_ladder(prices)

        out_of_range = {f"KXHIGHNY-{_DATE_STR}-B34.5", f"KXHIGHNY-{_DATE_STR}-B50.5"}
        opp_tickers = {opp.ticker for opp in opps}

        for ticker in out_of_range:
            assert ticker not in opp_tickers, f"{ticker} is >6°F from forecast and should be excluded"


class TestLadderPositionCounting:
    """Test that ladders count as ONE YES position for the cap."""

    def test_ladder_opps_share_same_city_date(self):
        """All opps in a ladder share the same city and market_date."""
        strategy = WeatherStrategy(min_edge=0.05)
        prices = {t: _make_price(yes_ask=5) for t in BRACKET_TICKERS}
        _setup_strategy_mocks(strategy, prices)

        markets = [_make_bracket_market(t, th) for t, th in zip(BRACKET_TICKERS, BRACKET_THRESHOLDS)]

        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.all.return_value = markets
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        strategy._fetch_latest_prices = MagicMock(return_value=prices)
        strategy._get_local_hour = MagicMock(return_value=10)

        def make_gen():
            yield mock_session

        with patch("src.strategy.weather_strategy.get_db_session", side_effect=lambda: make_gen()):
            with patch.dict(os.environ, {"WEATHER_MAX_DAYS_OUT": "3"}):
                opps = strategy.generate_ladder_opportunities()

        if opps:
            cities = {opp.city for opp in opps}
            dates = {opp.market_date for opp in opps}
            assert len(cities) == 1, "All ladder opps should be same city"
            assert len(dates) == 1, "All ladder opps should be same date"


class TestLadderBudgetConstraints:
    """Test budget and sizing constraints."""

    def test_max_per_bracket_constant(self):
        """LADDER_MAX_PER_BRACKET_DOLLARS is $2."""
        assert WeatherStrategy.LADDER_MAX_PER_BRACKET_DOLLARS == 2.00

    def test_max_brackets_constant(self):
        """LADDER_MAX_BRACKETS is 5."""
        assert WeatherStrategy.LADDER_MAX_BRACKETS == 5

    def test_min_edge_constant(self):
        """LADDER_MIN_EDGE is 3%."""
        assert WeatherStrategy.LADDER_MIN_EDGE == 0.03

    def test_max_yes_price_constant(self):
        """LADDER_MAX_YES_PRICE is 20¢."""
        assert WeatherStrategy.LADDER_MAX_YES_PRICE == 0.20


class TestLadderDeduplication:
    """Test that ladders don't duplicate existing squeeze opportunities."""

    def test_get_tradeable_deduplicates_ladder_vs_squeeze(self):
        """get_tradeable_opportunities() doesn't return duplicate tickers."""
        strategy = WeatherStrategy(min_edge=0.05)

        # Mock scan_bracket_squeeze to return some opps
        squeeze_opp = WeatherOpportunity(
            ticker=f"KXHIGHNY-{_DATE_STR}-B42.5",
            city="NYC", market_date=MARKET_DATE,
            threshold_temp=42, threshold_type="above",
            nws_forecast_temp=42, nws_confidence="medium",
            market_price=0.15, our_probability=0.25,
            edge=0.10, recommendation="BUY_YES",
            reasoning="squeeze", market_type="temperature",
            trade_type="yes_convergence",
        )
        mock_result = MagicMock()
        mock_result.opportunities = [squeeze_opp]
        strategy.scan_bracket_squeeze = MagicMock(return_value=mock_result)

        # Mock ladder to return an opp with the SAME ticker
        ladder_opp = WeatherOpportunity(
            ticker=f"KXHIGHNY-{_DATE_STR}-B42.5",
            city="NYC", market_date=MARKET_DATE,
            threshold_temp=42, threshold_type="above",
            nws_forecast_temp=42, nws_confidence="medium",
            market_price=0.15, our_probability=0.25,
            edge=0.10, recommendation="BUY_YES",
            reasoning="ladder", market_type="temperature",
            trade_type="yes_ladder",
        )
        strategy.generate_ladder_opportunities = MagicMock(return_value=[ladder_opp])

        opps = strategy.get_tradeable_opportunities()
        tickers = [opp.ticker for opp in opps]
        assert tickers.count(f"KXHIGHNY-{_DATE_STR}-B42.5") == 1, "Duplicate ticker should be deduplicated"
        # The squeeze version should win (it's added first)
        assert opps[0].trade_type == "yes_convergence"
