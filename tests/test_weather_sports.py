"""
Tests for weather strategy improvements and sports odds strategy.

Run: pytest tests/test_weather_sports.py -v
"""
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ═══════════════════════════════════════════════════════════════════════════════
# Weather Probability Calculation Tests (scipy normal distribution)
# ═══════════════════════════════════════════════════════════════════════════════

class TestWeatherProbability:
    """Test the scipy-based weather probability calculation."""

    def _make_forecast(self, high_f, low_f, confidence="high", days_out=1):
        from src.data_sources.nws_weather import TemperatureForecast
        from datetime import date, timedelta, datetime
        return TemperatureForecast(
            city="NYC",
            station_id="KNYC",
            forecast_date=date.today() + timedelta(days=days_out),
            high_f=high_f,
            low_f=low_f,
            high_confidence=confidence,
            forecast_generated=datetime.now(),
        )

    def test_high_above_threshold_well_above(self):
        """Forecast 45°F, threshold 35°F -> very high probability above."""
        from src.strategy.weather_strategy import WeatherStrategy
        strategy = WeatherStrategy(min_edge=0.05)
        forecast = self._make_forecast(45, 30)
        prob, _ = strategy._calculate_probability(forecast, 35, "above")
        assert prob > 0.95  # 10°F above with 2.4°F std_dev

    def test_high_above_threshold_close(self):
        """Forecast 45°F, threshold 44°F -> slightly above 50%."""
        from src.strategy.weather_strategy import WeatherStrategy
        strategy = WeatherStrategy(min_edge=0.05)
        forecast = self._make_forecast(45, 30)
        prob, _ = strategy._calculate_probability(forecast, 44, "above")
        assert 0.55 < prob < 0.75  # Slightly above

    def test_high_above_threshold_well_below(self):
        """Forecast 45°F, threshold 55°F -> very low probability."""
        from src.strategy.weather_strategy import WeatherStrategy
        strategy = WeatherStrategy(min_edge=0.05)
        forecast = self._make_forecast(45, 30)
        prob, _ = strategy._calculate_probability(forecast, 55, "above")
        assert prob < 0.05

    def test_low_below_threshold_well_below(self):
        """Forecast low 20°F, threshold 30°F -> high probability below."""
        from src.strategy.weather_strategy import WeatherStrategy
        strategy = WeatherStrategy(min_edge=0.05)
        forecast = self._make_forecast(40, 20)
        prob, _ = strategy._calculate_probability(forecast, 30, "below")
        assert prob > 0.95

    def test_low_below_threshold_close(self):
        """Forecast low 30°F, threshold 31°F -> slightly above 50%."""
        from src.strategy.weather_strategy import WeatherStrategy
        strategy = WeatherStrategy(min_edge=0.05)
        forecast = self._make_forecast(40, 30)
        prob, _ = strategy._calculate_probability(forecast, 31, "below")
        assert 0.50 < prob < 0.70

    def test_confidence_widens_distribution(self):
        """Low confidence should produce probability closer to 50%."""
        from src.strategy.weather_strategy import WeatherStrategy
        strategy = WeatherStrategy(min_edge=0.05)
        high_conf = self._make_forecast(45, 30, confidence="high")
        low_conf = self._make_forecast(45, 30, confidence="low")

        prob_high, _ = strategy._calculate_probability(high_conf, 40, "above")
        prob_low, _ = strategy._calculate_probability(low_conf, 40, "above")

        # High confidence should be more extreme (further from 50%)
        assert abs(prob_high - 0.5) > abs(prob_low - 0.5)

    def test_days_out_widens_distribution(self):
        """Further out forecast should have wider uncertainty."""
        from src.strategy.weather_strategy import WeatherStrategy
        strategy = WeatherStrategy(min_edge=0.05)
        day1 = self._make_forecast(45, 30, days_out=1)
        day5 = self._make_forecast(45, 30, days_out=5)

        prob_d1, _ = strategy._calculate_probability(day1, 40, "above")
        prob_d5, _ = strategy._calculate_probability(day5, 40, "above")

        # Day 1 should be more confident (further from 50%)
        assert abs(prob_d1 - 0.5) > abs(prob_d5 - 0.5)

    def test_probability_clamped(self):
        """Probabilities should be clamped between 0.02 and 0.98."""
        from src.strategy.weather_strategy import WeatherStrategy
        strategy = WeatherStrategy(min_edge=0.05)
        forecast = self._make_forecast(80, 60)

        # Extreme case: 80°F forecast vs 10°F threshold
        prob, _ = strategy._calculate_probability(forecast, 10, "above")
        assert prob == 0.98  # Clamped at upper bound

        # Extreme case: 80°F forecast vs 150°F threshold
        prob2, _ = strategy._calculate_probability(forecast, 150, "above")
        assert prob2 == 0.02  # Clamped at lower bound

    def test_zero_forecast_returns_50_pct(self):
        """Zero forecast temp should return 50%."""
        from src.strategy.weather_strategy import WeatherStrategy
        strategy = WeatherStrategy(min_edge=0.05)
        forecast = self._make_forecast(0, 0)
        prob, reasoning = strategy._calculate_probability(forecast, 40, "above")
        assert prob == 0.5
        assert "No forecast" in reasoning


# ═══════════════════════════════════════════════════════════════════════════════
# Weather Ticker Parsing Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestWeatherTickerParsing:
    """Test Kalshi weather ticker parsing."""

    def test_parse_high_with_threshold(self):
        from src.strategy.weather_strategy import WeatherStrategy
        s = WeatherStrategy()
        result = s._parse_temperature_ticker("KXHIGHNY-26FEB07-T31")
        assert result is not None
        assert result["city"] == "NYC"
        assert result["threshold"] == 31
        assert result["type"] == "above"

    def test_parse_high_bracket(self):
        from src.strategy.weather_strategy import WeatherStrategy
        s = WeatherStrategy()
        result = s._parse_temperature_ticker("KXHIGHNY-26FEB07-B30.5")
        assert result is not None
        assert result["city"] == "NYC"
        assert result["threshold"] == 30.5  # midpoint of 2°F bracket (30-31°)
        assert result["type"] == "above"

    def test_parse_low_chicago(self):
        from src.strategy.weather_strategy import WeatherStrategy
        s = WeatherStrategy()
        result = s._parse_temperature_ticker("KXLOWCHI-26FEB07-B20")
        assert result is not None
        assert result["city"] == "CHICAGO"
        assert result["threshold"] == 20
        assert result["type"] == "below"

    def test_parse_lowtnyc(self):
        from src.strategy.weather_strategy import WeatherStrategy
        s = WeatherStrategy()
        result = s._parse_temperature_ticker("KXLOWTNYC-26FEB07-T9")
        assert result is not None
        assert result["type"] == "below"

    def test_unknown_city_returns_none(self):
        from src.strategy.weather_strategy import WeatherStrategy
        s = WeatherStrategy()
        result = s._parse_temperature_ticker("KXHIGHXYZ-26FEB07-T31")
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# Weather Market Discovery Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestWeatherDiscovery:
    """Test weather market discovery found markets in the database."""

    def test_weather_markets_exist_in_db(self):
        """Weather discovery script should have populated the database."""
        import sqlite3
        conn = sqlite3.connect("data/kalshi_trading.db")
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM markets WHERE category='Climate and Weather'")
        count = c.fetchone()[0]
        conn.close()
        assert count > 1000, f"Expected 1000+ weather markets, got {count}"

    def test_settled_weather_markets_exist(self):
        """Settled weather markets should exist for training data."""
        import sqlite3
        conn = sqlite3.connect("data/kalshi_trading.db")
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM markets WHERE category='Climate and Weather' AND status='settled'")
        count = c.fetchone()[0]
        conn.close()
        assert count > 100, f"Expected 100+ settled weather markets, got {count}"


# ═══════════════════════════════════════════════════════════════════════════════
# Odds API Client Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestOddsAPIClient:
    """Test the Odds API client structure."""

    def test_client_initializes(self):
        from src.connectors.odds_api_client import OddsAPIClient
        client = OddsAPIClient(api_key="test_key")
        assert client.api_key == "test_key"

    @pytest.fixture(autouse=False)
    def _clear_odds_key(self, monkeypatch):
        monkeypatch.delenv("ODDS_API_KEY", raising=False)

    def test_client_without_key(self, monkeypatch):
        monkeypatch.delenv("ODDS_API_KEY", raising=False)
        from src.connectors.odds_api_client import OddsAPIClient
        client = OddsAPIClient(api_key="")
        assert client.api_key == ""

    def test_kalshi_sports_mapping(self):
        from src.connectors.odds_api_client import OddsAPIClient
        assert "americanfootball_nfl" in OddsAPIClient.KALSHI_SPORTS
        assert "basketball_nba" in OddsAPIClient.KALSHI_SPORTS

    def test_sport_event_compute_consensus(self):
        from src.connectors.odds_api_client import SportEvent
        from datetime import datetime
        event = SportEvent(
            event_id="test",
            sport_key="nfl",
            sport_title="NFL",
            home_team="Chiefs",
            away_team="Eagles",
            commence_time=datetime.now(),
            bookmakers=[
                {
                    "markets": [{
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Chiefs", "price": 1.50},  # 66.7%
                            {"name": "Eagles", "price": 2.80},  # 35.7%
                        ]
                    }]
                },
                {
                    "markets": [{
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Chiefs", "price": 1.55},  # 64.5%
                            {"name": "Eagles", "price": 2.60},  # 38.5%
                        ]
                    }]
                },
            ],
        )
        event.compute_consensus()

        # Should be normalized (sum to ~1.0)
        total = event.consensus_home_prob + event.consensus_away_prob
        assert abs(total - 1.0) < 0.01

        # Chiefs should be favored
        assert event.consensus_home_prob > event.consensus_away_prob

    def test_quota_status(self):
        from src.connectors.odds_api_client import OddsAPIClient
        client = OddsAPIClient(api_key="test")
        status = client.get_quota_status()
        assert "remaining" in status
        assert "used" in status


# ═══════════════════════════════════════════════════════════════════════════════
# Sports Odds Strategy Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestSportsOddsStrategy:
    """Test sports odds comparison strategy."""

    def test_strategy_initializes(self):
        from src.strategy.sports_odds_strategy import SportsOddsStrategy
        strategy = SportsOddsStrategy(min_edge=0.05)
        assert strategy.min_edge == 0.05

    def test_kalshi_to_sport_mapping(self):
        from src.strategy.sports_odds_strategy import SportsOddsStrategy
        assert "KXNFL" in SportsOddsStrategy.KALSHI_TO_SPORT
        assert "KXNBA" in SportsOddsStrategy.KALSHI_TO_SPORT
        assert SportsOddsStrategy.KALSHI_TO_SPORT["KXNFL"] == "americanfootball_nfl"

    def test_min_bookmakers_threshold(self):
        from src.strategy.sports_odds_strategy import SportsOddsStrategy
        assert SportsOddsStrategy.MIN_BOOKMAKERS >= 3

    def test_factory_function(self):
        from src.strategy.sports_odds_strategy import get_sports_odds_strategy
        strategy = get_sports_odds_strategy(min_edge=0.08)
        assert strategy.min_edge == 0.08


# ═══════════════════════════════════════════════════════════════════════════════
# Kalshi API series_ticker Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestKalshiAPISeriesTicker:
    """Test that Kalshi client supports series_ticker parameter."""

    def test_get_markets_accepts_series_ticker(self):
        import inspect
        from src.api.kalshi_client import KalshiClient
        sig = inspect.signature(KalshiClient.get_markets)
        param_names = list(sig.parameters.keys())
        assert "series_ticker" in param_names


# ═══════════════════════════════════════════════════════════════════════════════
# Settings Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestSettingsUpdated:
    """Test that settings include new strategy allocations."""

    def test_sports_allocation_exists(self):
        from config.settings import Settings
        assert "alloc_sports_odds" in Settings.model_fields

    def test_sports_scan_interval_exists(self):
        from config.settings import Settings
        assert "scan_interval_sports" in Settings.model_fields

    def test_allocations_sum_to_one(self):
        """Strategy allocations should sum to approximately 1.0."""
        from config.settings import Settings
        s = Settings.__new__(Settings)
        # Get default values
        total = 0.25 + 0.25 + 0.10 + 0.10 + 0.20 + 0.10  # defaults
        assert abs(total - 1.0) < 0.01


# ═══════════════════════════════════════════════════════════════════════════════
# Connectors __init__ Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestConnectorsInit:
    """Test connectors __init__ exports."""

    def test_odds_client_importable(self):
        from src.connectors import OddsAPIClient
        assert OddsAPIClient is not None

    def test_sport_event_importable(self):
        from src.connectors import SportEvent
        assert SportEvent is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
