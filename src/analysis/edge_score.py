"""
EdgeScore calculator: scores weather opportunities for ranking.

Consolidates scoring logic that was previously inline in main.py's
_score_opportunity method. Adds:
- Time discount: decay edge for further-out markets
- Fee adjustment: half-Kelly with Kalshi 2% winner fee
- Uncertainty buffer: reduce score when NWS confidence is low
- Liquidity bonus: prefer mid-priced markets with tighter spreads
- Trade type bonus: bracket-squeeze type awareness
"""

import os
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional

from src.utils.fees import KALSHI_WINNER_FEE_RATE


# ── Configurable thresholds ──────────────────────────────────────────

# Uncertainty buffer: how much to penalize low-confidence forecasts
# Higher = more conservative
UNCERTAINTY_STD_DEV = {
    "high": 2.0,      # NWS high confidence → tight distribution
    "medium": 3.5,    # Medium → wider
    "low": 5.5,       # Low → widest
}

# Time discount per day out
TIME_DISCOUNT_PER_DAY = float(os.environ.get("EDGE_TIME_DISCOUNT_PER_DAY", "0.10"))

# Max entry price (cents) — pre-filter in edge scoring
# Main.py safety gates enforce tighter per-side limits:
#   YES: 5-25¢ | NO: up to 65¢ (or 98¢ if observation-settled)
# This is a broad pre-filter; side-specific limits are in main.py
MAX_ENTRY_CENTS = int(os.environ.get("EDGE_MAX_ENTRY_CENTS", "96"))

# Observation-settled NO trades (temp already passed the bracket) can enter
# at higher prices since the outcome is known. Requires observation_settled=True
# on the opportunity AND the bracket must be >= MIN_OBS_SETTLED_DISTANCE_F from forecast.
MAX_OBS_SETTLED_ENTRY_CENTS = int(os.environ.get("EDGE_MAX_OBS_SETTLED_CENTS", "98"))
MIN_OBS_SETTLED_DISTANCE_F = float(os.environ.get("EDGE_MIN_OBS_SETTLED_DIST", "2.0"))


@dataclass
class EdgeScoreResult:
    """Result of scoring a weather opportunity."""

    side: str
    entry_price: float        # 0-1
    price_cents: int
    our_prob: float           # probability for our side
    half_kelly: float
    kelly_f: float
    net_payout: float
    ev_per_dollar: float
    confidence_mult: float
    time_discount: float
    uncertainty_buffer: float
    liquidity_bonus: float
    trade_type_bonus: float
    score: float
    days_out: int

    def to_dict(self) -> dict:
        """Convert to dict compatible with legacy _score_opportunity output."""
        return {
            "side": self.side,
            "entry_price": self.entry_price,
            "price_cents": self.price_cents,
            "our_prob": self.our_prob,
            "half_kelly": self.half_kelly,
            "kelly_f": self.kelly_f,
            "net_payout": self.net_payout,
            "ev_per_dollar": self.ev_per_dollar,
            "confidence_mult": self.confidence_mult,
            "time_bonus": self.time_discount,       # alias for compatibility
            "time_discount": self.time_discount,
            "uncertainty_buffer": self.uncertainty_buffer,
            "liquidity_bonus": self.liquidity_bonus,
            "trade_type_bonus": self.trade_type_bonus,
            "score": self.score,
            "days_out": self.days_out,
        }


class EdgeScoreCalculator:
    """
    Scores weather trading opportunities for ranking.

    Composite score formula:
        score = ev_per_dollar * confidence * time_discount * uncertainty
                * liquidity * trade_type

    Components:
    - ev_per_dollar:    half_kelly * directional_edge
    - confidence:       NWS confidence → {high:1.0, medium:0.85, low:0.70}
    - time_discount:    1.2 today, 1.0 tomorrow, decays 10%/day thereafter
    - uncertainty:      1.0 - (std_dev / 20) — higher std_dev = more penalty
    - liquidity:        prefer 20c-80c (1.0), penalize extremes
    - trade_type:       bonus for reliable no_exclusion, time-aware yes_convergence
    """

    CONFIDENCE_MAP = {"high": 1.0, "medium": 0.85, "low": 0.70}

    def score(self, opp) -> Optional[EdgeScoreResult]:
        """
        Score a weather opportunity.

        Args:
            opp: WeatherOpportunity with ticker, recommendation, market_price,
                 our_probability, edge, nws_confidence, market_date, etc.

        Returns:
            EdgeScoreResult or None if opportunity should be filtered.
        """
        # --- Determine side and entry price ---
        if opp.recommendation == "BUY_YES":
            side = "yes"
            entry_price = opp.market_price
        elif opp.recommendation == "BUY_NO":
            side = "no"
            entry_price = 1.0 - opp.market_price
        else:
            return None

        price_cents = int(entry_price * 100)
        if price_cents <= 0 or price_cents >= 100:
            return None

        # --- Entry price ceiling with observation-settled carve-out ---
        # High-price NO entries (>65¢) are only allowed when:
        #   1. Live observation has confirmed the outcome (bracket dead / settled)
        #   2. The bracket is far enough from forecast (≥3°F) to be genuinely risk-free
        # This prevents buying NO @98¢ on a bracket right at the forecast
        if price_cents > MAX_ENTRY_CENTS:
            is_obs_settled = getattr(opp, "observation_settled", False)
            forecast_distance = abs(
                getattr(opp, "threshold_temp", 0) - getattr(opp, "nws_forecast_temp", 0)
            )
            if is_obs_settled and forecast_distance >= MIN_OBS_SETTLED_DISTANCE_F:
                # Genuinely risk-free: observation confirmed + far from forecast
                if price_cents > MAX_OBS_SETTLED_ENTRY_CENTS:
                    return None  # Even settled plays have a ceiling
            else:
                return None  # Not settled or too close to forecast — blocked

        # --- Half-Kelly with fee adjustment ---
        our_prob = opp.our_probability if side == "yes" else (1.0 - opp.our_probability)
        payout_if_win = 1.0 - entry_price
        fee = KALSHI_WINNER_FEE_RATE * payout_if_win
        net_payout = payout_if_win - fee
        if net_payout <= 0:
            return None

        kelly_f = (our_prob * net_payout - (1.0 - our_prob) * entry_price) / net_payout
        half_kelly = max(0.0, kelly_f / 2)
        if half_kelly <= 0:
            return None

        ev_per_dollar = half_kelly * opp.edge

        # --- Confidence multiplier ---
        confidence_mult = self.CONFIDENCE_MAP.get(opp.nws_confidence, 0.70)

        # --- Time discount ---
        days_out = (opp.market_date - date.today()).days
        time_discount = self._calc_time_discount(days_out)

        # --- Uncertainty buffer ---
        # Higher NWS uncertainty → wider forecast std dev → less trust in edge
        uncertainty_buffer = self._calc_uncertainty_buffer(opp.nws_confidence)

        # --- Liquidity bonus ---
        liquidity_bonus = self._calc_liquidity_bonus(opp.market_price)

        # --- Trade type bonus ---
        trade_type_bonus = self._calc_trade_type_bonus(opp, our_prob, days_out)

        # --- Composite score ---
        score = (
            ev_per_dollar
            * confidence_mult
            * time_discount
            * uncertainty_buffer
            * liquidity_bonus
            * trade_type_bonus
        )

        return EdgeScoreResult(
            side=side,
            entry_price=entry_price,
            price_cents=price_cents,
            our_prob=our_prob,
            half_kelly=half_kelly,
            kelly_f=kelly_f,
            net_payout=net_payout,
            ev_per_dollar=ev_per_dollar,
            confidence_mult=confidence_mult,
            time_discount=time_discount,
            uncertainty_buffer=uncertainty_buffer,
            liquidity_bonus=liquidity_bonus,
            trade_type_bonus=trade_type_bonus,
            score=score,
            days_out=days_out,
        )

    def _calc_time_discount(self, days_out: int) -> float:
        """Time discount: today=1.2, tomorrow=1.0, decays further out."""
        if days_out <= 0:
            return 1.2
        elif days_out == 1:
            return 1.0
        else:
            return max(0.3, 1.0 - TIME_DISCOUNT_PER_DAY * (days_out - 1))

    def _calc_uncertainty_buffer(self, nws_confidence: str) -> float:
        """
        Reduce score when forecast uncertainty is high.

        Maps NWS confidence to an assumed std dev, then converts to a
        multiplier. A high-confidence forecast (2F std dev) gets ~0.90,
        while a low-confidence forecast (5.5F std dev) gets ~0.725.
        """
        std_dev = UNCERTAINTY_STD_DEV.get(nws_confidence, 5.5)
        # Buffer = 1.0 - (std_dev / 20): higher uncertainty = lower score
        return max(0.5, 1.0 - std_dev / 20.0)

    def _calc_liquidity_bonus(self, market_price: float) -> float:
        """Prefer mid-priced markets with tighter spreads."""
        mid_distance = abs(market_price - 0.50)
        if mid_distance < 0.30:
            return 1.0      # 20c-80c: liquid
        elif mid_distance < 0.40:
            return 0.7      # 10c-90c: less liquid
        else:
            return 0.4      # <10c or >90c: illiquid

    def _calc_trade_type_bonus(self, opp, our_prob: float, days_out: int) -> float:
        """Bonus for bracket-squeeze trade types."""
        trade_type = getattr(opp, "trade_type", "unknown")
        if trade_type == "no_exclusion" and our_prob > 0.90:
            return 1.3  # Reliable income stream
        elif trade_type == "yes_convergence" and days_out == 0:
            try:
                from src.strategy.weather_strategy import WeatherStrategy
                from src.data_sources.nws_weather import KALSHI_STATIONS
                from zoneinfo import ZoneInfo

                nws_city = WeatherStrategy.CITY_CODES.get(opp.city, opp.city)
                tz_name = KALSHI_STATIONS.get(nws_city, {}).get(
                    "timezone", "America/New_York"
                )
                local_hour = datetime.now(ZoneInfo(tz_name)).hour
                if local_hour < 10:
                    return 1.2  # Morning: best convergence window
                elif local_hour < 14:
                    return 1.0  # Midday: decent
                else:
                    return 0.3  # Afternoon: market already efficient
            except Exception:
                return 1.0
        return 1.0


# ── Singleton ────────────────────────────────────────────────────────

_calculator: Optional[EdgeScoreCalculator] = None


def get_edge_score_calculator() -> EdgeScoreCalculator:
    """Get or create the global EdgeScoreCalculator instance."""
    global _calculator
    if _calculator is None:
        _calculator = EdgeScoreCalculator()
    return _calculator
