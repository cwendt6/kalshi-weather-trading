"""
Weather-specific session statistics for focused weather trading mode.

Tracks weather forecasts, trades, cities, and edge metrics.
"""
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Set
import threading


class WeatherStats:
    """Thread-safe weather-specific stats tracker."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._start_time = datetime.now(timezone.utc)
        self._forecasts_generated = 0
        self._opportunities_found = 0
        self._trades_executed = 0
        self._cities_active: Set[str] = set()
        self._total_edge = 0.0
        self._edge_count = 0
        self._total_entry_cents = 0
        self._entry_count = 0
        self._skipped = 0
        self._fee_blocked = 0

        # 30-minute window
        self._last_summary_time = datetime.now(timezone.utc)
        self._w_forecasts = 0
        self._w_opportunities = 0
        self._w_trades = 0
        self._w_cities: Set[str] = set()
        self._w_total_edge = 0.0
        self._w_edge_count = 0
        self._w_total_entry = 0
        self._w_entry_count = 0

    def record_forecast(self, city: str) -> None:
        with self._lock:
            self._forecasts_generated += 1
            self._cities_active.add(city)
            self._w_forecasts += 1
            self._w_cities.add(city)

    def record_opportunity(self, edge: float) -> None:
        with self._lock:
            self._opportunities_found += 1
            self._total_edge += abs(edge)
            self._edge_count += 1
            self._w_opportunities += 1
            self._w_total_edge += abs(edge)
            self._w_edge_count += 1

    def record_trade(self, entry_price_cents: int, edge: float, city: str) -> None:
        with self._lock:
            self._trades_executed += 1
            self._total_entry_cents += entry_price_cents
            self._entry_count += 1
            self._cities_active.add(city)
            self._w_trades += 1
            self._w_total_entry += entry_price_cents
            self._w_entry_count += 1
            self._w_cities.add(city)

    def record_skip(self) -> None:
        with self._lock:
            self._skipped += 1

    def record_fee_block(self) -> None:
        with self._lock:
            self._fee_blocked += 1

    def get_stats(self) -> Dict[str, Any]:
        """Get cumulative weather stats."""
        with self._lock:
            avg_edge = (self._total_edge / self._edge_count * 100) if self._edge_count > 0 else 0
            avg_entry = (self._total_entry_cents / self._entry_count) if self._entry_count > 0 else 0
            return {
                "forecasts_generated": self._forecasts_generated,
                "opportunities_found": self._opportunities_found,
                "trades_executed": self._trades_executed,
                "avg_edge_pct": round(avg_edge, 1),
                "avg_entry_price_cents": round(avg_entry, 1),
                "cities_active": sorted(self._cities_active),
                "skipped": self._skipped,
                "fee_blocked": self._fee_blocked,
            }

    def get_and_reset_window(self) -> Dict[str, Any]:
        """Get 30-minute window stats and reset."""
        with self._lock:
            avg_edge = (self._w_total_edge / self._w_edge_count * 100) if self._w_edge_count > 0 else 0
            avg_entry = (self._w_total_entry / self._w_entry_count) if self._w_entry_count > 0 else 0
            cities = sorted(self._w_cities)

            result = {
                "forecasts_generated": self._w_forecasts,
                "opportunities_found": self._w_opportunities,
                "trades_executed": self._w_trades,
                "avg_edge_pct": round(avg_edge, 1),
                "avg_entry_price_cents": round(avg_entry, 1),
                "cities_active": cities,
            }

            # Reset window
            self._last_summary_time = datetime.now(timezone.utc)
            self._w_forecasts = 0
            self._w_opportunities = 0
            self._w_trades = 0
            self._w_cities = set()
            self._w_total_edge = 0.0
            self._w_edge_count = 0
            self._w_total_entry = 0
            self._w_entry_count = 0

            return result


# Global singleton
_weather_stats: Optional[WeatherStats] = None


def get_weather_stats() -> WeatherStats:
    """Get or create the global weather stats tracker."""
    global _weather_stats
    if _weather_stats is None:
        _weather_stats = WeatherStats()
    return _weather_stats
