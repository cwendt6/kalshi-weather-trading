"""
City Bracket Portfolio Manager: treats all brackets for a (city, date) as a single portfolio unit.

Instead of picking individual trades, this module builds a portfolio view of
all temperature brackets for each city on each date, classifying each bracket
by distance from the NWS forecast and assigning a role (far_no, medium_no,
forecast_yes, near_yes_hedge).

Capital allocation follows Monte Carlo simulation results:
- 50% to far NOs (backbone consistency, 95% win rate)
- 20% to medium NOs (89% win rate)
- 15% to forecast YES (swing-trade, dynamic exit)
- 15% to near-YES hedge (natural hedge, 22% win rate, +$0.25/day)

Entry order is outside-in: furthest NOs first, then YES positions.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Dict, List, Optional

from src.data_sources.nws_weather import get_active_cities
from src.utils.fees import calculate_fee_on_winnings, is_profitable_after_fees
from src.utils.logging import logger


# ── Enums & Data Structures ──────────────────────────────────────────


class BracketRole(Enum):
    """Role of a bracket within a city portfolio."""
    FORECAST_YES = "forecast_yes"       # Buy YES — bracket containing our forecast
    NEAR_YES_HEDGE = "near_yes_hedge"   # Buy YES — adjacent bracket, natural hedge
    MEDIUM_NO = "medium_no"             # Buy NO — 3° from forecast
    FAR_NO = "far_no"                   # Buy NO — 4-5° from forecast
    VERY_FAR_NO = "very_far_no"         # Buy NO — 6°+ from forecast
    SKIP = "skip"                       # Don't trade this bracket


@dataclass
class BracketView:
    """One bracket within a city portfolio."""
    ticker: str
    bracket_lower: float          # e.g., 35.0
    bracket_upper: float          # e.g., 37.0
    distance_from_forecast: float # °F, signed (negative = below forecast)
    role: BracketRole
    side: str                     # "yes" or "no"
    our_probability: float        # Our model P(temp in bracket)
    market_yes_price: float       # Current Kalshi YES price (0-1)
    market_no_price: float        # Current Kalshi NO price (0-1)
    edge: float                   # our_prob - market_price for our side
    recommended_allocation_pct: float  # % of city budget to allocate


@dataclass
class CityBracketPortfolio:
    """Complete bracket portfolio for one city on one date."""
    city: str
    market_date: date
    forecast_high: float          # NWS forecast high temperature
    forecast_low: float           # NWS forecast low temperature
    forecast_source: str = "NWS"
    brackets: List[BracketView] = field(default_factory=list)
    total_edge: float = 0.0       # Sum of positive edges across tradeable brackets

    # Tracking
    last_forecast_temp: float = 0.0   # For shift detection
    reshape_count: int = 0            # Max 2 per day
    created_at: Optional[datetime] = None


# ── Portfolio Manager ─────────────────────────────────────────────────


class CityPortfolioManager:
    """Builds and manages bracket portfolios for all active cities."""

    # Capital allocation percentages (from simulation results)
    FAR_NO_PCT = 0.50        # 50% to far NOs (includes very_far)
    MEDIUM_NO_PCT = 0.20     # 20% to medium NOs
    FORECAST_YES_PCT = 0.15  # 15% to forecast YES
    NEAR_YES_PCT = 0.15      # 15% to near-YES hedge

    # Edge thresholds
    MIN_NO_EDGE = 0.03       # 3% minimum edge for NO trades
    MIN_YES_EDGE = 0.02      # 2% minimum edge for forecast YES (lower bar)

    @property
    def ACTIVE_CITIES(self) -> list:
        return get_active_cities()

    def build_portfolio(
        self,
        city: str,
        market_date: date,
        forecast_temp: float,
        brackets: List[dict],
        current_prices: Dict[str, dict],
        lead_days: int = 1,
        confidence: str = "medium",
        hours_to_close: Optional[float] = None,
        current_observation: Optional[float] = None,
        is_high: bool = True,
    ) -> CityBracketPortfolio:
        """
        Build a portfolio view for one city/date.

        Args:
            city: Standardized city name (e.g., "NYC")
            market_date: Date the markets settle
            forecast_temp: NWS forecast temperature (high or low depending on market type)
            brackets: List of dicts with keys: ticker, threshold, type ("above"/"below"),
                      is_bracket (bool)
            current_prices: Dict[ticker] -> {yes_ask, yes_bid, no_ask, no_bid} (cents)
            lead_days: Days until market settles (0=today, 1=tomorrow)
            confidence: NWS confidence level ("high", "medium", "low")
            hours_to_close: Hours until market closes/settles (for time-of-day adjustment)
            current_observation: Latest observed temperature (°F) for uncertainty reduction
            is_high: True for HIGH temp markets, False for LOW

        Returns:
            CityBracketPortfolio with classified brackets and allocation percentages.
        """
        # Get dynamic std_dev for this market's lead time
        from src.probability.weather import _get_std_dev, get_time_adjusted_std_dev

        # Determine local hour for time-of-day σ schedule
        local_hour = None
        if lead_days == 0:
            try:
                from zoneinfo import ZoneInfo
                from src.data_sources.nws_weather import KALSHI_STATIONS
                tz_name = KALSHI_STATIONS.get(city, {}).get("timezone", "America/New_York")
                local_hour = datetime.now(ZoneInfo(tz_name)).hour
            except Exception:
                pass

        base_std = _get_std_dev(lead_days, confidence, is_high=is_high)

        if hours_to_close is not None or local_hour is not None:
            std_dev = get_time_adjusted_std_dev(
                base_std, hours_to_close or 12.0,
                current_observation=current_observation,
                forecast_temp=forecast_temp,
                is_high=is_high,
                local_hour=local_hour,
            )
        else:
            std_dev = base_std

        portfolio = CityBracketPortfolio(
            city=city,
            market_date=market_date,
            forecast_high=forecast_temp,
            forecast_low=0.0,
            last_forecast_temp=forecast_temp,
            created_at=datetime.now(timezone.utc),
        )

        # Build bracket views
        bracket_views: List[BracketView] = []
        forecast_bracket_lower = None
        forecast_bracket_upper = None

        for b in brackets:
            ticker = b["ticker"]
            threshold = float(b["threshold"])

            # Kalshi bracket: threshold ± 1°F → 2°F wide bracket
            bracket_lower = threshold - 1.0
            bracket_upper = threshold + 1.0

            distance = self._calc_distance(forecast_temp, bracket_lower, bracket_upper)

            # Determine role from distance
            role = self.classify_bracket(bracket_lower, bracket_upper, forecast_temp)

            # Track the forecast bracket boundaries for hedge direction
            if role == BracketRole.FORECAST_YES:
                forecast_bracket_lower = bracket_lower
                forecast_bracket_upper = bracket_upper

            # Get prices
            price_data = current_prices.get(ticker, {})
            yes_ask = (price_data.get("yes_ask") or 0) / 100.0
            yes_bid = (price_data.get("yes_bid") or 0) / 100.0
            no_ask = (price_data.get("no_ask") or 0) / 100.0
            no_bid = (price_data.get("no_bid") or 0) / 100.0

            # Use ask prices for entry (what we'd pay)
            market_yes = yes_ask if yes_ask > 0 else yes_bid
            market_no = no_ask if no_ask > 0 else no_bid
            if market_yes <= 0 and market_no <= 0:
                continue  # No usable price data

            # Calculate our probability for this bracket
            our_prob = self._calc_bracket_probability(
                forecast_temp, bracket_lower, bracket_upper, std_dev=std_dev
            )

            # Determine side and edge
            if role == BracketRole.FORECAST_YES:
                side = "yes"
                edge = our_prob - market_yes if market_yes > 0 else 0.0
            elif role == BracketRole.NEAR_YES_HEDGE:
                # Evaluate BOTH sides — if YES edge is negative, the market
                # is overpricing YES on this adjacent bracket.  Flip to NO
                # (reclassify as MEDIUM_NO) instead of buying a losing hedge.
                yes_edge = our_prob - market_yes if market_yes > 0 else 0.0
                no_prob = 1.0 - our_prob
                no_edge = no_prob - market_no if market_no > 0 else 0.0

                if yes_edge < 0 and no_edge >= self.MIN_NO_EDGE:
                    # YES is negative EV — reclassify to NO side
                    side = "no"
                    edge = no_edge
                    role = BracketRole.MEDIUM_NO
                    logger.info(
                        f"  📊 HEDGE FLIP: {ticker} | near_yes_hedge → medium_no "
                        f"(YES edge {yes_edge:.1%} negative, NO edge {no_edge:.1%})"
                    )
                else:
                    side = "yes"
                    edge = yes_edge
            else:
                side = "no"
                no_prob = 1.0 - our_prob
                edge = no_prob - market_no if market_no > 0 else 0.0

            bracket_views.append(BracketView(
                ticker=ticker,
                bracket_lower=bracket_lower,
                bracket_upper=bracket_upper,
                distance_from_forecast=distance,
                role=role,
                side=side,
                our_probability=our_prob,
                market_yes_price=market_yes,
                market_no_price=market_no,
                edge=edge,
                recommended_allocation_pct=0.0,  # Set below
            ))

        # Resolve near-YES hedge direction
        if forecast_bracket_lower is not None:
            self._assign_hedge(
                bracket_views, forecast_temp,
                forecast_bracket_lower, forecast_bracket_upper,
            )

        # Set allocation percentages
        self._set_allocations(bracket_views)

        # Filter to only tradeable brackets (positive edge above threshold)
        tradeable = []
        for bv in bracket_views:
            if bv.role == BracketRole.SKIP:
                continue
            min_edge = self.MIN_YES_EDGE if bv.side == "yes" else self.MIN_NO_EDGE
            if bv.edge >= min_edge:
                tradeable.append(bv)

        portfolio.brackets = tradeable
        portfolio.total_edge = sum(bv.edge for bv in tradeable if bv.edge > 0)

        htc_str = f"{hours_to_close:.1f}h" if hours_to_close is not None else "N/A"
        logger.info(
            f"Portfolio built: {city} {market_date} | "
            f"forecast={forecast_temp:.0f}F | std_dev={std_dev:.2f}F | "
            f"lead_days={lead_days} | confidence={confidence} | "
            f"hours_to_close={htc_str} | "
            f"tradeable_brackets={len(tradeable)}"
        )

        return portfolio

    def classify_bracket(
        self,
        bracket_lower: float,
        bracket_upper: float,
        forecast: float,
    ) -> BracketRole:
        """
        Classify a bracket based on distance from forecast.

        Distance thresholds:
        - 0° (forecast is IN this bracket): FORECAST_YES
        - ±2° (adjacent): NEAR_YES_HEDGE candidate (resolved later by hedge direction)
        - ±3°: MEDIUM_NO
        - ±4-5°: FAR_NO
        - ±6°+: VERY_FAR_NO
        """
        # Check if forecast is inside the bracket
        if bracket_lower <= forecast < bracket_upper:
            return BracketRole.FORECAST_YES

        # Signed distance: midpoint of bracket vs forecast
        bracket_mid = (bracket_lower + bracket_upper) / 2.0
        abs_distance = abs(forecast - bracket_mid)

        if abs_distance <= 2.0:
            # Adjacent — candidate for near-YES hedge (resolved later)
            return BracketRole.NEAR_YES_HEDGE
        elif abs_distance <= 3.5:
            return BracketRole.MEDIUM_NO
        elif abs_distance <= 5.5:
            return BracketRole.FAR_NO
        else:
            return BracketRole.VERY_FAR_NO

    def determine_hedge_direction(
        self,
        forecast: float,
        forecast_bracket_lower: float,
        forecast_bracket_upper: float,
    ) -> str:
        """
        Determine which adjacent bracket gets the near-YES hedge.

        Based on where the forecast sits within its bracket:
        - Bottom of bracket → hedge downward (protects against undershoot)
        - Top of bracket → hedge upward (protects against overshoot)
        - Middle → pick better-priced adjacent

        Returns:
            "below", "above", or "best_priced"
        """
        bracket_width = forecast_bracket_upper - forecast_bracket_lower
        if bracket_width <= 0:
            return "best_priced"

        relative_position = (forecast - forecast_bracket_lower) / bracket_width

        if relative_position < 0.4:
            return "below"
        elif relative_position > 0.6:
            return "above"
        else:
            return "best_priced"

    def get_entry_order(self, portfolio: CityBracketPortfolio) -> List[BracketView]:
        """
        Return brackets in execution order (outside-in).

        Order:
        1. Very far NOs (highest confidence, execute first)
        2. Far NOs
        3. Medium NOs
        4. Forecast YES bracket
        5. Near-YES hedge bracket
        """
        role_priority = {
            BracketRole.VERY_FAR_NO: 0,
            BracketRole.FAR_NO: 1,
            BracketRole.MEDIUM_NO: 2,
            BracketRole.FORECAST_YES: 3,
            BracketRole.NEAR_YES_HEDGE: 4,
            BracketRole.SKIP: 99,
        }

        ordered = sorted(
            portfolio.brackets,
            key=lambda bv: (
                role_priority.get(bv.role, 50),
                -abs(bv.distance_from_forecast),  # Within same role, furthest first
            ),
        )
        return ordered

    def calculate_city_budget(
        self,
        city: str,
        total_available: float,
        all_portfolios: List[CityBracketPortfolio],
    ) -> float:
        """
        Allocate budget to this city based on edge quality.

        city_budget = (city_total_edge / sum_all_cities_edge) × available_capital
        Minimum 10% per city to avoid starving any portfolio.
        """
        total_edge = sum(p.total_edge for p in all_portfolios if p.total_edge > 0)
        if total_edge <= 0:
            # Equal split if no edge data
            active_count = len([p for p in all_portfolios if p.brackets])
            if active_count == 0:
                return 0.0
            return total_available / active_count

        city_portfolio = next(
            (p for p in all_portfolios if p.city == city and p.total_edge > 0),
            None,
        )
        if city_portfolio is None:
            return 0.0

        raw_pct = city_portfolio.total_edge / total_edge
        # Floor at 10% per city so no portfolio is starved
        min_pct = 0.10
        n_active = len([p for p in all_portfolios if p.total_edge > 0])
        if n_active > 0:
            min_pct = min(min_pct, 1.0 / n_active)

        allocated_pct = max(min_pct, raw_pct)
        return total_available * allocated_pct

    def check_reshape_needed(
        self,
        portfolio: CityBracketPortfolio,
        new_forecast: float,
    ) -> bool:
        """
        Check if portfolio needs reshaping based on forecast change.

        CONSERVATIVE — only reshape on large, unambiguous shifts:
        1. old_bracket != new_bracket AND shift >= 4°F
        2. OR distance from any YES position to new forecast > 5°F
        3. AND reshape_count < 2 (max 2 per city per day)
        """
        if portfolio.reshape_count >= 2:
            return False

        old_forecast = portfolio.last_forecast_temp
        shift = abs(new_forecast - old_forecast)

        # Check if forecast moved to a different bracket
        old_bracket = self._get_bracket_center(old_forecast)
        new_bracket = self._get_bracket_center(new_forecast)

        if old_bracket != new_bracket and shift >= 4.0:
            return True

        # Check if any YES positions are now dangerously far from forecast
        for bv in portfolio.brackets:
            if bv.side == "yes":
                bracket_mid = (bv.bracket_lower + bv.bracket_upper) / 2.0
                distance = abs(new_forecast - bracket_mid)
                if distance > 5.0:
                    return True

        return False

    # ── Private helpers ───────────────────────────────────────────────

    def _calc_distance(
        self, forecast: float, bracket_lower: float, bracket_upper: float
    ) -> float:
        """Calculate signed distance from forecast to bracket midpoint."""
        bracket_mid = (bracket_lower + bracket_upper) / 2.0
        return bracket_mid - forecast

    def _calc_bracket_probability(
        self,
        forecast: float,
        bracket_lower: float,
        bracket_upper: float,
        std_dev: Optional[float] = None,
    ) -> float:
        """
        Calculate P(temp falls in bracket) using normal distribution.

        Args:
            forecast: NWS forecast temperature (°F)
            bracket_lower: Lower bound of bracket (°F)
            bracket_upper: Upper bound of bracket (°F)
            std_dev: Forecast uncertainty. If None, uses 2.5 (day-1 medium).

        Uses shared norm_cdf from probability_utils.
        """
        from src.utils.probability_utils import norm_cdf

        if std_dev is None:
            std_dev = 2.5  # Fallback: day-1 medium confidence

        prob = norm_cdf(bracket_upper, loc=forecast, scale=std_dev) - \
               norm_cdf(bracket_lower, loc=forecast, scale=std_dev)
        return max(0.0, min(1.0, prob))

    def _assign_hedge(
        self,
        bracket_views: List[BracketView],
        forecast: float,
        forecast_bracket_lower: float,
        forecast_bracket_upper: float,
    ) -> None:
        """
        Resolve near-YES hedge: pick ONE adjacent bracket, demote others to SKIP.

        Uses determine_hedge_direction() to decide above/below, then picks
        the best candidate from NEAR_YES_HEDGE-classified brackets.
        """
        hedge_dir = self.determine_hedge_direction(
            forecast, forecast_bracket_lower, forecast_bracket_upper,
        )

        near_yes_candidates = [
            bv for bv in bracket_views if bv.role == BracketRole.NEAR_YES_HEDGE
        ]
        if not near_yes_candidates:
            return

        above_candidates = [
            bv for bv in near_yes_candidates if bv.distance_from_forecast > 0
        ]
        below_candidates = [
            bv for bv in near_yes_candidates if bv.distance_from_forecast < 0
        ]

        chosen = None
        if hedge_dir == "above" and above_candidates:
            # Pick the closest above bracket
            chosen = min(above_candidates, key=lambda bv: bv.distance_from_forecast)
        elif hedge_dir == "below" and below_candidates:
            # Pick the closest below bracket (least negative distance)
            chosen = max(below_candidates, key=lambda bv: bv.distance_from_forecast)
        else:
            # "best_priced" or no candidates in preferred direction:
            # pick the one with the best edge
            chosen = max(near_yes_candidates, key=lambda bv: bv.edge)

        # Mark chosen as NEAR_YES_HEDGE, demote others to MEDIUM_NO
        for bv in near_yes_candidates:
            if bv is chosen:
                bv.role = BracketRole.NEAR_YES_HEDGE
            else:
                # Reclassify as MEDIUM_NO (they're close enough to be medium NOs)
                bv.role = BracketRole.MEDIUM_NO
                bv.side = "no"
                no_prob = 1.0 - bv.our_probability
                bv.edge = no_prob - bv.market_no_price if bv.market_no_price > 0 else 0.0

    def _set_allocations(self, bracket_views: List[BracketView]) -> None:
        """Set recommended allocation percentages based on role."""
        # Count brackets per role to split allocation within each role group
        role_counts: Dict[BracketRole, int] = {}
        for bv in bracket_views:
            if bv.role == BracketRole.SKIP:
                continue
            role_counts[bv.role] = role_counts.get(bv.role, 0) + 1

        for bv in bracket_views:
            if bv.role == BracketRole.SKIP:
                bv.recommended_allocation_pct = 0.0
                continue

            # Get the allocation pool for this role
            if bv.role == BracketRole.VERY_FAR_NO:
                pool = self.FAR_NO_PCT  # Very far shares the far-NO pool
            elif bv.role == BracketRole.FAR_NO:
                pool = self.FAR_NO_PCT
            elif bv.role == BracketRole.MEDIUM_NO:
                pool = self.MEDIUM_NO_PCT
            elif bv.role == BracketRole.FORECAST_YES:
                pool = self.FORECAST_YES_PCT
            elif bv.role == BracketRole.NEAR_YES_HEDGE:
                pool = self.NEAR_YES_PCT
            else:
                pool = 0.0

            # Split pool evenly among brackets in the same role
            # For far NOs and very far NOs, share the same 50% pool
            if bv.role in (BracketRole.FAR_NO, BracketRole.VERY_FAR_NO):
                count = (
                    role_counts.get(BracketRole.FAR_NO, 0)
                    + role_counts.get(BracketRole.VERY_FAR_NO, 0)
                )
            else:
                count = role_counts.get(bv.role, 1)

            bv.recommended_allocation_pct = pool / max(1, count)

    def _get_bracket_center(self, temp: float) -> float:
        """Get the center of the 2°F bracket that contains this temperature.

        Brackets are aligned on even integers: ...[34, 36), [36, 38), [38, 40)...
        """
        # Floor to nearest even integer
        base = int(temp) if int(temp) % 2 == 0 else int(temp) - 1
        return float(base + 1)  # Center of [base, base+2)


# ── Singleton ─────────────────────────────────────────────────────────

_manager: Optional[CityPortfolioManager] = None


def get_city_portfolio_manager() -> CityPortfolioManager:
    """Get or create the global CityPortfolioManager instance."""
    global _manager
    if _manager is None:
        _manager = CityPortfolioManager()
    return _manager
