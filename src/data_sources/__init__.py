"""External data source integrations.

Weather-only mode: only NWS weather data is used.
"""

from .nws_weather import NWSClient, get_temperature_forecast, KALSHI_STATIONS

__all__ = [
    "NWSClient",
    "get_temperature_forecast",
    "KALSHI_STATIONS",
]
