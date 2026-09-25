"""
OpenWeatherMap API client for weather forecasts.

Covers 12 major US cities with current conditions and 5-day forecasts.
Free tier: 1,000 calls/day.
"""
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, date, timezone
from typing import Any, Dict, List, Optional

import requests

from src.utils.logging import logger


OPENWEATHER_BASE = "https://api.openweathermap.org/data/2.5"

# City coordinates (lat, lon) for direct API calls (no geocoding needed)
CITIES: Dict[str, Dict[str, Any]] = {
    "NYC": {"lat": 40.7128, "lon": -74.0060, "name": "New York"},
    "CHI": {"lat": 41.8781, "lon": -87.6298, "name": "Chicago"},
    "MIA": {"lat": 25.7617, "lon": -80.1918, "name": "Miami"},
    "AUS": {"lat": 30.2672, "lon": -97.7431, "name": "Austin"},
    "PHIL": {"lat": 39.9526, "lon": -75.1652, "name": "Philadelphia"},
    "DEN": {"lat": 39.7392, "lon": -104.9903, "name": "Denver"},
    "LA": {"lat": 34.0522, "lon": -118.2437, "name": "Los Angeles"},
    "SF": {"lat": 37.7749, "lon": -122.4194, "name": "San Francisco"},
    "SEA": {"lat": 47.6062, "lon": -122.3321, "name": "Seattle"},
    "DAL": {"lat": 32.7767, "lon": -96.7970, "name": "Dallas"},
    "ATL": {"lat": 33.7490, "lon": -84.3880, "name": "Atlanta"},
    "BOS": {"lat": 42.3601, "lon": -71.0589, "name": "Boston"},
}

DEFAULT_POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_OPENWEATHER", "1800"))
DAILY_QUOTA = 1000
QUOTA_WARN_THRESHOLD = 800


class OpenWeatherClient:
    """Client for OpenWeatherMap API (free tier)."""

    def __init__(self, api_key: Optional[str] = None, timeout: int = 15) -> None:
        self.api_key = api_key or os.getenv("OPENWEATHER_API_KEY", "")
        self.timeout = timeout
        self._cache: Dict[str, Any] = {}
        self._cache_expiry: Dict[str, float] = {}
        self._poll_interval = DEFAULT_POLL_INTERVAL
        self._daily_calls = 0
        self._daily_reset = time.time()

        if not self.api_key:
            logger.warning("OPENWEATHER_API_KEY not set — weather data unavailable")

    def _is_cache_valid(self, key: str) -> bool:
        return key in self._cache_expiry and time.time() < self._cache_expiry[key]

    def _set_cache(self, key: str, value: Any, ttl: Optional[int] = None) -> None:
        self._cache[key] = value
        self._cache_expiry[key] = time.time() + (ttl or self._poll_interval)

    def _track_call(self) -> None:
        now = time.time()
        if now - self._daily_reset > 86400:
            self._daily_calls = 0
            self._daily_reset = now
        self._daily_calls += 1
        if self._daily_calls >= QUOTA_WARN_THRESHOLD:
            logger.warning(f"OpenWeather daily quota: {self._daily_calls}/{DAILY_QUOTA}")

    def get_weather_forecast(self, city_code: str) -> Dict[str, Any]:
        """
        Fetch current weather + 5-day forecast for a city.

        Args:
            city_code: City code key (e.g. "NYC", "CHI").

        Returns:
            Dict with city, current conditions, 5-day forecast, status.
        """
        cache_key = f"owm_{city_code}"
        if self._is_cache_valid(cache_key):
            return self._cache[cache_key]

        city_info = CITIES.get(city_code)
        if not city_info:
            return {"city": city_code, "status": "unknown_city"}

        if not self.api_key:
            return {"city": city_info["name"], "status": "no_api_key"}

        result: Dict[str, Any] = {
            "city": city_info["name"],
            "city_code": city_code,
            "status": "success",
        }

        # Current weather
        try:
            self._track_call()
            resp = requests.get(
                f"{OPENWEATHER_BASE}/weather",
                params={
                    "lat": city_info["lat"],
                    "lon": city_info["lon"],
                    "appid": self.api_key,
                    "units": "imperial",
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
            result["current"] = {
                "temp": data["main"]["temp"],
                "feels_like": data["main"]["feels_like"],
                "humidity": data["main"]["humidity"],
                "description": data["weather"][0]["description"] if data.get("weather") else "",
                "wind_speed": data.get("wind", {}).get("speed", 0),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        except requests.RequestException as e:
            logger.debug(f"OpenWeather current failed for {city_code}: {e}")
            result["current"] = None
            result["status"] = "partial"

        # 5-day forecast
        try:
            self._track_call()
            resp = requests.get(
                f"{OPENWEATHER_BASE}/forecast",
                params={
                    "lat": city_info["lat"],
                    "lon": city_info["lon"],
                    "appid": self.api_key,
                    "units": "imperial",
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()

            # Group by date, extract daily high/low
            daily: Dict[str, Dict[str, Any]] = {}
            for entry in data.get("list", []):
                dt = entry.get("dt_txt", "")[:10]
                temp = entry["main"]["temp"]
                pop = entry.get("pop", 0)
                desc = entry["weather"][0]["description"] if entry.get("weather") else ""

                if dt not in daily:
                    daily[dt] = {"high": temp, "low": temp, "pop": pop, "desc": desc}
                else:
                    daily[dt]["high"] = max(daily[dt]["high"], temp)
                    daily[dt]["low"] = min(daily[dt]["low"], temp)
                    daily[dt]["pop"] = max(daily[dt]["pop"], pop)

            result["forecast_5day"] = [
                {
                    "date": dt,
                    "high_temp": round(info["high"], 1),
                    "low_temp": round(info["low"], 1),
                    "precipitation_probability": round(info["pop"], 2),
                    "description": info["desc"],
                }
                for dt, info in sorted(daily.items())[:5]
            ]
        except requests.RequestException as e:
            logger.debug(f"OpenWeather forecast failed for {city_code}: {e}")
            result["forecast_5day"] = []
            if result["status"] == "partial":
                result["status"] = "failed"
            else:
                result["status"] = "partial"

        self._set_cache(cache_key, result)
        return result

    def get_all_cities_weather(self) -> Dict[str, Any]:
        """Fetch weather for all 12 cities."""
        cache_key = "owm_all_cities"
        if self._is_cache_valid(cache_key):
            return self._cache[cache_key]

        results = {}
        for code in CITIES:
            results[code] = self.get_weather_forecast(code)

        self._set_cache(cache_key, results)
        return results

    def get_weather_context(self, city_code: Optional[str] = None) -> Dict[str, Any]:
        """
        Return weather context for LLM forecaster.

        Args:
            city_code: Optional specific city. If None, returns all cities.
        """
        if city_code:
            data = self.get_weather_forecast(city_code)
            return {
                "weather_data": data,
                "source": "OpenWeatherMap",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "data_quality": "current" if data.get("status") == "success" else "degraded",
            }

        all_data = self.get_all_cities_weather()
        successes = sum(1 for v in all_data.values() if v.get("status") == "success")
        total = len(all_data)

        return {
            "weather_data": all_data,
            "source": "OpenWeatherMap",
            "cities_covered": total,
            "cities_success": successes,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data_quality": "current" if successes == total else "partial" if successes > 0 else "unavailable",
        }

    def health_check(self) -> str:
        if not self.api_key:
            return "no_key"
        try:
            resp = requests.get(
                f"{OPENWEATHER_BASE}/weather",
                params={"lat": 40.7128, "lon": -74.0060, "appid": self.api_key, "units": "imperial"},
                timeout=10,
            )
            return "online" if resp.status_code == 200 else "offline"
        except Exception:
            return "offline"


_client: Optional[OpenWeatherClient] = None


def get_openweather_client() -> OpenWeatherClient:
    global _client
    if _client is None:
        _client = OpenWeatherClient()
    return _client
