"""
National Weather Service (NWS) API Integration

Kalshi weather markets settle based on NWS Daily Climate Reports.
Using NWS forecasts gives us the actual settlement source data.

Settlement Locations (CRITICAL - these are exact Kalshi settlement stations):
- NYC: Central Park (KNYC) - 40.7829, -73.9654
- Chicago: Midway Airport (KMDW) - 41.7868, -87.7522
- Miami: Miami International Airport (KMIA) - 25.7959, -80.2870
- Austin: Austin-Bergstrom International Airport (KAUS) - 30.1944, -97.6700

API Documentation: https://www.weather.gov/documentation/services-web-api
"""

import json
import os
import requests
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from typing import Optional, Dict, List, Tuple
from zoneinfo import ZoneInfo
import time
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from src.utils.logging import logger


@dataclass
class StationObservation:
    """Live observation from an NWS settlement station (METAR-based).

    Used by the observation-settled scanner for guaranteed-outcome trades.
    The daily_high_f / daily_low_f are tracked as running extremes across
    all observations fetched today — NOT a single API field.
    """
    city: str
    station_id: str
    current_temp_f: float          # Latest METAR reading
    daily_high_f: float            # Running max of all readings today
    daily_low_f: float             # Running min of all readings today
    observation_time: datetime     # When the METAR was issued (UTC)
    local_hour: int                # Local hour (0-23)
    is_past_peak_high: bool        # After 4pm local (highs typically 2-4pm)
    is_past_sunrise: bool          # After 8am local (lows typically 5-7am)


@dataclass
class TemperatureForecast:
    """NWS temperature forecast for a specific location and date."""
    city: str
    station_id: str
    forecast_date: date
    high_f: int
    low_f: int
    high_confidence: str  # "high", "medium", "low"
    forecast_generated: datetime
    raw_periods: List[dict] = None  # Store raw forecast periods for debugging

    def __repr__(self):
        return (
            f"TemperatureForecast({self.city}, {self.forecast_date}: "
            f"High={self.high_f}°F, Low={self.low_f}°F, "
            f"Confidence={self.high_confidence})"
        )


# Kalshi settlement station coordinates
# IMPORTANT: These must match exactly what Kalshi uses for settlement
KALSHI_STATIONS = {
    "NYC": {
        "station_id": "KNYC",
        "name": "Central Park",
        "lat": 40.7829,
        "lon": -73.9654,
        "timezone": "America/New_York",
    },
    "CHICAGO": {
        "station_id": "KMDW",
        "name": "Midway Airport",
        "lat": 41.7868,
        "lon": -87.7522,
        "timezone": "America/Chicago",
    },
    "MIAMI": {
        "station_id": "KMIA",
        "name": "Miami International Airport",
        "lat": 25.7959,
        "lon": -80.2870,
        "timezone": "America/New_York",
    },
    "AUSTIN": {
        "station_id": "KAUS",
        "name": "Austin-Bergstrom International Airport",
        "lat": 30.1944,
        "lon": -97.6700,
        "timezone": "America/Chicago",
    },
    "DENVER": {
        "station_id": "KDEN",
        "name": "Denver International Airport",
        "lat": 39.8561,
        "lon": -104.6737,
        "timezone": "America/Denver",
    },
    "ATLANTA": {
        "station_id": "KATL",
        "name": "Hartsfield-Jackson Atlanta International Airport",
        "lat": 33.6407,
        "lon": -84.4277,
        "timezone": "America/New_York",
    },
    "PHILADELPHIA": {
        "station_id": "KPHL",
        "name": "Philadelphia International Airport",
        "lat": 39.8721,
        "lon": -75.2411,
        "timezone": "America/New_York",
    },
    "SEATTLE": {
        "station_id": "KSEA",
        "name": "Seattle-Tacoma International Airport",
        "lat": 47.4502,
        "lon": -122.3088,
        "timezone": "America/Los_Angeles",
    },
    "LOS_ANGELES": {
        "station_id": "KLAX",
        "name": "Los Angeles International Airport",
        "lat": 33.9425,
        "lon": -118.4081,
        "timezone": "America/Los_Angeles",
    },
    "BOSTON": {
        "station_id": "KBOS",
        "name": "Logan International Airport",
        "lat": 42.3656,
        "lon": -71.0096,
        "timezone": "America/New_York",
    },
    "DALLAS": {
        "station_id": "KDFW",
        "name": "Dallas/Fort Worth International Airport",
        "lat": 32.8998,
        "lon": -97.0403,
        "timezone": "America/Chicago",
    },
    "HOUSTON": {
        "station_id": "KIAH",
        "name": "George Bush Intercontinental Airport",
        "lat": 29.9844,
        "lon": -95.3414,
        "timezone": "America/Chicago",
    },
    "WASHINGTON_DC": {
        "station_id": "KDCA",
        "name": "Ronald Reagan Washington National Airport",
        "lat": 38.8512,
        "lon": -77.0402,
        "timezone": "America/New_York",
    },
    "DETROIT": {
        "station_id": "KDTW",
        "name": "Detroit Metropolitan Wayne County Airport",
        "lat": 42.2124,
        "lon": -83.3534,
        "timezone": "America/Detroit",
    },
    "SALT_LAKE_CITY": {
        "station_id": "KSLC",
        "name": "Salt Lake City International Airport",
        "lat": 40.7884,
        "lon": -111.9778,
        "timezone": "America/Denver",
    },
}


def get_active_cities() -> list:
    """Get the list of active trading cities.

    Single source of truth for city list. Reads from env var
    WEATHER_ENABLED_CITIES, falling back to all cities in KALSHI_STATIONS.
    """
    env_cities = os.getenv("WEATHER_ENABLED_CITIES")
    if env_cities:
        cities = [c.strip().upper() for c in env_cities.split(",") if c.strip()]
        valid = [c for c in cities if c in KALSHI_STATIONS]
        invalid = [c for c in cities if c not in KALSHI_STATIONS]
        if invalid:
            logger.warning(
                f"WEATHER_ENABLED_CITIES contains unknown cities: {invalid}. "
                f"Valid cities: {list(KALSHI_STATIONS.keys())}"
            )
        return valid
    return list(KALSHI_STATIONS.keys())


class NWSClient:
    """
    Client for National Weather Service API.

    The NWS API is free and requires no authentication.
    Rate limiting: Be respectful, add delays between requests.

    Usage:
        client = NWSClient()
        forecast = client.get_forecast("NYC", date.today() + timedelta(days=1))
        print(f"Tomorrow's high in NYC: {forecast.high_f}°F")
    """

    BASE_URL = "https://api.weather.gov"
    USER_AGENT = "KalshiTradingBot/1.0 (contact@example.com)"  # NWS requires User-Agent
    EXTREMES_FILE = Path(os.getenv("NWS_EXTREMES_FILE", "data/nws_daily_extremes.json"))

    def __init__(self, cache_ttl_minutes: int = 15):
        """
        Initialize NWS client.

        Args:
            cache_ttl_minutes: How long to cache forecasts (default 15 min).
                Reduced from 60 min — same-day markets need fresh forecasts
                as NWS updates intraday, especially near peak temperature hours.
        """
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self.USER_AGENT,
            "Accept": "application/geo+json",
        })
        self._forecast_cache: Dict[str, Tuple[datetime, TemperatureForecast]] = {}
        self._grid_cache: Dict[str, dict] = {}  # Cache grid lookups
        self.cache_ttl = timedelta(minutes=cache_ttl_minutes)
        self._daily_extremes = self._load_extremes()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception_type(requests.RequestException),
        reraise=True,
    )
    def _fetch_json(self, url: str, timeout: int = 10) -> dict:
        """Fetch JSON from NWS API with automatic retry on transient failures."""
        response = self.session.get(url, timeout=timeout)
        response.raise_for_status()
        return response.json()

    def _get_grid_info(self, city: str) -> Optional[dict]:
        """
        Get NWS grid information for a city.

        The NWS API requires you to first look up the grid coordinates
        for a lat/lon point, then use those to get forecasts.
        """
        if city.upper() not in KALSHI_STATIONS:
            logger.error(f"Unknown city: {city}. Valid cities: {list(KALSHI_STATIONS.keys())}")
            return None

        # Check cache
        if city.upper() in self._grid_cache:
            return self._grid_cache[city.upper()]

        station = KALSHI_STATIONS[city.upper()]
        url = f"{self.BASE_URL}/points/{station['lat']},{station['lon']}"

        try:
            data = self._fetch_json(url, timeout=10)

            grid_info = {
                "forecast_url": data["properties"]["forecast"],
                "forecast_hourly_url": data["properties"]["forecastHourly"],
                "grid_id": data["properties"]["gridId"],
                "grid_x": data["properties"]["gridX"],
                "grid_y": data["properties"]["gridY"],
                "timezone": data["properties"]["timeZone"],
            }

            # Cache the grid info (it doesn't change)
            self._grid_cache[city.upper()] = grid_info
            logger.debug(f"Got grid info for {city}: {grid_info['grid_id']}")

            return grid_info

        except requests.RequestException as e:
            logger.error(f"Error getting grid info for {city}: {e}")
            return None

    def get_forecast(self, city: str, target_date: date) -> Optional[TemperatureForecast]:
        """
        Get temperature forecast for a Kalshi settlement location.

        Args:
            city: One of "NYC", "CHICAGO", "MIAMI", "AUSTIN"
            target_date: Date to get forecast for (today through ~7 days out)

        Returns:
            TemperatureForecast or None if unavailable
        """
        city = city.upper()
        cache_key = f"{city}_{target_date.isoformat()}"

        # Check cache
        if cache_key in self._forecast_cache:
            cache_time, forecast = self._forecast_cache[cache_key]
            if datetime.now() - cache_time < self.cache_ttl:
                logger.debug(f"Using cached forecast for {cache_key}")
                return forecast

        # Get grid info
        grid_info = self._get_grid_info(city)
        if not grid_info:
            return None

        # Small delay to be nice to NWS servers
        time.sleep(0.5)

        try:
            # Get forecast
            data = self._fetch_json(grid_info["forecast_url"], timeout=15)

            periods = data["properties"]["periods"]

            # Find the periods for our target date
            # NWS returns periods like "Today", "Tonight", "Tuesday", "Tuesday Night", etc.
            high_temp = None
            low_temp = None
            matching_periods = []

            for period in periods:
                # Parse the period start time
                start_time = datetime.fromisoformat(
                    period["startTime"].replace("Z", "+00:00")
                )
                period_date = start_time.date()

                if period_date == target_date:
                    matching_periods.append(period)
                    temp = period["temperature"]

                    # Daytime period = high, nighttime = low
                    if period["isDaytime"]:
                        high_temp = temp
                    else:
                        low_temp = temp

            if high_temp is None and low_temp is None:
                logger.warning(f"No forecast found for {city} on {target_date}")
                return None

            # Determine confidence based on how far out the forecast is
            days_out = (target_date - date.today()).days
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
                high_f=high_temp or 0,
                low_f=low_temp or 0,
                high_confidence=confidence,
                forecast_generated=datetime.now(),
                raw_periods=matching_periods,
            )

            # Cache the forecast
            self._forecast_cache[cache_key] = (datetime.now(), forecast)

            logger.info(
                f"NWS Forecast for {city} on {target_date}: "
                f"High={high_temp}°F, Low={low_temp}°F"
            )

            return forecast

        except requests.RequestException as e:
            logger.error(f"Error getting forecast for {city}: {e}")
            return None
        except (KeyError, ValueError) as e:
            logger.error(f"Error parsing forecast data for {city}: {e}")
            return None

    def get_all_forecasts(self, target_date: date) -> Dict[str, TemperatureForecast]:
        """
        Get forecasts for all Kalshi settlement cities.

        Args:
            target_date: Date to get forecasts for

        Returns:
            Dict mapping city name to forecast
        """
        forecasts = {}
        for city in KALSHI_STATIONS:
            forecast = self.get_forecast(city, target_date)
            if forecast:
                forecasts[city] = forecast
            # Small delay between requests
            time.sleep(0.5)
        return forecasts

    # ── Live station observations (for observation-settled scanner) ──────

    def _load_extremes(self) -> Dict[str, dict]:
        """Load persisted daily extremes from disk."""
        if not self.EXTREMES_FILE.exists():
            return {}
        try:
            data = json.loads(self.EXTREMES_FILE.read_text())
            today = date.today().isoformat()
            return {k: v for k, v in data.items() if v.get("date") == today}
        except Exception:
            return {}

    def _save_extremes(self) -> None:
        """Persist current daily extremes to disk."""
        try:
            self.EXTREMES_FILE.parent.mkdir(parents=True, exist_ok=True)
            self.EXTREMES_FILE.write_text(
                json.dumps(self._daily_extremes, indent=2, default=str)
            )
        except Exception as e:
            logger.debug(f"Failed to save NWS extremes: {e}")

    def bootstrap_daily_extremes(self) -> None:
        """Reconstruct today's extremes from NWS observation history.

        Called once on startup if persisted extremes are empty/stale.
        Fetches the last 24h of observations and finds today's max/min.
        """
        today = date.today()
        for city, station_info in KALSHI_STATIONS.items():
            station_id = station_info.get("station_id")
            if not station_id:
                continue
            extreme_key = city.upper()
            if extreme_key in self._daily_extremes:
                existing = self._daily_extremes[extreme_key]
                if existing.get("date") == today.isoformat():
                    continue  # Already have today's data
            try:
                url = f"{self.BASE_URL}/stations/{station_id}/observations"
                response = self.session.get(url, timeout=15, params={"limit": 24})
                response.raise_for_status()
                features = response.json().get("features", [])

                tz_name = station_info.get("timezone", "America/New_York")
                tz = ZoneInfo(tz_name)
                today_local = today

                temps = []
                for feat in features:
                    props = feat.get("properties", {})
                    temp_c = props.get("temperature", {}).get("value")
                    ts_str = props.get("timestamp", "")
                    if temp_c is None:
                        continue
                    try:
                        obs_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        obs_local = obs_dt.astimezone(tz).date()
                        if obs_local == today_local:
                            temps.append(temp_c * 9.0 / 5.0 + 32.0)
                    except Exception:
                        continue

                if temps:
                    self._daily_extremes[extreme_key] = {
                        "date": today.isoformat(),
                        "high": round(max(temps), 1),
                        "low": round(min(temps), 1),
                    }
                    logger.info(
                        f"Bootstrap {city}: high={max(temps):.1f}F low={min(temps):.1f}F "
                        f"from {len(temps)} observations"
                    )
            except Exception as e:
                logger.debug(f"Bootstrap extremes failed for {city}: {e}")

        self._save_extremes()

    def get_station_observation(self, city: str) -> Optional[StationObservation]:
        """
        Fetch the latest METAR observation from the actual NWS settlement station.

        This is the ground-truth data source — the same stations Kalshi uses
        for settlement (KNYC, KMIA, KMDW, KAUS, etc.).

        Returns a StationObservation with the current reading AND a running
        daily high/low tracked across all calls today.

        Cache: 3 minutes (METAR updates every 5-20 min for airports).
        """
        city_upper = city.upper()
        if city_upper not in KALSHI_STATIONS:
            logger.error(f"Unknown city for observation: {city}")
            return None

        station = KALSHI_STATIONS[city_upper]
        station_id = station["station_id"]
        tz = ZoneInfo(station["timezone"])
        local_now = datetime.now(tz)
        today_local = local_now.date()

        # Check cache (3-minute TTL for observations)
        cache_key = f"obs_{station_id}"
        if cache_key in self._forecast_cache:
            cache_time, cached_obs = self._forecast_cache[cache_key]
            age = datetime.now() - cache_time
            if age < timedelta(minutes=3):
                return cached_obs

        try:
            url = f"{self.BASE_URL}/stations/{station_id}/observations/latest"
            response = self.session.get(url, timeout=10)
            response.raise_for_status()
            data = response.json()

            props = data.get("properties", {})

            # Temperature is in Celsius from NWS API
            temp_c = props.get("temperature", {}).get("value")
            if temp_c is None:
                logger.warning(f"No temperature in observation for {station_id}")
                return None

            current_temp_f = temp_c * 9.0 / 5.0 + 32.0

            # Parse observation time
            obs_time_str = props.get("timestamp", "")
            try:
                obs_time = datetime.fromisoformat(obs_time_str.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                obs_time = datetime.now(timezone.utc)

            # Update running daily extremes
            extreme_key = city_upper
            if (extreme_key not in self._daily_extremes
                    or self._daily_extremes[extreme_key]["date"] != today_local.isoformat()):
                # New day — reset
                self._daily_extremes[extreme_key] = {
                    "date": today_local.isoformat(),
                    "high": current_temp_f,
                    "low": current_temp_f,
                }
                self._save_extremes()
            else:
                extremes = self._daily_extremes[extreme_key]
                changed = False
                if current_temp_f > extremes["high"]:
                    extremes["high"] = current_temp_f
                    changed = True
                if current_temp_f < extremes["low"]:
                    extremes["low"] = current_temp_f
                    changed = True
                if changed:
                    self._save_extremes()

            daily_high = self._daily_extremes[extreme_key]["high"]
            daily_low = self._daily_extremes[extreme_key]["low"]

            local_hour = local_now.hour
            is_past_peak = local_hour >= 16  # After 4pm
            is_past_sunrise = local_hour >= 8  # After 8am

            obs = StationObservation(
                city=city_upper,
                station_id=station_id,
                current_temp_f=round(current_temp_f, 1),
                daily_high_f=round(daily_high, 1),
                daily_low_f=round(daily_low, 1),
                observation_time=obs_time,
                local_hour=local_hour,
                is_past_peak_high=is_past_peak,
                is_past_sunrise=is_past_sunrise,
            )

            # Cache it (3 min)
            self._forecast_cache[cache_key] = (datetime.now(), obs)

            logger.info(
                f"📡 NWS OBS {station_id} ({city_upper}): "
                f"{current_temp_f:.0f}°F now | "
                f"daily high={daily_high:.0f}°F low={daily_low:.0f}°F | "
                f"local {local_hour}:00 {'(past peak)' if is_past_peak else ''}"
            )

            return obs

        except requests.RequestException as e:
            logger.warning(f"NWS observation fetch failed for {station_id}: {e}")
            return None
        except (KeyError, ValueError, TypeError) as e:
            logger.warning(f"NWS observation parse failed for {station_id}: {e}")
            return None

    def get_all_observations(self, cities: List[str]) -> Dict[str, StationObservation]:
        """
        Fetch latest observations for multiple cities.

        Args:
            cities: List of city names (e.g., ["NYC", "MIAMI", "CHICAGO"])

        Returns:
            Dict mapping city name to StationObservation (skips failures)
        """
        observations = {}
        for city in cities:
            obs = self.get_station_observation(city)
            if obs:
                observations[city.upper()] = obs
            time.sleep(0.3)  # Respect NWS rate limits
        return observations

    def compare_to_kalshi_markets(
        self,
        city: str,
        target_date: date,
        kalshi_brackets: List[Tuple[int, int, float]],
    ) -> Dict:
        """
        Compare NWS forecast to Kalshi market brackets.

        Args:
            city: City name
            target_date: Forecast date
            kalshi_brackets: List of (low_temp, high_temp, market_price) tuples
                            e.g., [(40, 42, 0.35), (42, 44, 0.45), ...]

        Returns:
            Dict with forecast, recommended bracket, and edge analysis
        """
        forecast = self.get_forecast(city, target_date)
        if not forecast:
            return {"error": "Could not get forecast"}

        predicted_high = forecast.high_f

        # Find which bracket the NWS forecast falls into
        recommended_bracket = None
        for low, high, price in kalshi_brackets:
            if low <= predicted_high < high:
                recommended_bracket = (low, high, price)
                break

        # Handle edge cases (under/over brackets)
        if recommended_bracket is None:
            if kalshi_brackets and predicted_high < kalshi_brackets[0][0]:
                recommended_bracket = kalshi_brackets[0]
            elif kalshi_brackets and predicted_high >= kalshi_brackets[-1][1]:
                recommended_bracket = kalshi_brackets[-1]

        return {
            "forecast": {
                "city": city,
                "date": target_date.isoformat(),
                "predicted_high": predicted_high,
                "predicted_low": forecast.low_f,
                "confidence": forecast.high_confidence,
            },
            "recommended_bracket": recommended_bracket,
            "analysis": {
                "days_out": (target_date - date.today()).days,
                "forecast_age_minutes": 0,  # Just fetched
            },
        }


# Convenience function for quick access
def get_temperature_forecast(city: str, target_date: date) -> Optional[TemperatureForecast]:
    """
    Quick helper to get a temperature forecast.

    Args:
        city: "NYC", "CHICAGO", "MIAMI", or "AUSTIN"
        target_date: Date to forecast

    Returns:
        TemperatureForecast or None
    """
    client = NWSClient()
    return client.get_forecast(city, target_date)


# Example usage and testing
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print("=" * 60)
    print("NWS Weather API Test")
    print("=" * 60)

    client = NWSClient()

    # Test each city
    tomorrow = date.today() + timedelta(days=1)

    print(f"\nForecasts for {tomorrow}:")
    print("-" * 40)

    for city in KALSHI_STATIONS:
        forecast = client.get_forecast(city, tomorrow)
        if forecast:
            print(f"{city:10} | High: {forecast.high_f:3}°F | Low: {forecast.low_f:3}°F | Conf: {forecast.high_confidence}")
        else:
            print(f"{city:10} | FAILED to get forecast")

    print("\n" + "=" * 60)

    # Test comparison with mock Kalshi brackets
    print("\nExample: Comparing NYC forecast to Kalshi brackets")
    print("-" * 40)

    # Mock Kalshi brackets (temperature ranges and prices)
    mock_brackets = [
        (30, 32, 0.10),  # Under 32
        (32, 34, 0.15),
        (34, 36, 0.25),
        (36, 38, 0.30),
        (38, 40, 0.15),
        (40, 100, 0.05),  # Over 40
    ]

    analysis = client.compare_to_kalshi_markets("NYC", tomorrow, mock_brackets)
    print(f"Forecast: {analysis['forecast']['predicted_high']}°F")
    print(f"Recommended bracket: {analysis['recommended_bracket']}")
    print(f"Confidence: {analysis['forecast']['confidence']}")
