"""
Minimal forecaster stub for weather-only mode.

Provides the Forecast dataclass and get_default_forecaster() used by
edge.py and exit logic. The actual forecasting for weather markets is
done by WeatherStrategy using NWS data directly.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from src.utils.logging import logger


@dataclass
class Forecast:
    """A probability forecast for a market."""

    ticker: str
    probability: float  # 0.0 to 1.0
    confidence: float  # 0.0 to 1.0
    method: str  # e.g. "nws_weather", "rule_based"
    reasoning: str
    timestamp: datetime = datetime.now(timezone.utc)


class RuleBasedForecaster:
    """Simple forecaster that reads from ForecastDB (populated by weather strategy)."""

    def forecast(self, ticker: str) -> Optional[Forecast]:
        """Get latest forecast from database."""
        try:
            from src.data.database import get_db_session
            from src.data.models import ForecastDB

            with next(get_db_session()) as session:
                record = (
                    session.query(ForecastDB)
                    .filter(ForecastDB.ticker == ticker)
                    .order_by(ForecastDB.created_at.desc())
                    .first()
                )
                if record:
                    return Forecast(
                        ticker=str(record.ticker),
                        probability=float(record.probability),
                        confidence=float(record.confidence or 0.5),
                        method=str(record.method or "unknown"),
                        reasoning=str(record.context_data or ""),
                        timestamp=record.created_at or datetime.now(timezone.utc),
                    )
        except Exception as e:
            logger.debug(f"Forecast lookup failed for {ticker}: {e}")
        return None


# Singleton
_forecaster: Optional[RuleBasedForecaster] = None


def get_default_forecaster() -> RuleBasedForecaster:
    """Get or create the default forecaster."""
    global _forecaster
    if _forecaster is None:
        _forecaster = RuleBasedForecaster()
    return _forecaster
