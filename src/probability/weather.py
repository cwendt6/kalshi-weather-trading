"""
Weather Probability Calculator

Computes P(temperature > threshold) or P(temperature in range) using:
1. NWS forecast as the mean
2. Normal distribution with confidence-adjusted std dev
3. Historical forecast error calibration

Supports Kalshi weather markets:
- KXHIGH{CITY}-{DATE}-T{threshold}  (above threshold)
- KXHIGH{CITY}-{DATE}-B{low}.5      (bracket: low to low+1)
- KXLOW{CITY}-{DATE}-T{threshold}   (below threshold for lows)
- KXLOW{CITY}-{DATE}-B{low}.5       (bracket for lows)
"""
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

from src.probability.engine import ProbabilityEstimate
from src.utils.logging import logger
from src.utils.probability_utils import norm_cdf as _norm_cdf


# City code → NWS station mapping (must match Kalshi settlement stations)
CITY_MAP = {
    "NY": "NYC",
    "NYC": "NYC",          # Low-temp tickers use KXLOWTNYC (T? eats T, leaving NYC)
    "CHI": "CHICAGO",
    "MIA": "MIAMI",
    "AUS": "AUSTIN",
    "LA": "LOS_ANGELES",
    "DEN": "DENVER",
    "ATL": "ATLANTA",
    "PHIL": "PHILADELPHIA",
    "SEA": "SEATTLE",
}

# ═══════════════════════════════════════════════════════════════════════
# Standard Deviation Schedule — loaded from config/settings.py
# ═══════════════════════════════════════════════════════════════════════
# σ follows the diurnal cycle: uncertainty is highest before the
# temperature extreme occurs, and drops sharply as we approach/pass it.
# Values widened 1.5x from original aggressive schedule per calibration audit.
# See config/settings.py for the authoritative σ values.

def _load_schedule_from_settings():
    """Load σ schedules from settings. Returns tuple-based format for _lookup_schedule."""
    from config.settings import settings
    high = [(int(pair[0]), pair[1]) for pair in settings.weather_high_std_dev_schedule]
    low = [(int(pair[0]), pair[1]) for pair in settings.weather_low_std_dev_schedule]
    multi = {int(k): tuple(v) for k, v in settings.weather_multi_day_std_dev.items()}
    default = tuple(settings.weather_multi_day_default)
    conf = settings.weather_confidence_multipliers
    return high, low, multi, default, conf

_HIGH_STD_DEV_SCHEDULE, _LOW_STD_DEV_SCHEDULE, _MULTI_DAY_STD_DEV, _DEFAULT_MULTI_DAY, _CONFIDENCE_MULT = _load_schedule_from_settings()


def _lookup_schedule(schedule: list, hour: int) -> float:
    """Look up σ from a stepped schedule by local hour."""
    result = schedule[0][1]  # default to first entry
    for start_hour, sigma in schedule:
        if hour >= start_hour:
            result = sigma
        else:
            break
    return result


def _get_std_dev(lead_days: int, confidence: str = "medium",
                 is_high: bool = True) -> float:
    """Get forecast standard deviation based on lead time and confidence.

    For same-day (lead_days=0), returns the PRE-DAWN value as a conservative
    base. Callers should use get_time_adjusted_std_dev() for time-aware values.

    Can be globally overridden with FORECAST_STD_DEV_OVERRIDE env var.
    """
    override = os.getenv("FORECAST_STD_DEV_OVERRIDE")
    if override:
        try:
            return float(override)
        except ValueError:
            pass

    if lead_days == 0:
        # Return pre-dawn base (widest same-day value) from schedule
        schedule = _HIGH_STD_DEV_SCHEDULE if is_high else _LOW_STD_DEV_SCHEDULE
        base = schedule[0][1]  # First entry is pre-dawn
    else:
        capped = min(lead_days, 3)
        pair = _MULTI_DAY_STD_DEV.get(capped, _DEFAULT_MULTI_DAY)
        base = pair[0] if is_high else pair[1]

    mult = _CONFIDENCE_MULT.get(confidence.lower(), 1.0)
    return base * mult


def get_time_adjusted_std_dev(
    base_std_dev: float,
    hours_to_close: float,
    current_observation: Optional[float] = None,
    forecast_temp: Optional[float] = None,
    is_high: bool = True,
    local_hour: Optional[int] = None,
) -> float:
    """
    Get time-of-day adjusted σ for same-day weather markets.

    Uses the stepped diurnal schedule aligned to when HIGH or LOW
    temperatures typically occur, rather than a generic hours-to-close
    countdown.

    Args:
        base_std_dev: Base σ from _get_std_dev (used as fallback only)
        hours_to_close: Hours until market closes (used as fallback)
        current_observation: Latest observed temperature (°F), if available
        forecast_temp: NWS forecast temperature (°F)
        is_high: True for HIGH temp markets, False for LOW
        local_hour: Local hour (0-23) at the city. If None, falls back
                    to hours_to_close based estimation.

    Returns:
        Adjusted std_dev (always >= 0.5°F to avoid degenerate distributions)
    """
    if local_hour is not None:
        # Use the diurnal schedule
        schedule = _HIGH_STD_DEV_SCHEDULE if is_high else _LOW_STD_DEV_SCHEDULE
        adjusted = _lookup_schedule(schedule, local_hour)
    else:
        # Fallback: use hours_to_close with proportional reduction
        if hours_to_close > 12:
            time_mult = 1.0
        elif hours_to_close > 8:
            time_mult = 0.85
        elif hours_to_close > 4:
            time_mult = 0.70
        elif hours_to_close > 2:
            time_mult = 0.55
        else:
            time_mult = 0.40
        adjusted = base_std_dev * time_mult

    # Observation confirmation: if live temp closely matches forecast,
    # we can tighten further (forecast is tracking well).
    if current_observation is not None and forecast_temp is not None:
        obs_error = abs(current_observation - forecast_temp)
        if obs_error < 1.0:
            adjusted *= 0.75  # Forecast nailed it → very tight
        elif obs_error < 2.0:
            adjusted *= 0.85  # Close enough → moderately tight

    # Floor: never go below 0.5°F (prevents degenerate 99.9% probabilities)
    return max(0.5, adjusted)


class WeatherCalculator:
    """
    Calculates probability for Kalshi weather markets using NWS forecast data
    and normal distribution modeling.
    """

    def __init__(self):
        self._nws_client = None

    @property
    def nws(self):
        if self._nws_client is None:
            try:
                from src.data_sources.nws_weather import NWSClient
                self._nws_client = NWSClient()
            except Exception as e:
                logger.warning(f"NWS client not available: {e}")
        return self._nws_client

    def calculate(
        self,
        ticker: str,
        title: str = "",
        close_time: Optional[datetime] = None,
        context: Optional[Dict] = None,
    ) -> Optional[ProbabilityEstimate]:
        """
        Calculate probability for a weather market.

        Parses the ticker to extract city, date, threshold type, and value,
        then uses NWS forecast + normal distribution to compute probability.
        """
        parsed = self._parse_ticker(ticker)
        if not parsed:
            return None

        city_code, market_date, temp_type, threshold_type, threshold_value = parsed

        # Get NWS forecast
        nws_city = CITY_MAP.get(city_code)
        if not nws_city or not self.nws:
            return None

        try:
            forecast = self.nws.get_forecast(nws_city, market_date)
            if not forecast:
                return ProbabilityEstimate(
                    probability=0.5,
                    confidence=0.1,
                    method="nws_normal",
                    reasoning=f"No NWS forecast available for {nws_city}",
                    data_quality="unavailable",
                )
        except Exception as e:
            logger.debug(f"NWS forecast fetch failed for {nws_city}: {e}")
            return None

        # Get the relevant forecast temperature
        if temp_type == "HIGH":
            forecast_temp = forecast.high_f
        else:
            forecast_temp = forecast.low_f

        confidence = getattr(forecast, "high_confidence", "medium") or "medium"

        # Calculate lead time
        now = datetime.now(timezone.utc)
        if market_date:
            lead_days = max(0, (market_date - now.date()).days)
        elif close_time:
            lead_days = max(0, int((close_time.replace(tzinfo=timezone.utc) - now).total_seconds() / 86400))
        else:
            lead_days = 1

        std_dev = _get_std_dev(lead_days, confidence)

        # Calculate probability based on threshold type
        prob, reasoning = self._compute_probability(
            forecast_temp, threshold_type, threshold_value, std_dev
        )

        # Confidence based on data quality and lead time
        calc_confidence = self._compute_confidence(lead_days, confidence, std_dev)

        return ProbabilityEstimate(
            probability=prob,
            confidence=calc_confidence,
            method="nws_normal",
            reasoning=reasoning,
            data_quality="good" if lead_days <= 2 else "partial",
            features={
                "forecast_temp": forecast_temp,
                "threshold_value": threshold_value,
                "threshold_type": threshold_type,
                "std_dev": std_dev,
                "lead_days": lead_days,
                "confidence_level": confidence,
                "city": city_code,
                "temp_type": temp_type,
            },
        )

    def _compute_probability(
        self, forecast_temp: float, threshold_type: str,
        threshold_value: float, std_dev: float
    ) -> Tuple[float, str]:
        """
        Compute probability using normal distribution.

        threshold_type:
            "above" → P(temp > threshold)
            "below" → P(temp < threshold)
            "bracket" → P(threshold <= temp < threshold + 1)
        """
        if threshold_type == "above":
            # P(temp > threshold)
            prob = 1.0 - _norm_cdf(threshold_value, loc=forecast_temp, scale=std_dev)
            reasoning = (
                f"P(temp > {threshold_value}°F) = {prob:.1%} | "
                f"NWS forecast={forecast_temp}°F, σ={std_dev:.1f}°F"
            )
        elif threshold_type == "below":
            # P(temp < threshold)
            prob = _norm_cdf(threshold_value, loc=forecast_temp, scale=std_dev)
            reasoning = (
                f"P(temp < {threshold_value}°F) = {prob:.1%} | "
                f"NWS forecast={forecast_temp}°F, σ={std_dev:.1f}°F"
            )
        elif threshold_type == "bracket":
            # Kalshi brackets cover 2 integer °F. B36.5 = "36-37°".
            # The .5 value is the midpoint; CDF range = [midpoint-1, midpoint+1).
            lower = threshold_value - 1.0
            upper = threshold_value + 1.0
            prob = _norm_cdf(upper, loc=forecast_temp, scale=std_dev) - \
                   _norm_cdf(lower, loc=forecast_temp, scale=std_dev)
            reasoning = (
                f"P({lower:.0f}°F ≤ temp < {upper:.0f}°F) = {prob:.1%} | "
                f"NWS forecast={forecast_temp}°F, σ={std_dev:.1f}°F"
            )
        else:
            prob = 0.5
            reasoning = f"Unknown threshold type: {threshold_type}"

        # Clamp to reasonable range
        prob = max(0.01, min(0.99, prob))
        return prob, reasoning

    def _compute_confidence(
        self, lead_days: int, nws_confidence: str, std_dev: float
    ) -> float:
        """
        Compute confidence in our probability estimate.

        Higher confidence when:
        - Shorter lead time (same-day forecasts are much more accurate)
        - NWS reports high confidence
        - Lower std dev (less uncertainty)
        """
        # Base confidence from lead time
        if lead_days == 0:
            base = 0.85
        elif lead_days == 1:
            base = 0.75
        elif lead_days == 2:
            base = 0.60
        else:
            base = 0.40

        # Adjust for NWS confidence
        conf_multiplier = {"high": 1.1, "medium": 1.0, "low": 0.75}
        base *= conf_multiplier.get(nws_confidence.lower(), 1.0)

        return min(0.95, max(0.1, base))

    def compute_probability(
        self,
        forecast_temp: float,
        threshold: float,
        threshold_type: str,
        is_bracket: bool,
        is_high: bool,
        days_out: int,
        confidence: str = "medium",
        local_hour: Optional[int] = None,
    ) -> Tuple[float, str, float]:
        """
        Compute probability with unified σ selection and CDF math.

        This is the single source of truth for temperature probability.
        Both WeatherCalculator.calculate() and WeatherStrategy._calculate_probability()
        should delegate here for the core math.

        Args:
            forecast_temp: NWS forecast temperature (°F), possibly obs-biased.
            threshold: Temperature threshold value.
            threshold_type: "above" or "below" (probability direction).
            is_bracket: True for bracket markets (B-tickers).
            is_high: True for HIGH temp markets, False for LOW.
            days_out: Forecast lead days (0 = same-day).
            confidence: NWS confidence level ("high"/"medium"/"low").
            local_hour: Local hour (0-23) for same-day σ lookup. If None,
                        uses pre-dawn default for same-day.

        Returns:
            Tuple of (probability, reasoning, std_dev used).
        """
        # σ selection from unified config schedule
        if days_out == 0 and local_hour is not None:
            schedule = _HIGH_STD_DEV_SCHEDULE if is_high else _LOW_STD_DEV_SCHEDULE
            std_dev = _lookup_schedule(schedule, local_hour)
        elif days_out == 0:
            # Pre-dawn default (widest same-day value)
            std_dev = _HIGH_STD_DEV_SCHEDULE[0][1] if is_high else _LOW_STD_DEV_SCHEDULE[0][1]
        else:
            capped = min(days_out, 3)
            pair = _MULTI_DAY_STD_DEV.get(capped, _DEFAULT_MULTI_DAY)
            std_dev = pair[0] if is_high else pair[1]

        # Apply confidence multiplier
        std_dev *= _CONFIDENCE_MULT.get(confidence.lower(), 1.0)

        # CDF computation
        prob, reasoning = self._compute_probability(
            forecast_temp, threshold_type if not is_bracket else "bracket",
            threshold, std_dev,
        )

        return prob, reasoning, std_dev

    def _parse_ticker(self, ticker: str) -> Optional[tuple]:
        """
        Parse a Kalshi weather ticker into components.

        Returns: (city_code, market_date, temp_type, threshold_type, threshold_value)
        or None if not a recognized weather ticker.

        Examples:
            KXHIGHNY-26FEB07-T24    → ("NY", date(2026,2,7), "HIGH", "above", 24.0)
            KXHIGHNY-26FEB07-B24.5  → ("NY", date(2026,2,7), "HIGH", "bracket", 24.0)
            KXLOWTNYC-26FEB07-T2    → ("NYC", date(2026,2,7), "LOW", "below", 2.0)
            KXHIGHNY-26FEB07-T31    → ("NY", date(2026,2,7), "HIGH", "above", 31.0)
        """
        ticker = ticker.upper()

        # Match HIGH or LOW temperature markets
        # Pattern: KX(HIGH|LOW)T?{CITY}-{DATE}-{TYPE}{VALUE}
        match = re.match(
            r"KX(HIGH|LOW)T?(\w+?)-(\d{2})([A-Z]{3})(\d{2})-([TB])(\d+\.?\d*)",
            ticker,
        )
        if not match:
            return None

        temp_type = match.group(1)  # HIGH or LOW
        city_code = match.group(2)
        year_short = int(match.group(3))
        month_str = match.group(4)
        day = int(match.group(5))
        thresh_char = match.group(6)  # T or B
        thresh_value = float(match.group(7))

        # Parse date
        month_map = {
            "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
            "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
        }
        month = month_map.get(month_str)
        if not month:
            return None

        year = 2000 + year_short
        try:
            market_date = date(year, month, day)
        except ValueError:
            return None

        # Determine threshold type
        if thresh_char == "T":
            # "T" means threshold — could be above or below
            # For HIGH temps: T usually means "above this threshold" (Will high be > X?)
            # For LOW temps with "T" prefix in city: could mean "below" (Will low be < X?)
            if temp_type == "HIGH":
                threshold_type = "above"
            else:
                threshold_type = "below"
        elif thresh_char == "B":
            # "B" means bracket (e.g., B36.5 = "will temp be 36-37°?")
            # The .5 value is the midpoint of a 2°F bracket.
            # Keep it as-is so _compute_probability can use ±1 range.
            threshold_type = "bracket"
        else:
            return None

        return (city_code, market_date, temp_type, threshold_type, thresh_value)
