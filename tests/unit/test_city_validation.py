"""Tests for city list centralization (Phase 6)."""
import pytest
from unittest.mock import patch

from src.data_sources.nws_weather import get_active_cities, KALSHI_STATIONS


class TestCityValidation:
    def test_get_active_cities_default(self):
        """No env var -> returns all KALSHI_STATIONS keys."""
        with patch.dict("os.environ", {}, clear=True):
            # Remove WEATHER_ENABLED_CITIES if present
            import os
            os.environ.pop("WEATHER_ENABLED_CITIES", None)
            cities = get_active_cities()
        assert set(cities) == set(KALSHI_STATIONS.keys())

    def test_get_active_cities_env_override(self):
        """Env var -> returns filtered list."""
        with patch.dict("os.environ", {"WEATHER_ENABLED_CITIES": "NYC,MIAMI"}):
            cities = get_active_cities()
        assert cities == ["NYC", "MIAMI"]

    def test_invalid_city_warning(self):
        """Invalid city in env var -> filtered out, warning logged."""
        with patch.dict("os.environ", {"WEATHER_ENABLED_CITIES": "NYC,FAKECITY"}):
            cities = get_active_cities()
        assert cities == ["NYC"]
        assert "FAKECITY" not in cities

    def test_city_bracket_portfolio_uses_canonical(self):
        """CityPortfolioManager.ACTIVE_CITIES returns get_active_cities()."""
        from src.strategy.city_bracket_portfolio import CityPortfolioManager
        mgr = CityPortfolioManager()
        with patch.dict("os.environ", {"WEATHER_ENABLED_CITIES": "NYC,MIAMI"}):
            assert mgr.ACTIVE_CITIES == ["NYC", "MIAMI"]

    def test_observation_scanner_uses_canonical(self):
        """ObservationSettledScanner uses get_active_cities()."""
        with patch.dict("os.environ", {"WEATHER_ENABLED_CITIES": "NYC,MIAMI"}):
            from src.strategy.observation_scanner import ObservationSettledScanner
            scanner = ObservationSettledScanner()
            assert scanner.enabled_cities == ["NYC", "MIAMI"]
