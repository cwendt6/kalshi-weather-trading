"""
Conservative forecast-shift portfolio reshaper.

Detects when NWS forecast changes significantly and generates reshape
actions for the city bracket portfolio. Uses bracket-relative triggers
(not just absolute °F shift) to avoid unnecessary churn.

Key insight from simulation: the near-YES hedge naturally handles ±2-3°F
shifts. Only reshape when the shift is large enough that the entire
portfolio structure is wrong.

Reshape trigger rules:
1. New forecast lands in a DIFFERENT bracket than current AND shift >= 4°F
2. OR distance from any YES position to new forecast exceeds 5°F
3. AND reshape count < 2 per city per day (hard cap)
"""

import math
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Dict, List, Optional, Tuple

from src.utils.logging import logger


class ReshapeAction(Enum):
    SELL_YES = "sell_yes"
    SELL_NO = "sell_no"
    TRIM_NO = "trim_no"
    BUY_YES = "buy_yes"
    BUY_NO = "buy_no"
    HOLD = "hold"


@dataclass
class ReshapeOrder:
    ticker: str
    action: ReshapeAction
    side: str  # "yes" or "no"
    quantity: int  # Contracts to trade
    reason: str
    priority: int  # Lower = execute first
    estimated_cost: float  # Estimated transaction cost


@dataclass
class ReshapeDecision:
    city: str
    market_date: date
    old_forecast: float
    new_forecast: float
    shift_degrees: float
    orders: List[ReshapeOrder] = field(default_factory=list)
    estimated_total_cost: float = 0.0
    should_reshape: bool = False
    reason: str = ""


class ForecastReshaper:
    """Conservative portfolio reshaper triggered by NWS forecast changes."""

    MIN_SHIFT_DEGREES = float(os.getenv("RESHAPE_MIN_SHIFT", "4.0"))
    MAX_YES_DISTANCE = float(os.getenv("RESHAPE_MAX_YES_DISTANCE", "5.0"))
    MAX_RESHAPES_PER_DAY = int(os.getenv("RESHAPE_MAX_PER_DAY", "2"))
    BRACKET_WIDTH = 2.0
    SPREAD_COST_CENTS = 2.0  # Estimated bid-ask spread per contract

    def __init__(self):
        self._last_forecast: Dict[str, float] = {}
        self._reshape_counts: Dict[Tuple[str, str], int] = {}  # (city, date_str) -> count
        self._last_reset_date: Optional[date] = None

    def _reset_daily(self) -> None:
        """Reset reshape counts at midnight."""
        today = date.today()
        if self._last_reset_date != today:
            self._reshape_counts.clear()
            self._last_reset_date = today

    def _get_bracket_for_temp(self, temp: float) -> Tuple[float, float]:
        """Get the 2°F bracket containing this temperature.

        Brackets aligned to even numbers: [30,32), [32,34), [34,36)...
        """
        lower = math.floor(temp / self.BRACKET_WIDTH) * self.BRACKET_WIDTH
        upper = lower + self.BRACKET_WIDTH
        return (lower, upper)

    def check_forecast_shift(
        self,
        city: str,
        new_forecast: float,
        market_date: date,
    ) -> Optional[ReshapeDecision]:
        """Check if a forecast shift warrants portfolio reshaping.

        Called every 15 minutes when NWS forecasts are refreshed.
        Returns ReshapeDecision if reshape is needed, None otherwise.
        """
        self._reset_daily()

        old_forecast = self._last_forecast.get(city)
        self._last_forecast[city] = new_forecast

        if old_forecast is None:
            # First forecast of the day — no shift to compare
            logger.debug(f"Reshaper: first forecast for {city}: {new_forecast:.0f}F")
            return None

        shift = abs(new_forecast - old_forecast)
        if shift < 0.5:
            return None  # Trivial change, skip

        should, reason = self._should_reshape(city, old_forecast, new_forecast, market_date)

        decision = ReshapeDecision(
            city=city,
            market_date=market_date,
            old_forecast=old_forecast,
            new_forecast=new_forecast,
            shift_degrees=shift,
            should_reshape=should,
            reason=reason,
        )

        if should:
            key = (city, market_date.isoformat())
            self._reshape_counts[key] = self._reshape_counts.get(key, 0) + 1
            logger.info(
                f"🔄 RESHAPE TRIGGERED: {city} | "
                f"forecast {old_forecast:.0f}→{new_forecast:.0f}F "
                f"(shift={shift:.1f}F) | {reason}"
            )
        else:
            logger.debug(
                f"Reshaper: {city} shift {old_forecast:.0f}→{new_forecast:.0f}F "
                f"({shift:.1f}F) — no reshape: {reason}"
            )

        return decision

    def _should_reshape(
        self,
        city: str,
        old_forecast: float,
        new_forecast: float,
        market_date: date,
    ) -> Tuple[bool, str]:
        """Determine if reshape is warranted.

        CONSERVATIVE logic:
        1. Shift >= MIN_SHIFT_DEGREES? If not → NO
        2. Forecast crossed into different bracket? If not → NO
        3. Already reshaped MAX_RESHAPES_PER_DAY times? If so → NO
        4. Within 2 hours of market close? If so → NO
        """
        shift = abs(new_forecast - old_forecast)

        # 1. Minimum shift threshold
        if shift < self.MIN_SHIFT_DEGREES:
            return False, f"shift {shift:.1f}F < {self.MIN_SHIFT_DEGREES}F minimum"

        # 2. Bracket-relative: must cross into a different bracket
        old_bracket = self._get_bracket_for_temp(old_forecast)
        new_bracket = self._get_bracket_for_temp(new_forecast)
        if old_bracket == new_bracket:
            return False, f"still in same bracket {old_bracket} despite {shift:.1f}F shift"

        # 3. Max reshapes per day
        key = (city, market_date.isoformat())
        count = self._reshape_counts.get(key, 0)
        if count >= self.MAX_RESHAPES_PER_DAY:
            return False, f"max reshapes reached ({count}/{self.MAX_RESHAPES_PER_DAY})"

        # 4. Too close to market close (2 hours)
        try:
            from zoneinfo import ZoneInfo
            from src.data_sources.nws_weather import KALSHI_STATIONS

            # Get the NWS city name
            from src.strategy.weather_strategy import WeatherStrategy
            nws_city = city
            tz_name = KALSHI_STATIONS.get(nws_city, {}).get("timezone", "America/New_York")
            local_hour = datetime.now(ZoneInfo(tz_name)).hour
            if local_hour >= 22:  # Within ~2h of midnight (markets close ~11:59pm)
                return False, f"too late in day ({local_hour}:00 local)"
        except Exception:
            pass

        return True, f"bracket changed {old_bracket}→{new_bracket}, shift={shift:.1f}F"

    def generate_reshape_orders(
        self,
        city: str,
        old_forecast: float,
        new_forecast: float,
        current_positions: List[dict],
        available_brackets: List[dict],
    ) -> List[ReshapeOrder]:
        """Generate specific orders for a portfolio reshape.

        Args:
            current_positions: [{ticker, side, quantity, entry_price}]
            available_brackets: [{ticker, threshold, yes_price, no_price}]

        Logic:
        1. SELL YES positions >= 5°F from new forecast
        2. SELL NO positions at forecast center (0-1° away)
        3. TRIM 50% NO positions 2° away
        4. BUY YES on new forecast bracket (if not already owned)
        5. Check near-YES hedge coverage before buying
        6. BUY NO on brackets now far from new forecast

        Sells have priority 1-10, buys 11+.
        """
        orders: List[ReshapeOrder] = []
        new_bracket = self._get_bracket_for_temp(new_forecast)
        priority_counter = 1

        # Track which brackets we already own
        owned_tickers = {p["ticker"] for p in current_positions}

        # ── SELL phase (free up capital first) ──
        for pos in current_positions:
            ticker = pos["ticker"]
            side = pos["side"]
            qty = pos["quantity"]
            threshold = self._extract_threshold(ticker)
            if threshold is None:
                continue

            bracket_mid = threshold  # threshold IS the midpoint for Kalshi brackets
            distance = abs(new_forecast - bracket_mid)

            if side == "yes":
                # Sell YES positions that are now too far from forecast
                if distance >= self.MAX_YES_DISTANCE:
                    orders.append(ReshapeOrder(
                        ticker=ticker,
                        action=ReshapeAction.SELL_YES,
                        side="yes",
                        quantity=qty,
                        reason=f"YES {distance:.0f}F from new forecast {new_forecast:.0f}F",
                        priority=priority_counter,
                        estimated_cost=qty * self.SPREAD_COST_CENTS / 100.0,
                    ))
                    priority_counter += 1

            elif side == "no":
                # Sell NO positions that are now at the forecast center
                if distance <= 1.0:
                    orders.append(ReshapeOrder(
                        ticker=ticker,
                        action=ReshapeAction.SELL_NO,
                        side="no",
                        quantity=qty,
                        reason=f"NO now at forecast center ({distance:.0f}F away)",
                        priority=priority_counter,
                        estimated_cost=qty * self.SPREAD_COST_CENTS / 100.0,
                    ))
                    priority_counter += 1
                elif distance <= 2.0:
                    # Trim 50% if getting risky
                    trim_qty = max(1, qty // 2)
                    orders.append(ReshapeOrder(
                        ticker=ticker,
                        action=ReshapeAction.TRIM_NO,
                        side="no",
                        quantity=trim_qty,
                        reason=f"NO getting close to forecast ({distance:.0f}F) — trim 50%",
                        priority=priority_counter,
                        estimated_cost=trim_qty * self.SPREAD_COST_CENTS / 100.0,
                    ))
                    priority_counter += 1

        # ── BUY phase (deploy freed capital) ──
        priority_counter = 11  # Buys start at priority 11

        # Check if we need a new YES on the forecast bracket
        need_forecast_yes = True
        for pos in current_positions:
            threshold = self._extract_threshold(pos["ticker"])
            if threshold is None:
                continue
            distance = abs(new_forecast - threshold)
            # If we already own YES within 2° of new forecast, the near-YES hedge covers us
            if pos["side"] == "yes" and distance <= 2.0:
                # Check if this position is being sold in this reshape
                being_sold = any(
                    o.ticker == pos["ticker"] and o.action == ReshapeAction.SELL_YES
                    for o in orders
                )
                if not being_sold:
                    need_forecast_yes = False
                    logger.info(
                        f"Reshaper: existing YES {pos['ticker']} covers "
                        f"new forecast ({distance:.1f}F away) — skip new YES buy"
                    )
                    break

        if need_forecast_yes:
            # Find the bracket market at the new forecast
            for bracket in available_brackets:
                threshold = bracket.get("threshold", 0)
                if abs(threshold - new_forecast) <= 1.5:  # Within the bracket
                    if bracket["ticker"] not in owned_tickers:
                        orders.append(ReshapeOrder(
                            ticker=bracket["ticker"],
                            action=ReshapeAction.BUY_YES,
                            side="yes",
                            quantity=10,  # Default quantity, will be sized by executor
                            reason=f"New forecast bracket YES at {threshold:.0f}F",
                            priority=priority_counter,
                            estimated_cost=10 * (bracket.get("yes_price", 20) / 100.0),
                        ))
                        priority_counter += 1
                        break

        # Buy NO on brackets now far from forecast
        for bracket in available_brackets:
            threshold = bracket.get("threshold", 0)
            distance = abs(new_forecast - threshold)
            if distance >= 5.0 and bracket["ticker"] not in owned_tickers:
                no_price = bracket.get("no_price", 95)
                if no_price < 98:  # Don't overpay
                    orders.append(ReshapeOrder(
                        ticker=bracket["ticker"],
                        action=ReshapeAction.BUY_NO,
                        side="no",
                        quantity=10,
                        reason=f"Far bracket NO at {threshold:.0f}F ({distance:.0f}F from forecast)",
                        priority=priority_counter,
                        estimated_cost=10 * (no_price / 100.0),
                    ))
                    priority_counter += 1

        # Sort by priority (sells first, then buys)
        orders.sort(key=lambda o: o.priority)

        return orders

    def estimate_reshape_cost(self, orders: List[ReshapeOrder]) -> float:
        """Estimate total transaction cost of a reshape.

        Includes bid-ask spread on all trades.
        """
        return sum(o.estimated_cost for o in orders)

    def should_execute_reshape(
        self,
        decision: ReshapeDecision,
        estimated_edge_gain: float = 0.0,
    ) -> bool:
        """Final gate: only execute if benefit > cost × 1.5.

        Args:
            decision: The reshape decision with orders
            estimated_edge_gain: Sum of edge improvements from new positions

        Returns True only if benefit exceeds cost with 50% safety margin.
        """
        if not decision.should_reshape:
            return False

        cost = self.estimate_reshape_cost(decision.orders)
        decision.estimated_total_cost = cost

        # Benefit must exceed cost by 50% safety margin
        if estimated_edge_gain > 0 and estimated_edge_gain > cost * 1.5:
            logger.info(
                f"Reshape cost-benefit OK: edge_gain=${estimated_edge_gain:.2f} > "
                f"cost=${cost:.2f} × 1.5 = ${cost * 1.5:.2f}"
            )
            return True

        # If no edge estimate available, use heuristic: only if shift >= 5°F
        if estimated_edge_gain <= 0 and decision.shift_degrees >= 5.0:
            logger.info(
                f"Reshape approved (heuristic): shift={decision.shift_degrees:.1f}F >= 5.0F"
            )
            return True

        logger.info(
            f"Reshape BLOCKED by cost-benefit: edge_gain=${estimated_edge_gain:.2f} <= "
            f"cost=${cost:.2f} × 1.5 = ${cost * 1.5:.2f}"
        )
        return False

    def _extract_threshold(self, ticker: str) -> Optional[float]:
        """Extract threshold from a bracket ticker like KXHIGHNY-26FEB13-B36.5."""
        import re
        match = re.search(r'-B([\d.]+)$', ticker.upper())
        if match:
            return float(match.group(1))
        return None

    def record_forecast(self, city: str, forecast_temp: float) -> None:
        """Record a forecast without triggering reshape check.

        Used for initial forecast loading at startup.
        """
        self._last_forecast[city] = forecast_temp

    def get_reshape_count(self, city: str, market_date: date) -> int:
        """Get current reshape count for a city/date."""
        key = (city, market_date.isoformat())
        return self._reshape_counts.get(key, 0)


# ── Singleton ────────────────────────────────────────────────────────

_reshaper: Optional[ForecastReshaper] = None


def get_forecast_reshaper() -> ForecastReshaper:
    """Get or create the global ForecastReshaper instance."""
    global _reshaper
    if _reshaper is None:
        _reshaper = ForecastReshaper()
    return _reshaper
