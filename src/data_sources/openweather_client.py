"""
OpenWeatherMap API Client

Alternative to NWS API when it's unavailable. Provides real weather forecasts
suitable for testing the weather trading strategy.

Free tier: 1,000 calls/day (plenty for testing)
Sign up: https://openweathermap.org/api
"""
import os
import requests
from datetime import datetime, date, timedelta, timezone
from typing import Optional, Dict, List
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from src.data_sources.nws_weather import TemperatureForecast, KALSHI_STATIONS
from src.utils.logging import logger


# OpenWeatherMap city coordinates (match KALSHI_STATIONS)
CITY_COORDS = {
    "NYC": (40.7829, -73.9654),
    "CHICAGO": (41.7868, -87.7522),
    "MIAMI": (25.7959, -80.2870),
    "AUSTIN": (30.1944, -97.6700),
    "DENVER": (39.8561, -104.6737),
    "ATLANTA": (33.6407, -84.4277),
    "PHILADELPHIA": (39.8721, -75.2411),
    "SEATTLE": (47.4502, -122.3088),
    "LOS_ANGELES": (33.9425, -118.4081),
    "BOSTON": (42.3656, -71.0096),
    "DALLAS": (32.8998, -97.0403),
    "HOUSTON": (29.9844, -95.3414),
    "WASHINGTON_DC": (38.8512, -77.0402),
    "DETROIT": (42.2124, -83.3534),
    "SALT_LAKE_CITY": (40.7884, -111.9778),
}

# City timezones for determining local time
CITY_TIMEZONES = {
    "NYC": "America/New_York",
    "CHICAGO": "America/Chicago",
    "MIAMI": "America/New_York",
    "AUSTIN": "America/Chicago",
    "DENVER": "America/Denver",
    "ATLANTA": "America/New_York",
    "PHILADELPHIA": "America/New_York",
    "SEATTLE": "America/Los_Angeles",
    "LOS_ANGELES": "America/Los_Angeles",
    "BOSTON": "America/New_York",
    "DALLAS": "America/Chicago",
    "HOUSTON": "America/Chicago",
    "WASHINGTON_DC": "America/New_York",
    "DETROIT": "America/Detroit",
    "SALT_LAKE_CITY": "America/Denver",
}


@dataclass
class CurrentObservation:
    """Current weather observation for a city."""
    city: str
    current_temp_f: float
    observed_high_f: float  # Highest temp observed today so far
    observed_low_f: float   # Lowest temp observed today so far
    observation_time: datetime
    local_hour: int         # Local hour (0-23) at observation
    is_past_peak_high: bool # True if likely past daily high (after 4pm local)
    is_past_sunrise: bool   # True if past sunrise (low already set)


class OpenWeatherClient:
    """
    OpenWeatherMap API client for real weather forecasts.

    Provides actual forecast data suitable for testing weather trading strategy.
    Free tier is sufficient for development/testing.
    """

    BASE_URL = "https://api.openweathermap.org/data/2.5/forecast"
    CURRENT_URL = "https://api.openweathermap.org/data/2.5/weather"

    def __init__(self, api_key: Optional[str] = None, cache_ttl_minutes: int = 60):
        """
        Initialize OpenWeatherMap client.

        Args:
            api_key: OpenWeatherMap API key (or set OPENWEATHER_API_KEY env var)
            cache_ttl_minutes: Cache duration for forecasts
        """
        self.api_key = api_key or os.getenv("OPENWEATHER_API_KEY")
        if not self.api_key:
            raise ValueError(
                "OpenWeatherMap API key required. "
                "Set OPENWEATHER_API_KEY env var or pass api_key parameter. "
                "Get free key at: https://openweathermap.org/api"
            )

        self._forecast_cache: Dict[str, tuple] = {}
        self.cache_ttl = timedelta(minutes=cache_ttl_minutes)

        logger.info("OpenWeatherMap client initialized (using real forecast data)")

    def get_forecast(
        self, city: str, target_date: date
    ) -> Optional[TemperatureForecast]:
        """
        Get temperature forecast for a specific city and date.

        OpenWeatherMap provides 5-day forecast in 3-hour intervals.
        We aggregate the intervals to get daily high/low.

        Args:
            city: City name (must be in CITY_COORDS)
            target_date: Date to forecast

        Returns:
            TemperatureForecast matching NWS format, or None if unavailable
        """
        city = city.upper()

        # Validate city
        if city not in CITY_COORDS:
            logger.error(f"City {city} not supported by OpenWeatherMap client")
            return None

        # Can't forecast past dates
        if target_date < date.today():
            logger.warning(f"Cannot forecast past date {target_date}")
            return None

        # Check if beyond 5-day forecast window
        days_out = (target_date - date.today()).days
        if days_out > 5:
            logger.warning(
                f"OpenWeatherMap only provides 5-day forecasts. "
                f"{target_date} is {days_out} days out."
            )
            return None

        # Check cache
        cache_key = f"{city}_{target_date.isoformat()}"
        if cache_key in self._forecast_cache:
            cache_time, forecast = self._forecast_cache[cache_key]
            if datetime.now() - cache_time < self.cache_ttl:
                logger.debug(f"Using cached OpenWeatherMap forecast for {cache_key}")
                return forecast

        try:
            # Get coordinates
            lat, lon = CITY_COORDS[city]

            # Call OpenWeatherMap API
            params = {
                "lat": lat,
                "lon": lon,
                "appid": self.api_key,
                "units": "imperial",  # Fahrenheit
            }

            response = requests.get(self.BASE_URL, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()

            # Parse forecast periods for target date
            high_temp = None
            low_temp = None
            total_periods = len(data.get("list", []))
            matched_periods = 0

            for period in data.get("list", []):
                # Parse timestamp
                period_time = datetime.fromtimestamp(period["dt"], tz=timezone.utc)
                period_date = period_time.date()

                if period_date == target_date:
                    temp = period["main"]["temp"]
                    matched_periods += 1

                    # Track high and low for the day
                    if high_temp is None or temp > high_temp:
                        high_temp = temp
                    if low_temp is None or temp < low_temp:
                        low_temp = temp

            # SAME-DAY FALLBACK: When the 5-day forecast API has no remaining
            # 3-hour intervals for today (late in the day, or API starts from
            # next period), fall back to the current weather observation which
            # has the actual observed high/low for today.
            if days_out == 0 and high_temp is None:
                logger.info(
                    f"No forecast periods for {city} today "
                    f"({matched_periods}/{total_periods} matched), "
                    f"falling back to current observation"
                )
                obs = self.get_current_observation(city)
                if obs:
                    high_temp = obs.observed_high_f
                    low_temp = obs.observed_low_f
                    logger.info(
                        f"Same-day observation fallback for {city}: "
                        f"High={high_temp:.0f}°F, Low={low_temp:.0f}°F "
                        f"(current={obs.current_temp_f:.1f}°F)"
                    )

            # Check if we still have no data after fallback
            if high_temp is None and low_temp is None:
                logger.warning(
                    f"No OpenWeatherMap data available for {city} on {target_date} "
                    f"({matched_periods}/{total_periods} forecast periods matched)"
                )
                return None

            # SAME-DAY FIX: When querying today's forecast late in the day,
            # the 3-hour intervals only cover remaining hours, causing
            # high==low (just the current temp). Supplement with current
            # observation data which tracks the actual observed high/low.
            if days_out == 0 and high_temp is not None and high_temp == low_temp:
                obs = self.get_current_observation(city)
                if obs:
                    # Use the broader of forecast vs observed range
                    if obs.observed_high_f and obs.observed_high_f > high_temp:
                        high_temp = obs.observed_high_f
                    if obs.observed_low_f and obs.observed_low_f < low_temp:
                        low_temp = obs.observed_low_f
                    logger.info(
                        f"Same-day fix for {city}: adjusted to "
                        f"High={high_temp:.0f}°F, Low={low_temp:.0f}°F "
                        f"(from current observation)"
                    )

            # Determine confidence based on forecast horizon
            if days_out == 0:
                confidence = "high"  # Same-day = highest confidence
            elif days_out <= 1:
                confidence = "high"
            elif days_out <= 3:
                confidence = "medium"
            else:
                confidence = "low"

            # Create forecast object matching NWS format
            forecast = TemperatureForecast(
                city=city,
                station_id=KALSHI_STATIONS[city]["station_id"],
                forecast_date=target_date,
                high_f=int(round(high_temp)) if high_temp else 0,
                low_f=int(round(low_temp)) if low_temp else 0,
                high_confidence=confidence,
                forecast_generated=datetime.now(timezone.utc),
                raw_periods=[],  # Could store OpenWeatherMap periods here
            )

            # Cache it
            self._forecast_cache[cache_key] = (datetime.now(), forecast)

            logger.info(
                f"OpenWeatherMap forecast for {city} on {target_date}: "
                f"High={forecast.high_f}°F, Low={forecast.low_f}°F "
                f"(confidence={confidence})"
            )

            return forecast

        except requests.RequestException as e:
            logger.error(f"Error fetching OpenWeatherMap data for {city}: {e}")
            return None
        except (KeyError, ValueError) as e:
            logger.error(f"Error parsing OpenWeatherMap response for {city}: {e}")
            return None

    def get_all_forecasts(self, target_date: date) -> Dict[str, TemperatureForecast]:
        """Get forecasts for all supported cities."""
        forecasts = {}
        for city in CITY_COORDS:
            forecast = self.get_forecast(city, target_date)
            if forecast:
                forecasts[city] = forecast
        return forecasts

    def get_current_observation(self, city: str) -> Optional[CurrentObservation]:
        """
        Get current weather observation for a city.

        This is crucial for same-day markets where the high/low may have
        already been reached. For example, if it's 5pm and the high was
        recorded at 2pm, we have much higher confidence in the outcome.

        Args:
            city: City name (must be in CITY_COORDS)

        Returns:
            CurrentObservation with current temp and observed high/low
        """
        city = city.upper()

        if city not in CITY_COORDS:
            logger.error(f"City {city} not supported")
            return None

        # Check cache (short TTL - 5 minutes for current weather)
        cache_key = f"current_{city}"
        if cache_key in self._forecast_cache:
            cache_time, obs = self._forecast_cache[cache_key]
            if datetime.now() - cache_time < timedelta(minutes=5):
                return obs

        try:
            lat, lon = CITY_COORDS[city]

            # Get current weather
            params = {
                "lat": lat,
                "lon": lon,
                "appid": self.api_key,
                "units": "imperial",
            }

            response = requests.get(self.CURRENT_URL, params=params, timeout=10)
            response.raise_for_status()
            data = response.json()

            current_temp = data["main"]["temp"]
            # OpenWeatherMap provides daily min/max in current weather endpoint
            temp_min = data["main"].get("temp_min", current_temp)
            temp_max = data["main"].get("temp_max", current_temp)

            # Get local time for this city
            tz = ZoneInfo(CITY_TIMEZONES.get(city, "America/New_York"))
            local_now = datetime.now(tz)
            local_hour = local_now.hour

            # Determine if we're past peak times
            # Highs typically occur between 2-4pm local time
            # Lows typically occur just before sunrise (5-7am)
            is_past_peak_high = local_hour >= 16  # After 4pm
            is_past_sunrise = local_hour >= 8     # After 8am (conservative)

            obs = CurrentObservation(
                city=city,
                current_temp_f=current_temp,
                observed_high_f=temp_max,
                observed_low_f=temp_min,
                observation_time=datetime.now(timezone.utc),
                local_hour=local_hour,
                is_past_peak_high=is_past_peak_high,
                is_past_sunrise=is_past_sunrise,
            )

            # Cache it
            self._forecast_cache[cache_key] = (datetime.now(), obs)

            logger.info(
                f"Current observation for {city}: {current_temp:.1f}°F "
                f"(high={temp_max:.1f}°F, low={temp_min:.1f}°F, "
                f"local_hour={local_hour}, past_peak={is_past_peak_high})"
            )

            return obs

        except requests.RequestException as e:
            logger.error(f"Error fetching current weather for {city}: {e}")
            return None
        except (KeyError, ValueError) as e:
            logger.error(f"Error parsing current weather for {city}: {e}")
            return None

    def check_same_day_settlement(
        self,
        city: str,
        threshold: int,
        threshold_type: str
    ) -> Optional[tuple[float, str]]:
        """
        Check if a same-day market has effectively already settled.

        For HIGH markets: If past peak hours and observed high > threshold,
        probability of YES is ~99%.

        For LOW markets: If past sunrise and observed low < threshold,
        probability of YES is ~99%.

        Args:
            city: City name
            threshold: Temperature threshold (e.g., 36 for "above 36")
            threshold_type: "above" (high) or "below" (low)

        Returns:
            Tuple of (probability, reasoning) if we can determine outcome,
            None if we cannot make a determination
        """
        obs = self.get_current_observation(city)
        if not obs:
            return None

        if threshold_type == "above":
            # HIGH temperature market
            if obs.is_past_peak_high:
                if obs.observed_high_f > threshold:
                    prob = 0.98
                    reason = (
                        f"SAME-DAY SETTLED: Past peak hours ({obs.local_hour}:00 local), "
                        f"observed high {obs.observed_high_f:.0f}°F > threshold {threshold}°F. "
                        f"High already exceeded - YES is near-certain."
                    )
                    return (prob, reason)
                elif obs.observed_high_f < threshold - 3:
                    # High was well below threshold and cooling now
                    prob = 0.05
                    reason = (
                        f"SAME-DAY SETTLED: Past peak hours ({obs.local_hour}:00 local), "
                        f"observed high {obs.observed_high_f:.0f}°F < threshold {threshold}°F. "
                        f"Unlikely to reach threshold now - NO is likely."
                    )
                    return (prob, reason)
        else:
            # LOW temperature market
            if obs.is_past_sunrise:
                if obs.observed_low_f < threshold:
                    prob = 0.98
                    reason = (
                        f"SAME-DAY SETTLED: Past sunrise ({obs.local_hour}:00 local), "
                        f"observed low {obs.observed_low_f:.0f}°F < threshold {threshold}°F. "
                        f"Low already below threshold - YES is near-certain."
                    )
                    return (prob, reason)

        # Cannot make a confident determination
        return None


# Example usage
if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)

    # Check for API key
    api_key = os.getenv("OPENWEATHER_API_KEY")
    if not api_key:
        print("ERROR: Set OPENWEATHER_API_KEY environment variable")
        print("Get free key at: https://openweathermap.org/api")
        exit(1)

    client = OpenWeatherClient(api_key)

    # Test forecast
    tomorrow = date.today() + timedelta(days=1)
    print(f"\nOpenWeatherMap Forecasts for {tomorrow}:")
    print("-" * 60)

    for city in ["NYC", "CHICAGO", "MIAMI", "AUSTIN"]:
        forecast = client.get_forecast(city, tomorrow)
        if forecast:
            print(
                f"{city:10} | High: {forecast.high_f:3}°F | "
                f"Low: {forecast.low_f:3}°F | Conf: {forecast.high_confidence}"
            )
        else:
            print(f"{city:10} | FAILED")

    print("\n✅ Real weather forecasts from OpenWeatherMap!")
