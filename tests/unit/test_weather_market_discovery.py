"""
Unit tests for weather market discovery system.

Tests the dynamic weather market discovery that replaces hardcoded lists.
"""

import pytest
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from datetime import datetime, timezone

from src.data.weather_market_discovery import (
    WeatherMarketDiscovery,
    WeatherMarketType,
    get_weather_market_discovery,
)
from src.api.models import Market


class TestWeatherMarketDiscovery:
    """Test WeatherMarketDiscovery functionality."""

    def setup_method(self):
        """Set up test fixtures."""
        self.discovery = WeatherMarketDiscovery(cache_ttl_seconds=60)

    def test_is_weather_series_daily_high(self):
        """Test identification of daily high temperature series."""
        assert self.discovery._is_weather_series("KXHIGHNY")
        assert self.discovery._is_weather_series("KXHIGHCHI")
        assert self.discovery._is_weather_series("KXHIGHMIA")
        assert self.discovery._is_weather_series("KXHIGHDENVER")
        assert not self.discovery._is_weather_series("KXMODEL")
        assert not self.discovery._is_weather_series("INVALID")

    def test_is_weather_series_daily_low(self):
        """Test identification of daily low temperature series."""
        assert self.discovery._is_weather_series("KXLOWNY")
        assert self.discovery._is_weather_series("KXLOWTNYC")
        assert self.discovery._is_weather_series("KXLOWTCHI")
        assert self.discovery._is_weather_series("KXLOWMIA")

    def test_is_weather_series_daily_rain(self):
        """Test identification of daily rain series (no M suffix)."""
        assert self.discovery._is_weather_series("KXRAINNYC")
        assert self.discovery._is_weather_series("KXRAINCHI")
        # Monthly rain should also match (it's still a weather series)
        assert self.discovery._is_weather_series("KXRAINNYCM")

    def test_is_weather_series_monthly_rain(self):
        """Test identification of monthly rain series (M suffix)."""
        assert self.discovery._is_weather_series("KXRAINNYCM")
        assert self.discovery._is_weather_series("KXRAINLAM")
        assert self.discovery._is_weather_series("KXRAINSFM")

    def test_is_weather_series_monthly_snow(self):
        """Test identification of monthly snow series."""
        assert self.discovery._is_weather_series("KXBOSSNOWM")
        assert self.discovery._is_weather_series("KXNYCSNOWM")
        assert self.discovery._is_weather_series("KXCHISNOWM")

    def test_is_weather_series_daily_snow(self):
        """Test identification of daily snow series."""
        assert self.discovery._is_weather_series("KXSNOWNY")
        assert self.discovery._is_weather_series("KXSNOWCHI")
        # Should NOT match monthly snow
        assert not self.discovery._is_weather_series("KXSNOWNYM")

    def test_classify_series_high_temp(self):
        """Test classification of high temperature series."""
        assert (
            self.discovery._classify_series("KXHIGHNY")
            == WeatherMarketType.DAILY_HIGH_TEMP
        )
        assert (
            self.discovery._classify_series("KXHIGHMIA")
            == WeatherMarketType.DAILY_HIGH_TEMP
        )

    def test_classify_series_low_temp(self):
        """Test classification of low temperature series."""
        assert (
            self.discovery._classify_series("KXLOWNY")
            == WeatherMarketType.DAILY_LOW_TEMP
        )
        assert (
            self.discovery._classify_series("KXLOWTNYC")
            == WeatherMarketType.DAILY_LOW_TEMP
        )

    def test_classify_series_monthly_rain(self):
        """Test classification of monthly rain series."""
        assert (
            self.discovery._classify_series("KXRAINNYCM")
            == WeatherMarketType.MONTHLY_RAIN
        )
        assert (
            self.discovery._classify_series("KXRAINLAM")
            == WeatherMarketType.MONTHLY_RAIN
        )

    def test_classify_series_daily_rain(self):
        """Test classification of daily rain series."""
        assert (
            self.discovery._classify_series("KXRAINNYC")
            == WeatherMarketType.DAILY_RAIN
        )

    def test_classify_series_monthly_snow(self):
        """Test classification of monthly snow series."""
        assert (
            self.discovery._classify_series("KXBOSSNOWM")
            == WeatherMarketType.MONTHLY_SNOW
        )

    def test_classify_series_daily_snow(self):
        """Test classification of daily snow series."""
        assert (
            self.discovery._classify_series("KXSNOWNY")
            == WeatherMarketType.DAILY_SNOW
        )

    def test_classify_market(self):
        """Test classification of a full market ticker."""
        result = self.discovery.classify_market("KXHIGHNY-26FEB10-B36.5")
        assert result == WeatherMarketType.DAILY_HIGH_TEMP

        result = self.discovery.classify_market("KXRAINNYCM-26FEB-3")
        assert result == WeatherMarketType.MONTHLY_RAIN

        result = self.discovery.classify_market("KXBOSSNOWM-26FEB-12.0")
        assert result == WeatherMarketType.MONTHLY_SNOW

        # Non-weather market
        result = self.discovery.classify_market("MODELXYZ-26FEB10")
        assert result is None

    def test_is_weather_market(self):
        """Test is_weather_market convenience method."""
        assert self.discovery.is_weather_market("KXHIGHNY-26FEB10-B36.5")
        assert self.discovery.is_weather_market("KXRAINNYCM-26FEB-3")
        assert not self.discovery.is_weather_market("MODELXYZ-26FEB10")
        assert not self.discovery.is_weather_market("KXHMONTHRANGE-26FEB-25")

    @pytest.mark.asyncio
    async def test_discover_weather_series_success(self):
        """Test successful discovery of weather series."""
        # Create mock markets with series_ticker
        mock_market1 = Mock(spec=Market)
        mock_market1.series_ticker = "KXHIGHNY"

        mock_market2 = Mock(spec=Market)
        mock_market2.series_ticker = "KXLOWTNYC"

        mock_market3 = Mock(spec=Market)
        mock_market3.series_ticker = "KXHMODEL"  # Non-weather

        mock_market4 = Mock(spec=Market)
        mock_market4.series_ticker = "KXRAINNYCM"

        # Mock client
        mock_client = AsyncMock()
        mock_client.get_markets = AsyncMock(
            side_effect=[
                ([mock_market1, mock_market2, mock_market3, mock_market4], None),
            ]
        )

        # Discover series
        series = await self.discovery.discover_weather_series(
            mock_client, use_cache=False
        )

        # Should discover only weather series
        assert set(series) == {"KXHIGHNY", "KXLOWTNYC", "KXRAINNYCM"}
        assert "KXHMODEL" not in series

    @pytest.mark.asyncio
    async def test_discover_weather_series_cache(self):
        """Test that cached results are used when available."""
        mock_client = AsyncMock()

        # First call - should query API
        self.discovery._cached_series = {"KXHIGHNY": datetime.now(timezone.utc)}
        self.discovery._cache_timestamp = datetime.now(timezone.utc)

        # Use cache
        series = await self.discovery.discover_weather_series(
            mock_client, use_cache=True
        )

        # Should return cached series without calling API
        assert series == ["KXHIGHNY"]
        mock_client.get_markets.assert_not_called()

    @pytest.mark.asyncio
    async def test_discover_weather_series_pagination(self):
        """Test pagination through multiple pages of markets."""
        mock_market1 = Mock(spec=Market)
        mock_market1.series_ticker = "KXHIGHNY"

        mock_market2 = Mock(spec=Market)
        mock_market2.series_ticker = "KXLOWTNYC"

        mock_market3 = Mock(spec=Market)
        mock_market3.series_ticker = "KXRAINNYCM"

        # Mock client with pagination
        mock_client = AsyncMock()
        mock_client.get_markets = AsyncMock(
            side_effect=[
                ([mock_market1, mock_market2], "cursor123"),
                ([mock_market3], None),  # Second page, no more pages
            ]
        )

        series = await self.discovery.discover_weather_series(
            mock_client, use_cache=False
        )

        # Should discover all series from both pages
        assert set(series) == {"KXHIGHNY", "KXLOWTNYC", "KXRAINNYCM"}
        assert mock_client.get_markets.call_count == 2

    @pytest.mark.asyncio
    async def test_get_active_weather_markets(self):
        """Test getting all active weather markets."""
        # Mock series discovery
        series_market1 = Mock(spec=Market)
        series_market1.ticker = "KXHIGHNY-26FEB10-B36.5"
        series_market1.series_ticker = "KXHIGHNY"

        series_market2 = Mock(spec=Market)
        series_market2.ticker = "KXLOWTNYC-26FEB10-T21"
        series_market2.series_ticker = "KXLOWTNYC"

        rain_market = Mock(spec=Market)
        rain_market.ticker = "KXRAINNYCM-26FEB-3"
        rain_market.series_ticker = "KXRAINNYCM"

        mock_client = AsyncMock()
        mock_client.get_markets = AsyncMock(
            side_effect=[
                # Series discovery call
                ([series_market1, series_market2, rain_market], None),
                # Market fetch for KXHIGHNY
                ([series_market1], None),
                # Market fetch for KXLOWTNYC
                ([series_market2], None),
                # Market fetch for KXRAINNYCM
                ([rain_market], None),
            ]
        )

        markets = await self.discovery.get_active_weather_markets(mock_client)

        # Should get all markets
        assert len(markets) == 3
        tickers = {m.ticker for m in markets}
        assert tickers == {
            "KXHIGHNY-26FEB10-B36.5",
            "KXLOWTNYC-26FEB10-T21",
            "KXRAINNYCM-26FEB-3",
        }

    def test_clear_cache(self):
        """Test cache clearing."""
        # Add some cached data
        self.discovery._cached_series = {"KXHIGHNY": datetime.now(timezone.utc)}
        self.discovery._cache_timestamp = datetime.now(timezone.utc)

        # Clear cache
        self.discovery.clear_cache()

        # Should be empty
        assert len(self.discovery._cached_series) == 0
        assert self.discovery._cache_timestamp is None


class TestWeatherMarketDiscoverySingleton:
    """Test singleton pattern for discovery."""

    def test_get_weather_market_discovery_singleton(self):
        """Test that singleton returns same instance."""
        discovery1 = get_weather_market_discovery()
        discovery2 = get_weather_market_discovery()

        assert discovery1 is discovery2
