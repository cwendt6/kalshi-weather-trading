"""
Session statistics tracking for the Kalshi trading system.

Tracks in-memory stats (reset on restart) for observability.
"""
from datetime import datetime, timezone
from typing import Any, Dict, Optional
import threading


class SessionStats:
    """Thread-safe in-memory session statistics tracker."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._start_time = datetime.now(timezone.utc)
        self._markets_scanned = 0
        self._opportunities_found = 0
        self._trades_executed = 0
        self._trades_by_strategy: Dict[str, int] = {}
        self._total_entry_price_cents = 0  # sum of entry prices in cents
        self._trades_for_avg = 0  # count for avg calculation
        self._wins = 0
        self._losses = 0
        self._skipped = 0

        # 30-minute window tracking
        self._last_summary_time = datetime.now(timezone.utc)
        self._window_markets_scanned = 0
        self._window_opportunities = 0
        self._window_trades = 0
        self._window_entry_sum = 0
        self._window_trade_count = 0
        self._window_strategies: Dict[str, int] = {}

    def record_markets_scanned(self, count: int) -> None:
        with self._lock:
            self._markets_scanned += count
            self._window_markets_scanned += count

    def record_opportunity(self, count: int = 1) -> None:
        with self._lock:
            self._opportunities_found += count
            self._window_opportunities += count

    def record_trade(self, strategy: str, entry_price_cents: int) -> None:
        with self._lock:
            self._trades_executed += 1
            self._trades_by_strategy[strategy] = self._trades_by_strategy.get(strategy, 0) + 1
            self._total_entry_price_cents += entry_price_cents
            self._trades_for_avg += 1

            self._window_trades += 1
            self._window_strategies[strategy] = self._window_strategies.get(strategy, 0) + 1
            self._window_entry_sum += entry_price_cents
            self._window_trade_count += 1

    def record_skip(self) -> None:
        with self._lock:
            self._skipped += 1

    def record_resolution(self, won: bool) -> None:
        with self._lock:
            if won:
                self._wins += 1
            else:
                self._losses += 1

    def get_session_stats(self) -> Dict[str, Any]:
        """Get cumulative session statistics."""
        with self._lock:
            runtime = (datetime.now(timezone.utc) - self._start_time).total_seconds()
            avg_entry = (
                self._total_entry_price_cents / self._trades_for_avg
                if self._trades_for_avg > 0 else 0
            )
            total_resolved = self._wins + self._losses
            win_rate = self._wins / total_resolved if total_resolved > 0 else 0.0

            return {
                "runtime_seconds": runtime,
                "runtime_hours": runtime / 3600,
                "markets_scanned": self._markets_scanned,
                "opportunities_found": self._opportunities_found,
                "trades_executed": self._trades_executed,
                "trades_by_strategy": dict(self._trades_by_strategy),
                "avg_entry_price_cents": round(avg_entry, 1),
                "wins": self._wins,
                "losses": self._losses,
                "win_rate": round(win_rate, 3),
                "skipped": self._skipped,
            }

    def get_and_reset_window(self) -> Optional[Dict[str, Any]]:
        """Get 30-minute window stats and reset the window counters.

        Returns None if no activity in the window.
        """
        with self._lock:
            window_duration = (datetime.now(timezone.utc) - self._last_summary_time).total_seconds()
            avg_entry = (
                self._window_entry_sum / self._window_trade_count
                if self._window_trade_count > 0 else 0
            )

            result = {
                "window_seconds": window_duration,
                "markets_scanned": self._window_markets_scanned,
                "opportunities_found": self._window_opportunities,
                "trades_executed": self._window_trades,
                "avg_entry_price_cents": round(avg_entry, 1),
                "strategies": dict(self._window_strategies),
            }

            # Reset window
            self._last_summary_time = datetime.now(timezone.utc)
            self._window_markets_scanned = 0
            self._window_opportunities = 0
            self._window_trades = 0
            self._window_entry_sum = 0
            self._window_trade_count = 0
            self._window_strategies = {}

            return result


# Global singleton
_session_stats: Optional[SessionStats] = None


def get_session_stats() -> SessionStats:
    """Get or create the global session stats tracker."""
    global _session_stats
    if _session_stats is None:
        _session_stats = SessionStats()
    return _session_stats
