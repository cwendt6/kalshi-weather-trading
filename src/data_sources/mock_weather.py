"""
Mock Weather Client for Testing

Use this when NWS API is unavailable due to network restrictions.
Provides realistic temperature forecasts based on seasonal norms.
"""
from datetime import date, datetime, timedelta, timezone
from typing import Optional, Dict
import random

from src.data_sources.nws_weather import TemperatureForecast, KALSHI_STATIONS
from src.utils.logging import logger


# Seasonal temperature norms (roughly realistic for each city)
# Format: {city: (winter_high, winter_low, summer_high, summer_low)}
SEASONAL_NORMS = {
    "NYC": {
        "winter": (40, 28),  # Dec-Feb
        "spring": (60, 45),  # Mar-May
        "summer": (82, 68),  # Jun-Aug
        "fall": (62, 48),    # Sep-Nov
    },
    "CHICAGO": {
        "winter": (32, 18),
        "spring": (58, 40),
        "summer": (82, 65),
        "fall": (58, 42),
    },
    "MIAMI": {
        "winter": (76, 62),
        "spring": (82, 70),
        "summer": (89, 77),
        "fall": (83, 72),
    },
    "AUSTIN": {
        "winter": (62, 42),
        "spring": (80, 60),
        "summer": (96, 75),
        "fall": (80, 60),
    },
    "DENVER": {
        "winter": (45, 18),
        "spring": (62, 35),
        "summer": (88, 58),
        "fall": (65, 38),
    },
    "ATLANTA": {
        "winter": (52, 35),
        "spring": (72, 52),
        "summer": (88, 70),
        "fall": (72, 52),
    },
    "PHILADELPHIA": {
        "winter": (42, 28),
        "spring": (62, 45),
        "summer": (85, 68),
        "fall": (65, 48),
    },
    "SEATTLE": {
        "winter": (48, 38),
        "spring": (60, 45),
        "summer": (75, 58),
        "fall": (60, 48),
    },
    "LOS_ANGELES": {
        "winter": (68, 50),
        "spring": (72, 55),
        "summer": (82, 65),
        "fall": (75, 58),
    },
}


def get_season(forecast_date: date) -> str:
    """Determine season from date."""
    month = forecast_date.month
    if month in [12, 1, 2]:
        return "winter"
    elif month in [3, 4, 5]:
        return "spring"
    elif month in [6, 7, 8]:
        return "summer"
    else:
        return "fall"


class MockNWSClient:
    """
    Mock weather client that generates realistic temperature forecasts.

    Uses seasonal norms with random variation to simulate real forecasts.
    Useful for testing when NWS API is unavailable.

    IMPORTANT: Only use for testing/development, not production trading!
    Real money should only be traded with actual NWS data.
    """

    def __init__(self, cache_ttl_minutes: int = 60):
        """Initialize mock client."""
        self._forecast_cache: Dict[str, TemperatureForecast] = {}
        logger.warning(
            "Using MOCK weather client - forecasts are synthetic. "
            "DO NOT use for real money trading!"
        )

    def get_forecast(
        self, city: str, target_date: date
    ) -> Optional[TemperatureForecast]:
        """
        Generate a mock temperature forecast.

        Args:
            city: City name (must be in SEASONAL_NORMS)
            target_date: Date to forecast

        Returns:
            TemperatureForecast with realistic but synthetic data
        """
        city = city.upper()

        # Check if city is supported
        if city not in SEASONAL_NORMS:
            logger.error(f"Mock weather: City {city} not supported")
            return None

        # Can't forecast past dates
        if target_date < date.today():
            logger.warning(f"Mock weather: Cannot forecast past date {target_date}")
            return None

        # Check cache
        cache_key = f"{city}_{target_date.isoformat()}"
        if cache_key in self._forecast_cache:
            return self._forecast_cache[cache_key]

        # Get seasonal norms
        season = get_season(target_date)
        base_high, base_low = SEASONAL_NORMS[city][season]

        # Add random variation (±5-10°F) to simulate day-to-day changes
        # More variation for further out forecasts
        days_out = (target_date - date.today()).days
        variation_range = 5 + (days_out * 1.5)  # Increase uncertainty with distance

        high_f = int(base_high + random.uniform(-variation_range, variation_range))
        low_f = int(base_low + random.uniform(-variation_range, variation_range))

        # Ensure high > low
        if high_f <= low_f:
            high_f = low_f + 10

        # Determine confidence based on forecast horizon
        if days_out <= 1:
            confidence = "high"
        elif days_out <= 3:
            confidence = "medium"
        else:
            confidence = "low"

        forecast = TemperatureForecast(
            city=city,
            station_id=KALSHI_STATIONS[city]["station_id"],
            forecast_date=target_date,
            high_f=high_f,
            low_f=low_f,
            high_confidence=confidence,
            forecast_generated=datetime.now(timezone.utc),
            raw_periods=[],
        )

        # Cache it
        self._forecast_cache[cache_key] = forecast

        logger.info(
            f"Mock forecast for {city} on {target_date}: "
            f"High={high_f}°F, Low={low_f}°F (confidence={confidence})"
        )

        return forecast

    def get_all_forecasts(self, target_date: date) -> Dict[str, TemperatureForecast]:
        """Get mock forecasts for all supported cities."""
        forecasts = {}
        for city in SEASONAL_NORMS:
            forecast = self.get_forecast(city, target_date)
            if forecast:
                forecasts[city] = forecast
        return forecasts


# For drop-in replacement
if __name__ == "__main__":
    import logging

    logging.basicConfig(level=logging.INFO)

    client = MockNWSClient()

    # Test forecasts
    tomorrow = date.today() + timedelta(days=1)
    print(f"\nMock Forecasts for {tomorrow}:")
    print("-" * 60)

    for city in ["NYC", "CHICAGO", "MIAMI", "AUSTIN"]:
        forecast = client.get_forecast(city, tomorrow)
        if forecast:
            print(
                f"{city:10} | High: {forecast.high_f:3}°F | "
                f"Low: {forecast.low_f:3}°F | Conf: {forecast.high_confidence}"
            )

    print("\n⚠️  WARNING: These are MOCK forecasts for testing only!")
    print("   Do not use for real money trading. Use actual NWS data.")
