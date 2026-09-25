#!/usr/bin/env python3
"""
Test script for dynamic weather market discovery.

Demonstrates the new WeatherMarketDiscovery system that dynamically queries
the Kalshi API for available weather markets instead of using hardcoded lists.

Usage:
    python scripts/test_weather_discovery.py
"""

import asyncio
import sys
from typing import List

# Add project root to path
sys.path.insert(0, '/sessions/hopeful-festive-curie/mnt/kalshi-trading-system')

from src.data.weather_market_discovery import get_weather_market_discovery
from src.api.kalshi_client import KalshiClient
from src.utils.logging import logger


async def test_discovery():
    """Test the weather market discovery system."""
    print("=" * 70)
    print("Weather Market Discovery System Test")
    print("=" * 70)

    discovery = get_weather_market_discovery()

    # Test 1: Series pattern matching
    print("\n1. Testing weather series pattern matching:")
    print("-" * 70)

    test_series = [
        ("KXHIGHNY", True, "Daily high temp"),
        ("KXLOWTNYC", True, "Daily low temp (with T)"),
        ("KXLOWNY", True, "Daily low temp (without T)"),
        ("KXRAINNYC", True, "Daily rain"),
        ("KXRAINNYCM", True, "Monthly rain"),
        ("KXSNOWNY", True, "Daily snow"),
        ("KXNYCSNOWM", True, "Monthly snow"),
        ("KXMODEL", False, "Non-weather (model)"),
        ("KXTRUMP", False, "Non-weather (politics)"),
        ("KXPGA", False, "Non-weather (sports)"),
    ]

    for ticker, expected, description in test_series:
        is_weather = discovery._is_weather_series(ticker)
        status = "✓" if is_weather == expected else "✗"
        print(f"{status} {ticker:15} -> {is_weather:5} ({description})")

    # Test 2: Market classification
    print("\n2. Testing market ticker classification:")
    print("-" * 70)

    test_markets = [
        "KXHIGHNY-26FEB10-B36.5",
        "KXLOWTNYC-26FEB10-T14",
        "KXRAINNYC-26FEB10-T0",
        "KXRAINNYCM-26FEB-3",
        "KXNYCSNOWM-26FEB-8.0",
        "KXHMONTHRANGE-26FEB-25",
    ]

    for ticker in test_markets:
        market_type = discovery.classify_market(ticker)
        is_weather = discovery.is_weather_market(ticker)
        print(
            f"  {ticker:30} -> Type: {market_type.value if market_type else 'None':20} "
            f"Is Weather: {is_weather}"
        )

    # Test 3: Demonstrate fallback series list
    print("\n3. Fallback known weather series (used if API discovery fails):")
    print("-" * 70)

    fallback_series = discovery.KNOWN_WEATHER_SERIES
    print(f"Total known series: {len(fallback_series)}")

    # Group by type
    high_temp = [s for s in fallback_series if s.startswith("KXHIGH")]
    low_temp = [s for s in fallback_series if s.startswith("KXLOW")]
    monthly_rain = [s for s in fallback_series if s.endswith("M") and "RAIN" in s]
    monthly_snow = [s for s in fallback_series if "SNOWM" in s]

    print(f"  Daily High Temps: {len(high_temp)} series")
    print(f"    {', '.join(high_temp[:5])}...")
    print(f"  Daily Low Temps: {len(low_temp)} series")
    print(f"    {', '.join(low_temp[:3])}...")
    print(f"  Monthly Rain: {len(monthly_rain)} series")
    print(f"    {', '.join(monthly_rain[:5])}...")
    print(f"  Monthly Snow: {len(monthly_snow)} series")
    print(f"    {', '.join(monthly_snow[:5])}...")

    # Test 4: Cache behavior
    print("\n4. Testing discovery cache:")
    print("-" * 70)

    print(f"Cache TTL: {discovery.cache_ttl_seconds} seconds")

    # Clear cache
    discovery.clear_cache()
    print("Cache cleared")

    # Try to query API (will fail without credentials in demo, but shows the method)
    print(f"Attempting to discover weather series from Kalshi API...")
    try:
        async with KalshiClient(read_only=True) as client:
            series = await discovery.discover_weather_series(client, use_cache=False)
            print(f"Successfully discovered {len(series)} weather series from API")
            if series:
                print(f"  Sample discovered series: {series[:10]}")
    except Exception as e:
        print(f"API discovery failed (expected in demo): {type(e).__name__}")
        print(f"  {e}")
        print(f"System will fall back to known weather series list")

    print("\n" + "=" * 70)
    print("Test Complete")
    print("=" * 70)


def main():
    """Run tests."""
    asyncio.run(test_discovery())


if __name__ == "__main__":
    main()
