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



# ═══════════════════════════════════════════════════════════════════
# BLS Client Tests
# ═══════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════
# GDELT Client Tests
# ═══════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════
# Metaculus Client Tests
# ═══════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════
# API Ninjas Client Tests
# ═══════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════
# Census Client Tests
# ═══════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════
# Dome Client Tests
# ═══════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════
# DataAggregator Tests
# ═══════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════
# Integration Tests
# ═══════════════════════════════════════════════════════════════════

