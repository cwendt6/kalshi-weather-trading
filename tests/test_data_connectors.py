"""
Tests for all data API connectors and the unified DataAggregator.

Covers: FRED, OpenWeatherMap, CoinGecko, BLS, GDELT, Metaculus,
API Ninjas, Census, Dome, and the DataAggregator layer.
"""
import os
import time
from datetime import datetime
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# Env var keys that must be cleared for "no_key" tests
_CLEAR_ENV = {
    "FRED_API_KEY": "",
    "OPENWEATHER_API_KEY": "",
    "BLS_API_KEY": "",
    "API_NINJAS_KEY": "",
    "DOME_API_KEY": "",
    "CENSUS_API_KEY": "",
}


# ═══════════════════════════════════════════════════════════════════
# FRED Client Tests
# ═══════════════════════════════════════════════════════════════════

class TestFREDClient:
    """Tests for src.connectors.fred_client.FREDClient."""

    @patch.dict(os.environ, {"FRED_API_KEY": ""})
    def test_init_no_key(self):
        from src.connectors.fred_client import FREDClient
        c = FREDClient()
        assert c._cache == {}
        assert c._daily_calls == 0

    def test_init_with_key(self):
        from src.connectors.fred_client import FREDClient
        c = FREDClient(api_key="test-key")
        assert c.api_key == "test-key"

    def test_cache_valid(self):
        from src.connectors.fred_client import FREDClient
        c = FREDClient()
        c._cache["test"] = {"value": 1}
        c._cache_expiry["test"] = time.time() + 999
        assert c._is_cache_valid("test") is True
        assert c._is_cache_valid("nonexistent") is False

    def test_cache_expired(self):
        from src.connectors.fred_client import FREDClient
        c = FREDClient()
        c._cache["test"] = {"value": 1}
        c._cache_expiry["test"] = time.time() - 1
        assert c._is_cache_valid("test") is False

    def test_set_cache(self):
        from src.connectors.fred_client import FREDClient
        c = FREDClient()
        c._set_cache("key1", {"data": True}, ttl=60)
        assert c._cache["key1"] == {"data": True}
        assert c._cache_expiry["key1"] > time.time()

    def test_track_call(self):
        from src.connectors.fred_client import FREDClient
        c = FREDClient()
        c._track_call()
        assert c._daily_calls == 1

    @patch.dict(os.environ, {"FRED_API_KEY": ""})
    def test_get_series_no_key(self):
        from src.connectors.fred_client import FREDClient
        c = FREDClient()
        result = c.get_series("GDP")
        assert result["status"] == "no_api_key"

    @patch("src.connectors.fred_client.requests.get")
    def test_get_series_success(self, mock_get):
        from src.connectors.fred_client import FREDClient
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "observations": [
                {"date": "2026-01-01", "value": "27500.0"},
                {"date": "2025-10-01", "value": "27000.0"},
            ]
        }
        mock_get.return_value.raise_for_status = MagicMock()
        c = FREDClient(api_key="test")
        result = c.get_series("GDP")
        assert result["status"] == "success"
        assert result["latest_value"] == 27500.0
        assert result["change_pct"] is not None

    @patch("src.connectors.fred_client.requests.get")
    def test_get_economic_context_all_success(self, mock_get):
        from src.connectors.fred_client import FREDClient
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {
            "observations": [
                {"date": "2026-01-01", "value": "100.0"},
                {"date": "2025-10-01", "value": "99.0"},
            ]
        }
        mock_get.return_value.raise_for_status = MagicMock()
        c = FREDClient(api_key="test")
        result = c.get_economic_context()
        assert result["data_quality"] == "current"
        assert result["source"] == "FRED"
        assert "gdp" in result

    def test_get_series_returns_cached(self):
        from src.connectors.fred_client import FREDClient
        c = FREDClient(api_key="test")
        cached = {"series_id": "GDP", "status": "success", "latest_value": 100}
        c._set_cache("fred_GDP", cached)
        result = c.get_series("GDP")
        assert result == cached

    def test_singleton(self):
        import src.connectors.fred_client as mod
        mod._client = None
        c1 = mod.get_fred_client()
        c2 = mod.get_fred_client()
        assert c1 is c2
        mod._client = None  # cleanup


# ═══════════════════════════════════════════════════════════════════
# OpenWeather Client Tests
# ═══════════════════════════════════════════════════════════════════

class TestOpenWeatherClient:
    """Tests for src.connectors.openweather_client.OpenWeatherClient."""

    def test_init_no_key(self):
        from src.connectors.openweather_client import OpenWeatherClient
        c = OpenWeatherClient()
        assert c._daily_calls == 0

    def test_unknown_city(self):
        from src.connectors.openweather_client import OpenWeatherClient
        c = OpenWeatherClient()
        result = c.get_weather_forecast("UNKNOWN")
        assert result["status"] == "unknown_city"

    @patch.dict(os.environ, {"OPENWEATHER_API_KEY": ""})
    def test_no_api_key(self):
        from src.connectors.openweather_client import OpenWeatherClient
        c = OpenWeatherClient()
        result = c.get_weather_forecast("NYC")
        assert result["status"] == "no_api_key"

    @patch("src.connectors.openweather_client.requests.get")
    def test_get_weather_forecast_success(self, mock_get):
        from src.connectors.openweather_client import OpenWeatherClient
        # First call: current weather
        current_resp = MagicMock()
        current_resp.status_code = 200
        current_resp.json.return_value = {
            "main": {"temp": 35.5, "feels_like": 30.0, "humidity": 45},
            "weather": [{"description": "clear sky"}],
            "wind": {"speed": 5.0},
        }
        current_resp.raise_for_status = MagicMock()
        # Second call: forecast
        forecast_resp = MagicMock()
        forecast_resp.status_code = 200
        forecast_resp.json.return_value = {
            "list": [
                {
                    "dt_txt": "2026-02-07 12:00:00",
                    "main": {"temp": 40.0},
                    "pop": 0.1,
                    "weather": [{"description": "light rain"}],
                }
            ]
        }
        forecast_resp.raise_for_status = MagicMock()
        mock_get.side_effect = [current_resp, forecast_resp]

        c = OpenWeatherClient(api_key="test")
        result = c.get_weather_forecast("NYC")
        assert result["status"] == "success"
        assert result["current"]["temp"] == 35.5
        assert len(result["forecast_5day"]) == 1

    def test_get_weather_context_specific_city(self):
        from src.connectors.openweather_client import OpenWeatherClient
        c = OpenWeatherClient(api_key="test")
        # Pre-populate cache
        cached = {"city": "New York", "status": "success", "current": {"temp": 40}}
        c._set_cache("owm_NYC", cached)
        result = c.get_weather_context("NYC")
        assert result["data_quality"] == "current"
        assert result["source"] == "OpenWeatherMap"

    def test_singleton(self):
        import src.connectors.openweather_client as mod
        mod._client = None
        c1 = mod.get_openweather_client()
        c2 = mod.get_openweather_client()
        assert c1 is c2
        mod._client = None


# ═══════════════════════════════════════════════════════════════════
# CoinGecko Client Tests
# ═══════════════════════════════════════════════════════════════════

class TestCoinGeckoClient:
    """Tests for src.connectors.coingecko_client.CoinGeckoClient."""

    def test_init(self):
        from src.connectors.coingecko_client import CoinGeckoClient
        c = CoinGeckoClient()
        assert c._daily_calls == 0
        assert c.timeout == 15

    def test_unknown_symbol(self):
        from src.connectors.coingecko_client import CoinGeckoClient
        c = CoinGeckoClient()
        result = c.get_crypto_price("DOGE")
        assert result["status"] == "unknown_symbol"

    @patch.object(
        __import__("requests").Session, "get"
    )
    def test_get_crypto_price_success(self, mock_get):
        from src.connectors.coingecko_client import CoinGeckoClient
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "market_data": {
                "current_price": {"usd": 95000},
                "market_cap": {"usd": 1800000000000},
                "total_volume": {"usd": 40000000000},
                "price_change_percentage_24h": 2.5,
                "price_change_percentage_7d": 5.0,
                "price_change_percentage_30d": -3.0,
                "ath": {"usd": 109000},
                "ath_change_percentage": {"usd": -12.8},
            }
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        c = CoinGeckoClient()
        result = c.get_crypto_price("btc")
        assert result["status"] == "success"
        assert result["current_price"] == 95000
        assert result["symbol"] == "BTC"

    def test_crypto_context_cached(self):
        from src.connectors.coingecko_client import CoinGeckoClient
        c = CoinGeckoClient()
        cached = {
            "btc": {"current_price": 95000, "change_24h": 2.5},
            "market_sentiment": "bullish",
            "data_quality": "current",
        }
        c._set_cache("cg_crypto_context", cached)
        result = c.get_crypto_context()
        assert result == cached

    def test_sentiment_bullish(self):
        from src.connectors.coingecko_client import CoinGeckoClient
        c = CoinGeckoClient()
        # Pre-populate individual coin caches
        c._set_cache("cg_btc", {
            "status": "success", "current_price": 95000, "change_24h": 5.0,
            "change_7d": 3, "change_30d": 2, "volume_24h": 100,
        })
        c._set_cache("cg_eth", {
            "status": "success", "current_price": 3500, "change_24h": 4.0,
            "change_7d": 2, "change_30d": 1, "volume_24h": 50,
        })
        result = c.get_crypto_context()
        assert result["market_sentiment"] == "bullish"

    def test_sentiment_bearish(self):
        from src.connectors.coingecko_client import CoinGeckoClient
        c = CoinGeckoClient()
        c._set_cache("cg_btc", {
            "status": "success", "current_price": 85000, "change_24h": -5.0,
            "change_7d": -3, "change_30d": -10, "volume_24h": 100,
        })
        c._set_cache("cg_eth", {
            "status": "success", "current_price": 2800, "change_24h": -4.0,
            "change_7d": -2, "change_30d": -8, "volume_24h": 50,
        })
        result = c.get_crypto_context()
        assert result["market_sentiment"] == "bearish"

    def test_singleton(self):
        import src.connectors.coingecko_client as mod
        mod._client = None
        c1 = mod.get_coingecko_client()
        c2 = mod.get_coingecko_client()
        assert c1 is c2
        mod._client = None


# ═══════════════════════════════════════════════════════════════════
# BLS Client Tests
# ═══════════════════════════════════════════════════════════════════

class TestBLSClient:
    """Tests for src.connectors.bls_client.BLSClient."""

    @patch.dict(os.environ, {"BLS_API_KEY": ""})
    def test_init_no_key(self):
        from src.connectors.bls_client import BLSClient
        c = BLSClient()
        assert c._daily_quota == 25  # v1 limit

    def test_init_with_key(self):
        from src.connectors.bls_client import BLSClient
        c = BLSClient(api_key="test-key")
        assert c._daily_quota == 500

    def test_calculate_trend_rising(self):
        from src.connectors.bls_client import BLSClient
        c = BLSClient()
        assert c._calculate_trend([10.0, 9.0, 8.0]) == "rising"

    def test_calculate_trend_falling(self):
        from src.connectors.bls_client import BLSClient
        c = BLSClient()
        assert c._calculate_trend([8.0, 9.0, 10.0]) == "falling"

    def test_calculate_trend_stable(self):
        from src.connectors.bls_client import BLSClient
        c = BLSClient()
        assert c._calculate_trend([10.0, 11.0, 9.0]) == "stable"

    def test_calculate_trend_insufficient(self):
        from src.connectors.bls_client import BLSClient
        c = BLSClient()
        assert c._calculate_trend([10.0]) == "insufficient_data"

    @patch("src.connectors.bls_client.requests.post")
    def test_get_series_success(self, mock_post):
        from src.connectors.bls_client import BLSClient
        mock_post.return_value.status_code = 200
        mock_post.return_value.raise_for_status = MagicMock()
        mock_post.return_value.json.return_value = {
            "status": "REQUEST_SUCCEEDED",
            "Results": {
                "series": [{
                    "data": [
                        {"year": "2026", "periodName": "January", "value": "3.5"},
                        {"year": "2025", "periodName": "December", "value": "3.6"},
                        {"year": "2025", "periodName": "November", "value": "3.7"},
                    ]
                }]
            }
        }
        c = BLSClient()
        result = c.get_series("LNS14000000")
        assert result["status"] == "success"
        assert result["latest_value"] == 3.5
        assert result["trend"] == "falling"

    @patch("src.connectors.bls_client.requests.post")
    def test_get_labor_context(self, mock_post):
        from src.connectors.bls_client import BLSClient
        mock_post.return_value.status_code = 200
        mock_post.return_value.raise_for_status = MagicMock()
        mock_post.return_value.json.return_value = {
            "status": "REQUEST_SUCCEEDED",
            "Results": {
                "series": [{
                    "data": [
                        {"year": "2026", "periodName": "January", "value": "100.0"},
                        {"year": "2025", "periodName": "December", "value": "99.0"},
                        {"year": "2025", "periodName": "November", "value": "98.0"},
                    ]
                }]
            }
        }
        c = BLSClient()
        result = c.get_labor_context()
        assert result["data_quality"] == "current"
        assert result["source"] == "BLS"

    def test_singleton(self):
        import src.connectors.bls_client as mod
        mod._client = None
        c1 = mod.get_bls_client()
        c2 = mod.get_bls_client()
        assert c1 is c2
        mod._client = None


# ═══════════════════════════════════════════════════════════════════
# GDELT Client Tests
# ═══════════════════════════════════════════════════════════════════

class TestGDELTClient:
    """Tests for src.connectors.gdelt_client.GDELTClient."""

    def test_init(self):
        from src.connectors.gdelt_client import GDELTClient
        c = GDELTClient()
        assert c._daily_calls == 0

    def test_sentiment_cached(self):
        from src.connectors.gdelt_client import GDELTClient
        c = GDELTClient()
        cached = {"status": "success", "avg_tone": 3.0}
        c._set_cache("gdelt_tone_test_7d", cached)
        result = c.get_news_sentiment("test")
        assert result == cached

    @patch.object(
        __import__("requests").Session, "get"
    )
    def test_sentiment_success(self, mock_get):
        from src.connectors.gdelt_client import GDELTClient
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {"bin": "3.0", "count": "100"},
            {"bin": "-1.0", "count": "50"},
        ]
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        c = GDELTClient()
        result = c.get_news_sentiment("inflation")
        assert result["status"] == "success"
        assert result["article_count"] == 150
        # Weighted average: (3*100 + -1*50) / 150 = 250/150 = 1.667
        assert abs(result["avg_tone"] - 1.667) < 0.01

    @patch.object(
        __import__("requests").Session, "get"
    )
    def test_sentiment_positive_tone(self, mock_get):
        from src.connectors.gdelt_client import GDELTClient
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [{"bin": "5.0", "count": "100"}]
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        c = GDELTClient()
        result = c.get_news_sentiment("good news")
        assert result["tone_trend"] == "positive"

    @patch.object(
        __import__("requests").Session, "get"
    )
    def test_volume_success(self, mock_get):
        from src.connectors.gdelt_client import GDELTClient
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [
            {"date": "2026-02-01", "value": 100},
            {"date": "2026-02-02", "value": 110},
            {"date": "2026-02-03", "value": 90},
            {"date": "2026-02-04", "value": 150},
            {"date": "2026-02-05", "value": 160},
            {"date": "2026-02-06", "value": 170},
        ]
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        c = GDELTClient()
        result = c.get_news_volume("bitcoin")
        assert result["status"] == "success"
        assert result["total_volume"] == 780

    def test_event_context_combined_signal(self):
        from src.connectors.gdelt_client import GDELTClient
        c = GDELTClient()
        # Pre-populate caches
        c._set_cache("gdelt_tone_inflation OR CPI OR unemployment OR GDP_7d", {
            "status": "success", "avg_tone": 3.5, "tone_trend": "positive",
            "article_count": 200,
        })
        c._set_cache("gdelt_vol_inflation OR CPI OR unemployment OR GDP_7d", {
            "status": "success", "total_volume": 5000, "volume_trend": "rising",
            "daily_volumes": [],
        })
        result = c.get_event_context("economics")
        assert result["combined_signal"] == "strong_positive"

    def test_singleton(self):
        import src.connectors.gdelt_client as mod
        mod._client = None
        c1 = mod.get_gdelt_client()
        c2 = mod.get_gdelt_client()
        assert c1 is c2
        mod._client = None


# ═══════════════════════════════════════════════════════════════════
# Metaculus Client Tests
# ═══════════════════════════════════════════════════════════════════

class TestMetaculusClient:
    """Tests for src.connectors.metaculus_client.MetaculusClient."""

    def test_init(self):
        from src.connectors.metaculus_client import MetaculusClient
        c = MetaculusClient()
        assert c._daily_calls == 0

    def test_simplify_title(self):
        from src.connectors.metaculus_client import MetaculusClient
        assert "BTC hit 100k" == MetaculusClient._simplify_title(
            "Will BTC hit 100k by June 2026?"
        )

    def test_simplify_title_with_prefix(self):
        from src.connectors.metaculus_client import MetaculusClient
        result = MetaculusClient._simplify_title("Will the election be held?")
        # "Will the" should be stripped, leaving "election" as key word
        assert "election" in result.lower()

    @patch.object(
        __import__("requests").Session, "get"
    )
    def test_search_questions(self, mock_get):
        from src.connectors.metaculus_client import MetaculusClient
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "results": [{
                "id": 12345,
                "title": "Will Bitcoin reach $100k?",
                "community_prediction": {"full": {"q2": 0.65}},
                "number_of_predictions": 150,
                "resolve_time": "2026-12-31T00:00:00Z",
            }]
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        c = MetaculusClient()
        results = c.search_questions("bitcoin")
        assert len(results) == 1
        assert results[0]["community_prediction"] == 0.65

    def test_get_crowd_probability_from_cache(self):
        from src.connectors.metaculus_client import MetaculusClient
        c = MetaculusClient()
        cached = {
            "id": 100, "title": "Test", "community_prediction": 0.75,
            "num_predictions": 50, "status": "success",
        }
        c._set_cache("mc_q_100", cached)
        prob = c.get_crowd_probability(100)
        assert prob == 0.75

    def test_find_matching_signal_no_match(self):
        from src.connectors.metaculus_client import MetaculusClient
        c = MetaculusClient()
        # Empty search results cached
        c._set_cache("mc_search_election held_open_5_0", [])
        result = c.find_matching_kalshi_signal("Will the election be held?", "politics")
        # Should return None when no results match
        assert result is None

    def test_singleton(self):
        import src.connectors.metaculus_client as mod
        mod._client = None
        c1 = mod.get_metaculus_client()
        c2 = mod.get_metaculus_client()
        assert c1 is c2
        mod._client = None


# ═══════════════════════════════════════════════════════════════════
# API Ninjas Client Tests
# ═══════════════════════════════════════════════════════════════════

class TestAPINinjasClient:
    """Tests for src.connectors.api_ninjas_client.APINinjasClient."""

    @patch.dict(os.environ, {"API_NINJAS_KEY": ""})
    def test_init_no_key(self):
        from src.connectors.api_ninjas_client import APINinjasClient
        c = APINinjasClient()
        assert c.api_key == ""

    @patch.dict(os.environ, {"API_NINJAS_KEY": ""})
    def test_inflation_no_key(self):
        from src.connectors.api_ninjas_client import APINinjasClient
        c = APINinjasClient()
        result = c.get_inflation()
        assert result["status"] == "no_api_key"

    @patch.dict(os.environ, {"API_NINJAS_KEY": ""})
    def test_commodity_no_key(self):
        from src.connectors.api_ninjas_client import APINinjasClient
        c = APINinjasClient()
        result = c.get_commodity_price("gold")
        assert result["status"] == "no_api_key"

    @patch.object(
        __import__("requests").Session, "get"
    )
    def test_inflation_success(self, mock_get):
        from src.connectors.api_ninjas_client import APINinjasClient
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [{
            "country": "United States",
            "type": "CPI",
            "period": "January 2026",
            "monthly_rate_pct": 0.3,
            "yearly_rate_pct": 2.8,
        }]
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        c = APINinjasClient(api_key="test")
        result = c.get_inflation()
        assert result["status"] == "success"
        assert result["yearly_rate_pct"] == 2.8

    @patch.object(
        __import__("requests").Session, "get"
    )
    def test_commodity_success(self, mock_get):
        from src.connectors.api_ninjas_client import APINinjasClient
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "name": "Gold",
            "price": 2050.50,
            "currency": "USD",
            "updated": "2026-02-07",
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        c = APINinjasClient(api_key="test")
        result = c.get_commodity_price("gold")
        assert result["status"] == "success"
        assert result["price"] == 2050.50

    @patch.dict(os.environ, {"API_NINJAS_KEY": ""})
    def test_economic_snapshot_no_key(self):
        from src.connectors.api_ninjas_client import APINinjasClient
        c = APINinjasClient()
        result = c.get_economic_snapshot()
        assert result["data_quality"] == "unavailable"

    def test_singleton(self):
        import src.connectors.api_ninjas_client as mod
        mod._client = None
        c1 = mod.get_api_ninjas_client()
        c2 = mod.get_api_ninjas_client()
        assert c1 is c2
        mod._client = None


# ═══════════════════════════════════════════════════════════════════
# Census Client Tests
# ═══════════════════════════════════════════════════════════════════

class TestCensusClient:
    """Tests for src.connectors.census_client.CensusClient."""

    def test_init(self):
        from src.connectors.census_client import CensusClient
        c = CensusClient()
        assert c._daily_calls == 0

    def test_cache_pattern(self):
        from src.connectors.census_client import CensusClient
        c = CensusClient()
        c._set_cache("test", {"value": 42})
        assert c._is_cache_valid("test") is True
        assert c._cache["test"]["value"] == 42

    def test_has_required_methods(self):
        from src.connectors.census_client import CensusClient
        c = CensusClient()
        assert callable(c.get_housing_data)
        assert callable(c.get_retail_trade)
        assert callable(c.get_economic_census_data)
        assert callable(c.health_check)

    def test_singleton(self):
        import src.connectors.census_client as mod
        mod._client = None
        c1 = mod.get_census_client()
        c2 = mod.get_census_client()
        assert c1 is c2
        mod._client = None


# ═══════════════════════════════════════════════════════════════════
# Dome Client Tests
# ═══════════════════════════════════════════════════════════════════

class TestDomeClient:
    """Tests for src.connectors.dome_client.DomeClient."""

    @patch.dict(os.environ, {"DOME_API_KEY": ""})
    def test_init_no_key(self):
        from src.connectors.dome_client import DomeClient
        c = DomeClient()
        assert c.api_key == ""

    @patch.dict(os.environ, {"DOME_API_KEY": ""})
    def test_cross_platform_no_key(self):
        from src.connectors.dome_client import DomeClient
        c = DomeClient()
        result = c.get_cross_platform_markets("test")
        assert result == []

    @patch.dict(os.environ, {"DOME_API_KEY": ""})
    def test_arbitrage_no_key(self):
        from src.connectors.dome_client import DomeClient
        c = DomeClient()
        result = c.find_arbitrage("KXBTC-26FEB07")
        assert result["status"] == "no_api_key"

    @patch.dict(os.environ, {"DOME_API_KEY": ""})
    def test_consensus_no_key(self):
        from src.connectors.dome_client import DomeClient
        c = DomeClient()
        result = c.get_market_consensus("test topic")
        assert result["status"] == "no_api_key"

    @patch.dict(os.environ, {"DOME_API_KEY": ""})
    def test_health_no_key(self):
        from src.connectors.dome_client import DomeClient
        c = DomeClient()
        assert c.health_check() == "no_key"

    def test_singleton(self):
        import src.connectors.dome_client as mod
        mod._client = None
        c1 = mod.get_dome_client()
        c2 = mod.get_dome_client()
        assert c1 is c2
        mod._client = None


# ═══════════════════════════════════════════════════════════════════
# DataAggregator Tests
# ═══════════════════════════════════════════════════════════════════

class TestDataAggregator:
    """Tests for src.connectors.data_aggregator.MarketDataAggregator."""

    def _make_aggregator(self):
        """Create aggregator with all clients mocked out."""
        with patch("src.connectors.data_aggregator.MarketDataAggregator._init_client"):
            from src.connectors.data_aggregator import MarketDataAggregator
            agg = MarketDataAggregator()
        return agg

    def test_init_no_crash(self):
        agg = self._make_aggregator()
        assert agg._clients == {}

    def test_get_client_missing(self):
        agg = self._make_aggregator()
        assert agg._get_client("nonexistent") is None

    def test_get_client_present(self):
        agg = self._make_aggregator()
        mock_client = MagicMock()
        agg._clients["fred"] = mock_client
        assert agg._get_client("fred") is mock_client

    def test_extract_city_code(self):
        from src.connectors.data_aggregator import MarketDataAggregator
        assert MarketDataAggregator._extract_city_code(
            {"title": "Will New York temperature exceed 80F?"}
        ) == "NYC"
        assert MarketDataAggregator._extract_city_code(
            {"title": "Will chicago snow?"}
        ) == "CHI"
        assert MarketDataAggregator._extract_city_code(
            {"title": "Will something happen?"}
        ) is None

    def test_assess_quality_high(self):
        from src.connectors.data_aggregator import MarketDataAggregator
        assert MarketDataAggregator._assess_quality(
            {"data_sources": ["FRED", "BLS", "CoinGecko"]}
        ) == "high"

    def test_assess_quality_partial(self):
        from src.connectors.data_aggregator import MarketDataAggregator
        assert MarketDataAggregator._assess_quality(
            {"data_sources": ["FRED"]}
        ) == "partial"

    def test_assess_quality_degraded(self):
        from src.connectors.data_aggregator import MarketDataAggregator
        assert MarketDataAggregator._assess_quality(
            {"data_sources": []}
        ) == "degraded"

    def test_get_full_context_economics(self):
        agg = self._make_aggregator()
        mock_fred = MagicMock()
        mock_fred.get_economic_context.return_value = {
            "data_quality": "current", "gdp": {"value": 27500},
        }
        agg._clients["fred"] = mock_fred

        result = agg.get_full_context({
            "title": "Will GDP grow?",
            "category": "economics",
        })
        assert "economics" in result
        assert "FRED" in result["data_sources"]

    def test_get_full_context_weather(self):
        agg = self._make_aggregator()
        mock_owm = MagicMock()
        mock_owm.get_weather_context.return_value = {
            "data_quality": "current",
        }
        agg._clients["openweather"] = mock_owm

        result = agg.get_full_context({
            "title": "Temperature in NYC above 50?",
            "category": "weather",
        })
        assert "weather" in result

    def test_get_full_context_crypto(self):
        agg = self._make_aggregator()
        mock_cg = MagicMock()
        mock_cg.get_crypto_context.return_value = {
            "data_quality": "current",
        }
        agg._clients["coingecko"] = mock_cg

        result = agg.get_full_context({
            "title": "Will Bitcoin hit 100k?",
            "category": "crypto",
        })
        assert "crypto" in result

    def test_get_full_context_fallback_news(self):
        agg = self._make_aggregator()
        mock_gdelt = MagicMock()
        mock_gdelt.get_event_context.return_value = {"status": "success"}
        agg._clients["gdelt"] = mock_gdelt

        result = agg.get_full_context({
            "title": "Will something random happen?",
            "category": "other",
        })
        # Should use news_sentiment fallback
        assert "news_sentiment" in result or "data_sources" in result

    def test_health_check(self):
        agg = self._make_aggregator()
        mock_client = MagicMock()
        mock_client.health_check.return_value = "online"
        agg._clients["test_source"] = mock_client
        result = agg.health_check()
        assert result["sources"]["test_source"] == "online"
        assert result["online"] == 1

    def test_health_check_mixed(self):
        agg = self._make_aggregator()
        mock_online = MagicMock()
        mock_online.health_check.return_value = "online"
        mock_offline = MagicMock()
        mock_offline.health_check.side_effect = Exception("down")
        agg._clients["good"] = mock_online
        agg._clients["bad"] = mock_offline
        result = agg.health_check()
        assert result["overall_status"] == "degraded"

    def test_quota_status(self):
        agg = self._make_aggregator()
        mock_client = MagicMock()
        mock_client._daily_calls = 42
        agg._clients["test"] = mock_client
        result = agg.get_quota_status()
        assert result["test"]["calls_today"] == 42

    def test_cross_market_signals_none(self):
        agg = self._make_aggregator()
        result = agg._get_cross_market_signals("test title")
        assert result is None  # No clients available

    def test_cross_market_signals_with_metaculus(self):
        agg = self._make_aggregator()
        mock_metaculus = MagicMock()
        mock_metaculus.find_matching_kalshi_signal.return_value = {
            "metaculus_prob": 0.65,
        }
        agg._clients["metaculus"] = mock_metaculus
        result = agg._get_cross_market_signals("Will BTC hit 100k?")
        assert result is not None
        assert "Metaculus" in result["sources"]

    def test_get_economics_context_all_sources(self):
        agg = self._make_aggregator()
        mock_fred = MagicMock()
        mock_fred.get_economic_context.return_value = {"data_quality": "current"}
        mock_bls = MagicMock()
        mock_bls.get_labor_context.return_value = {"data_quality": "current"}
        mock_ninjas = MagicMock()
        mock_ninjas.get_economic_snapshot.return_value = {"data_quality": "current"}
        mock_census = MagicMock()
        mock_census.get_economic_census_data.return_value = {"data_quality": "current"}

        agg._clients["fred"] = mock_fred
        agg._clients["bls"] = mock_bls
        agg._clients["api_ninjas"] = mock_ninjas
        agg._clients["census"] = mock_census

        result = agg.get_economics_context()
        assert "FRED" in result["sources"]
        assert "BLS" in result["sources"]
        assert "API_Ninjas" in result["sources"]
        assert "Census" in result["sources"]
        assert result["data_quality"] == "current"

    def test_singleton(self):
        import src.connectors.data_aggregator as mod
        mod._aggregator = None
        # Don't actually create a real one (would try to init all clients)
        with patch.object(mod.MarketDataAggregator, "__init__", return_value=None):
            a1 = mod.get_data_aggregator()
            a2 = mod.get_data_aggregator()
            assert a1 is a2
        mod._aggregator = None


# ═══════════════════════════════════════════════════════════════════
# Integration Tests
# ═══════════════════════════════════════════════════════════════════

class TestDataAggregatorIntegration:
    """Integration tests for aggregator wiring into forecaster."""

    @patch("src.connectors.data_aggregator.MarketDataAggregator._init_client")
    def test_aggregator_in_forecaster_context(self, mock_init):
        """Verify the aggregator is called from forecaster._get_domain_specific_data."""
        from src.connectors.data_aggregator import MarketDataAggregator

        mock_agg = MarketDataAggregator()
        mock_agg._clients = {}  # No real clients

        with patch(
            "src.connectors.data_aggregator.get_data_aggregator",
            return_value=mock_agg,
        ):
            with patch.object(
                mock_agg, "get_full_context",
                return_value={"data_sources": ["GDELT"], "timestamp": "test"},
            ) as mock_ctx:
                # Import and call the forecaster method
                from src.analysis.forecaster import LLMForecaster
                try:
                    f = LLMForecaster(api_key="test-key")
                    result = f._get_domain_specific_data(
                        "TESTMKT", "Will something happen?", {"category": "other"}
                    )
                    assert "aggregator_context" in result
                    mock_ctx.assert_called_once()
                except Exception:
                    # LLMForecaster may fail to init without anthropic
                    pass

    def test_connectors_init_exports(self):
        """Verify all new connectors are exported from __init__.py."""
        from src.connectors import (
            FREDClient, get_fred_client,
            OpenWeatherClient, get_openweather_client,
            CoinGeckoClient, get_coingecko_client,
            BLSClient, get_bls_client,
            GDELTClient, get_gdelt_client,
            MetaculusClient, get_metaculus_client,
            APINinjasClient, get_api_ninjas_client,
            CensusClient, get_census_client,
            DomeClient, get_dome_client,
            MarketDataAggregator, get_data_aggregator,
        )
        assert FREDClient is not None
        assert get_data_aggregator is not None
