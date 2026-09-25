"""
Dynamic weather market discovery from Kalshi API.

Discovers available weather markets by querying the Kalshi API for weather series
rather than using hardcoded lists. This prevents phantom markets and catches new
markets as Kalshi adds them.

Weather Market Patterns:
- Daily High Temp: KXHIGH{CITY}-{YYMONDD}-{B/T}{TEMP}  (e.g., KXHIGHNY-26FEB10-B36.5)
- Daily Low Temp:  KXLOW{CITY}-{YYMONDD}-{B/T}{TEMP}   (e.g., KXLOWTNYC-26FEB10-T14)
- Daily Rain:      KXRAIN{CITY}-{YYMONDD}-{T}{THRESHOLD} (e.g., KXRAINNYC-26FEB10-T0)
- Daily Snow:      KXSNOW{CITY}-{YYMONDD}-{THRESHOLD}   (hypothetical)
- Monthly Rain:    KXRAIN{CITY}M-{YYMON}-{THRESHOLD}   (e.g., KXRAINNYCM-26FEB-2)
- Monthly Snow:    KX{CITY}SNOWM-{YYMON}-{THRESHOLD}   (e.g., KXCHISNOWM-26FEB-8.0)
"""

import re
from enum import Enum
from typing import Dict, List, Optional, Set
from datetime import datetime, timedelta, timezone

from src.api.kalshi_client import KalshiClient
from src.api.models import Market
from src.utils.logging import logger


class WeatherMarketType(Enum):
    """Types of weather markets supported."""
    DAILY_HIGH_TEMP = "daily_high_temp"
    DAILY_LOW_TEMP = "daily_low_temp"
    DAILY_RAIN = "daily_rain"
    DAILY_SNOW = "daily_snow"
    MONTHLY_RAIN = "monthly_rain"
    MONTHLY_SNOW = "monthly_snow"
    UNKNOWN = "unknown"


class WeatherMarketDiscovery:
    """
    Dynamically discover available weather markets from Kalshi API.

    Instead of hardcoding weather series, queries the API to find actual
    available series. This prevents phantom markets and adapts to new
    markets as Kalshi adds them.
    """

    # Regex patterns to identify weather series
    WEATHER_SERIES_PATTERNS = [
        # Daily high/low temperature series
        r'^KXHIGH[A-Z]{2,10}$',      # KXHIGHNY, KXHIGHCHI, KXHIGHMIA, KXHIGHDENVER, etc.
        r'^KXLOW[A-Z]{2,10}$',       # KXLOWNY, KXLOWCHI (without T prefix)
        r'^KXLOWT[A-Z]{2,10}$',      # KXLOWTNYC, KXLOWTCHI (with T prefix)
        # Monthly rain series (has M suffix) - must come before daily rain
        r'^KXRAIN[A-Z]{2,10}M$',     # KXRAINNYCM, KXRAINLAM
        # Daily rain series (no M suffix)
        r'^KXRAIN[A-Z]{2,10}$',      # KXRAINNYC (daily) - but won't match above monthly pattern
        # Monthly snow series (has M suffix, may be KX{CITY}SNOWM format)
        r'^KX[A-Z]{2,10}SNOWM$',     # KXBOSSNOWM, KXNYCSNOWM, KXCHISNOWM
        # Daily snow series (no M suffix)
        r'^KXSNOW[A-Z]{2,10}$',      # KXSNOWNY (daily)
    ]

    # Known weather series prefixes (fallback if API query fails)
    # Used for initial bootstrap only
    KNOWN_WEATHER_SERIES = [
        # High temps (daily)
        "KXHIGHNY", "KXHIGHCHI", "KXHIGHLA", "KXHIGHMIA", "KXHIGHAUS",
        "KXHIGHPHIL", "KXHIGHDEN", "KXHIGHSF", "KXHIGHATL", "KXHIGHPHX",
        "KXHIGHLV", "KXHIGHSEA", "KXHIGHBOS", "KXHIGHNOLA", "KXHIGHMIN",
        # Low temps (daily)
        "KXLOWNY", "KXLOWTNYC", "KXLOWAUS", "KXLOWCHI", "KXLOWMIA",
        "KXLOWDEN", "KXLOWPHIL", "KXLOWLA",
        "KXLOWTCHI", "KXLOWTMIA",
        # Snow (monthly)
        "KXBOSSNOWM", "KXNYCSNOWM", "KXCHISNOWM", "KXDENSNOWM",
        "KXSEASNOWM", "KXDETSNOWM", "KXPHILSNOWM", "KXDCSNOWM",
        "KXLASNOWM", "KXSFSNOWM", "KXMIASNOWM", "KXDALSNOWM",
        "KXAUSSNOWM", "KXHOUSNOWM", "KXSLCSNOWM",
        # Rain (monthly)
        "KXRAINNYCM", "KXRAINLAM", "KXRAINSFM", "KXRAINSEANM",
        "KXRAINMIAM", "KXRAINAUSM", "KXRAINDENM", "KXRAINDALM",
        "KXRAINHOUM", "KXRAINCHIM",
    ]

    def __init__(self, cache_ttl_seconds: int = 14400, skip_full_discovery: bool = True):
        """
        Initialize the discovery system.

        Args:
            cache_ttl_seconds: How long to cache discovered series (default: 4 hours).
            skip_full_discovery: If True, use KNOWN_WEATHER_SERIES directly instead of
                                scanning all markets (saves 10K+ API calls per discovery).
        """
        self.cache_ttl_seconds = cache_ttl_seconds
        self.skip_full_discovery = skip_full_discovery
        self._cached_series: Dict[str, datetime] = {}
        self._cache_timestamp: Optional[datetime] = None

    def _is_weather_series(self, ticker: str) -> bool:
        """
        Check if a ticker matches weather series patterns.

        Args:
            ticker: The series ticker to check.

        Returns:
            True if ticker matches any weather pattern, False otherwise.
        """
        ticker_upper = ticker.upper()

        # Check high/low temperature series (no M suffix variations)
        if re.match(r'^KX(HIGH|LOW|LOWT)[A-Z]{2,10}$', ticker_upper):
            return True

        # Check monthly rain series (M suffix)
        if re.match(r'^KXRAIN[A-Z]{2,10}M$', ticker_upper):
            return True

        # Check daily rain series (no M suffix)
        if re.match(r'^KXRAIN[A-Z]{2,10}$', ticker_upper) and not ticker_upper.endswith('M'):
            return True

        # Check monthly snow series (SNOWM pattern)
        if re.match(r'^KX[A-Z]{2,10}SNOWM$', ticker_upper):
            return True

        # Check daily snow series (SNOW without M suffix)
        if re.match(r'^KXSNOW[A-Z]{2,10}$', ticker_upper) and not ticker_upper.endswith('M'):
            return True

        return False

    def _classify_series(self, ticker: str) -> WeatherMarketType:
        """
        Classify a weather series into its type.

        Args:
            ticker: The series ticker.

        Returns:
            WeatherMarketType enum value.
        """
        ticker_upper = ticker.upper()

        # Check patterns in order of specificity
        if re.match(r'^KXHIGH[A-Z]{2,5}$', ticker_upper):
            return WeatherMarketType.DAILY_HIGH_TEMP
        elif re.match(r'^KXLOWT?[A-Z]{2,5}$', ticker_upper):
            return WeatherMarketType.DAILY_LOW_TEMP
        elif re.match(r'^KXRAIN[A-Z]{2,5}M$', ticker_upper):
            return WeatherMarketType.MONTHLY_RAIN
        elif re.match(r'^KXRAIN[A-Z]{2,5}(?!M)$', ticker_upper):
            return WeatherMarketType.DAILY_RAIN
        elif re.match(r'^KX[A-Z]{2,5}SNOWM$', ticker_upper):
            return WeatherMarketType.MONTHLY_SNOW
        elif re.match(r'^KXSNOW[A-Z]{2,5}(?!M)$', ticker_upper):
            return WeatherMarketType.DAILY_SNOW
        else:
            return WeatherMarketType.UNKNOWN

    async def discover_weather_series(
        self, client: KalshiClient, use_cache: bool = True
    ) -> List[str]:
        """
        Discover available weather series by querying the entire market universe.

        Fetches all available markets and filters for those matching weather patterns.
        Results are cached to avoid repeated expensive API calls.

        Args:
            client: Kalshi API client.
            use_cache: Whether to use cached results if available.

        Returns:
            List of discovered weather series tickers (e.g., ["KXHIGHNY", "KXLOWTNYC"]).
        """
        # Check cache validity
        if use_cache and self._cache_timestamp:
            cache_age = (datetime.now(timezone.utc) - self._cache_timestamp).total_seconds()
            if cache_age < self.cache_ttl_seconds and self._cached_series:
                logger.debug(
                    f"Using cached weather series ({len(self._cached_series)} series, "
                    f"cache age: {cache_age:.0f}s)"
                )
                return list(self._cached_series.keys())

        # Skip expensive full discovery and use known series list
        # This saves 10K+ API calls per discovery cycle
        if self.skip_full_discovery:
            logger.info(
                f"Using known weather series list ({len(self.KNOWN_WEATHER_SERIES)} series) "
                "- skipping full market scan"
            )
            discovered_series = set(self.KNOWN_WEATHER_SERIES)
            self._cached_series = {s: datetime.now(timezone.utc) for s in discovered_series}
            self._cache_timestamp = datetime.now(timezone.utc)
            return list(discovered_series)

        logger.info("Discovering weather series from API (full scan)...")
        discovered_series: Set[str] = set()
        cursor: Optional[str] = None
        pages_fetched = 0
        markets_scanned = 0

        try:
            # Fetch markets with pagination, looking for weather series
            while True:
                try:
                    markets, cursor = await client.get_markets(
                        status="open", limit=1000, cursor=cursor
                    )
                except Exception as e:
                    logger.warning(f"Error fetching markets page {pages_fetched}: {e}")
                    if not discovered_series:
                        # Fall back to known series if no discoveries yet
                        logger.warning("No weather series discovered, falling back to known list")
                        break
                    else:
                        # Partial results are OK, stop here
                        break

                if not markets:
                    break

                markets_scanned += len(markets)

                # Extract and classify series from markets
                for market in markets:
                    if hasattr(market, 'series_ticker') and market.series_ticker:
                        series_ticker = str(market.series_ticker).upper()
                        if self._is_weather_series(series_ticker):
                            discovered_series.add(series_ticker)

                pages_fetched += 1
                logger.debug(f"Fetched page {pages_fetched}, discovered {len(discovered_series)} series so far")

                if not cursor:
                    break

                # Rate limiting between pages
                import asyncio
                await asyncio.sleep(0.3)

        except Exception as e:
            logger.error(f"Weather series discovery failed: {e}")
            # Fall back to known series
            if not discovered_series:
                logger.warning("Using fallback known weather series list")
                discovered_series = set(self.KNOWN_WEATHER_SERIES)

        # Update cache
        self._cached_series = {s: datetime.now(timezone.utc) for s in discovered_series}
        self._cache_timestamp = datetime.now(timezone.utc)

        logger.info(
            f"Weather series discovery complete: {len(discovered_series)} series found",
            series_count=len(discovered_series),
            pages=pages_fetched,
            markets_scanned=markets_scanned,
        )

        return sorted(list(discovered_series))

    async def get_active_weather_markets(
        self, client: KalshiClient
    ) -> List[Market]:
        """
        Get all currently open weather markets.

        Discovers available weather series, then fetches all active markets
        from those series.

        Args:
            client: Kalshi API client.

        Returns:
            List of Market objects for active weather markets.
        """
        logger.info("Fetching active weather markets...")

        # First, discover available weather series
        weather_series = await self.discover_weather_series(client)

        if not weather_series:
            logger.warning("No weather series discovered")
            return []

        all_markets: List[Market] = []
        series_with_markets = 0

        # Fetch markets for each discovered series
        for series in weather_series:
            try:
                cursor: Optional[str] = None
                series_market_count = 0

                while True:
                    try:
                        # Only fetch OPEN markets (not closed/settled historical ones)
                        markets, cursor = await client.get_markets(
                            series_ticker=series, limit=200, cursor=cursor,
                            status="open",  # Filter to active markets only
                        )
                    except Exception as e:
                        logger.debug(f"Error fetching markets for series {series}: {e}")
                        break

                    if not markets:
                        break

                    all_markets.extend(markets)
                    series_market_count += len(markets)

                    if not cursor:
                        break

                    import asyncio
                    await asyncio.sleep(0.1)  # Rate limit between pages

                if series_market_count > 0:
                    series_with_markets += 1
                    logger.debug(f"Series {series}: {series_market_count} markets")

                # Rate limit between series
                import asyncio
                await asyncio.sleep(0.05)

            except Exception as e:
                logger.warning(f"Failed to fetch markets for series {series}: {e}")
                continue

        logger.info(
            f"Active weather markets fetched: {len(all_markets)} markets from "
            f"{series_with_markets}/{len(weather_series)} series"
        )

        return all_markets

    def classify_market(self, ticker: str) -> Optional[WeatherMarketType]:
        """
        Classify a market ticker into its weather type.

        Args:
            ticker: The market ticker (e.g., "KXHIGHNY-26FEB10-B36.5").

        Returns:
            WeatherMarketType if it's a weather market, None otherwise.
        """
        if not ticker:
            return None

        # Extract series part (before the first dash)
        parts = ticker.upper().split('-')
        if not parts:
            return None

        series_ticker = parts[0]

        if not self._is_weather_series(series_ticker):
            return None

        market_type = self._classify_series(series_ticker)
        return market_type if market_type != WeatherMarketType.UNKNOWN else None

    def is_weather_market(self, ticker: str) -> bool:
        """
        Check if a ticker is a weather market.

        Args:
            ticker: The market ticker.

        Returns:
            True if ticker is a weather market, False otherwise.
        """
        return self.classify_market(ticker) is not None

    def clear_cache(self) -> None:
        """Clear the cached weather series list."""
        self._cached_series.clear()
        self._cache_timestamp = None
        logger.debug("Weather series cache cleared")


# Singleton instance
_discovery_instance: Optional[WeatherMarketDiscovery] = None


def get_weather_market_discovery() -> WeatherMarketDiscovery:
    """Get or create the weather market discovery singleton."""
    global _discovery_instance
    if _discovery_instance is None:
        _discovery_instance = WeatherMarketDiscovery()
    return _discovery_instance
