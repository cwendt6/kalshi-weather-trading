"""
Position re-evaluator: deterministic, model-driven exit decision engine.

Runs every cycle, checks all open positions against mathematically-derived
exit criteria. All decisions are deterministic — no LLM signals.

Exit hierarchy:
1. Hard safety stop-loss (catastrophic protection)
2. Edge flipped negative (model disagrees with position)
3. Edge eroded below transaction-cost floor (edge gone)
4. Smart take-profit: edge decaying + momentum reversing + in profit
5. Time-based force exit (48h with unknown edge)

Mathematical framework:
  - At any moment: EV(hold) = model_prob × $1.00
  - EV(sell now) = current_market_price
  - Hold if model_prob > market_price + fee_threshold (edge remains)
  - Sell if model_prob < market_price (edge gone or flipped)
  - Smart take-profit: when edge has decayed >50% from peak AND price
    momentum has reversed >30% of favorable move. These thresholds are
    derived from transaction costs and mean-reversion statistics.

Threshold derivations:
  - MIN_EDGE_TO_HOLD = 3%: Kalshi 2% winner fee + ~1% bid-ask spread
  - EDGE_DECAY_TRIGGER = 50%: edge at less than half its peak → information
    advantage is dissolving faster than the market can price it in
  - MOMENTUM_RETRACEMENT = 30%: price retraced 30% of favorable move,
    statistically indicates trend exhaustion (beyond noise, before capitulation)
  - HARD_STOP_LOSS = 50%: catastrophic safety net for model failures
"""
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple

from sqlalchemy import func

from src.data.database import get_db_session
from src.data.models import MarketDB, PositionDB, PriceDB, TradeDB
from src.utils.fees import KALSHI_WINNER_FEE_RATE
from src.utils.logging import logger



_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


def parse_ticker_date(ticker: str) -> date | None:
    """Market date from a Kalshi ticker. Format is YYMMMDD: KXHIGHNY-26FEB12-T32 is Feb 12, 2026."""
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})-", ticker)
    if not m:
        return None
    yr_str, mon_str, day_str = m.groups()
    month = _MONTHS.get(mon_str)
    if month is None:
        return None
    try:
        return date(2000 + int(yr_str), month, int(day_str))
    except ValueError:
        return None

class ExitReason(Enum):
    """Why a position is being exited."""

    # Legacy reasons (kept for backwards compatibility with DB records)
    PROFIT_TARGET_TIER1 = "profit_target_tier1"
    PROFIT_TARGET_TIER2 = "profit_target_tier2"
    PROFIT_TARGET_TIER3 = "profit_target_tier3"
    LLM_SIGNAL = "llm_signal"

    # Active reasons
    STOP_LOSS = "stop_loss"                    # Hard safety stop (catastrophic protection)
    TIME_DECAY_24H = "time_decay_24h"          # 24h+ with weak edge
    TIME_DECAY_48H = "time_decay_48h"          # 48h+ force exit
    EDGE_ERODED = "edge_eroded"                # Edge dropped below hold threshold (3%)
    EDGE_FLIPPED = "edge_flipped"              # Edge went negative (model disagrees)
    EDGE_DECAY_MOMENTUM = "edge_decay_momentum"  # Edge decaying + momentum reversed (smart take-profit)
    MOMENTUM_REVERSAL = "momentum_reversal"    # Price reversed significantly (no model fallback)
    MOMENTUM_STALL = "momentum_stall"          # Price stalled (no model fallback)

    # Weather-specific exit reasons
    WEATHER_YES_TAKE_PROFIT = "weather_yes_take_profit"      # YES convergence profit-taking
    WEATHER_YES_EDGE_GONE = "weather_yes_edge_gone"          # Market converged to model
    WEATHER_YES_HARD_EXIT = "weather_yes_hard_exit"          # Force exit before resolution
    WEATHER_NO_FORECAST_SHIFT = "weather_no_forecast_shift"  # Forecast shifted toward bracket

    # Phase 2: Dynamic YES cost-basis exits
    WEATHER_YES_COST_BASIS = "weather_yes_cost_basis"         # Stage 1: sold to cover cost
    WEATHER_YES_PURE_PROFIT = "weather_yes_pure_profit"       # Stage 2: pure profit take
    WEATHER_YES_REMAINDER_EXIT = "weather_yes_remainder_exit" # Stage 3: time/forecast exit

    # Capital efficiency
    OPPORTUNITY_COST = "opportunity_cost"  # Better opportunity available elsewhere


@dataclass
class ExitDecision:
    """Decision to exit a position (or not)."""

    ticker: str
    side: str  # "yes" or "no"
    quantity: int  # How many contracts to exit (may be partial)
    should_exit: bool  # If False, position continues to be held

    reason: Optional[ExitReason] = None

    # Financial details
    entry_price_cents: Optional[int] = None
    current_price_cents: Optional[int] = None
    unrealized_pnl_dollars: Optional[float] = None
    unrealized_pnl_pct: Optional[float] = None
    net_profit_after_fees: Optional[float] = None

    # Model details
    model_edge: Optional[float] = None  # Current edge estimate

    # Timing
    hours_held: Optional[float] = None

    # Reasoning
    reasoning: str = ""


class PositionReEvaluator:
    """
    Deterministic, model-driven position re-evaluator.

    Runs every cycle, checks all open positions using the weather forecaster
    model and price momentum data. All decisions are mathematically derived —
    no LLM or non-deterministic signals.
    """

    # ─── Mathematically derived thresholds ────────────────────────────

    # Minimum edge to continue holding.
    # Derived from: Kalshi 2% winner fee + ~1% typical bid-ask spread = 3%.
    # Below this, the expected profit from holding doesn't cover transaction costs.
    MIN_EDGE_TO_HOLD = float(os.getenv("EXIT_MIN_EDGE_TO_HOLD", "0.03"))

    # Hard safety stop-loss as percentage of entry price.
    # Catastrophic protection for model failures. Should rarely trigger
    # because edge-based exits catch most losses before this.
    HARD_STOP_LOSS_PCT = float(os.getenv("EXIT_HARD_STOP_LOSS_PCT", "0.50"))

    # Edge decay trigger: take profit when current edge < this fraction of peak edge.
    # At 0.50, this means "edge has dropped to less than half its peak" — our
    # information advantage is dissolving. Combined with momentum reversal,
    # this signals "take profits while the market still offers a good price."
    EDGE_DECAY_TRIGGER = float(os.getenv("EXIT_EDGE_DECAY_TRIGGER", "0.50"))

    # Momentum retracement threshold: price retraced this fraction of the
    # favorable move from entry to peak. 0.30 = 30% pullback.
    # Statistically, a 30% retracement indicates trend exhaustion beyond
    # random noise (which is typically <15% of the move).
    MOMENTUM_RETRACEMENT_PCT = float(os.getenv("EXIT_MOMENTUM_RETRACEMENT_PCT", "0.30"))

    # Time decay rules
    FORCE_EXIT_HOURS = int(os.getenv("EXIT_FORCE_HOURS", "48"))

    # Edge cache: re-forecast interval in seconds (avoid hammering weather APIs)
    EDGE_CACHE_TTL_SECONDS = int(os.getenv("EXIT_EDGE_CACHE_TTL", "120"))  # 2 minutes

    # Grace period: don't exit positions less than this many minutes old
    MIN_HOLD_MINUTES = int(os.getenv("EXIT_MIN_HOLD_MINUTES", "10"))

    # Exit failure cooldown: after N consecutive failures, stop retrying
    # for a cooldown period to avoid log spam and wasted API calls.
    EXIT_FAILURE_MAX_RETRIES = 3        # attempts before cooldown
    EXIT_FAILURE_COOLDOWN_MINUTES = 10  # minutes to wait after max retries

    # ─── Weather-specific thresholds (bracket squeeze exits) ───────────
    WEATHER_YES_TP1_CENTS = int(os.getenv("WEATHER_YES_TP1_CENTS", "5"))
    WEATHER_YES_TP2_CENTS = int(os.getenv("WEATHER_YES_TP2_CENTS", "10"))
    WEATHER_YES_TP3_CENTS = int(os.getenv("WEATHER_YES_TP3_CENTS", "15"))
    WEATHER_YES_TP1_FRACTION = float(os.getenv("WEATHER_YES_TP1_FRACTION", "0.25"))
    WEATHER_YES_TP2_FRACTION = float(os.getenv("WEATHER_YES_TP2_FRACTION", "0.50"))
    WEATHER_YES_HARD_EXIT_HOURS = float(os.getenv("WEATHER_YES_HARD_EXIT_HOURS", "2.0"))
    WEATHER_NO_TRIM_DISTANCE_F = float(os.getenv("WEATHER_NO_TRIM_DISTANCE_F", "2.0"))
    WEATHER_DIVERGENCE_TIMEOUT_HOURS = float(os.getenv("WEATHER_DIVERGENCE_TIMEOUT_HOURS", "2.5"))
    WEATHER_HARD_DROP_PCT = float(os.getenv("WEATHER_HARD_DROP_PCT", "0.30"))
    WEATHER_NO_FREE_CAPITAL_THRESHOLD = float(os.getenv("WEATHER_NO_FREE_CAPITAL_THRESHOLD", "0.90"))

    # Phase 2: Dynamic YES cost-basis exit thresholds
    WEATHER_YES_STAGE1_MIN_MULT = float(os.getenv("WEATHER_YES_STAGE1_MIN_MULT", "1.5"))
    WEATHER_YES_STAGE2_MULT = float(os.getenv("WEATHER_YES_STAGE2_MULT", "3.0"))
    WEATHER_YES_STAGE2_SELL_FRAC = float(os.getenv("WEATHER_YES_STAGE2_SELL_FRAC", "0.50"))
    WEATHER_YES_MIN_EDGE_STAGE2 = float(os.getenv("WEATHER_YES_MIN_EDGE_S2", "0.02"))

    def __init__(self, paper_trading: bool = True, resting_order_manager=None) -> None:
        """Initialize re-evaluator."""
        self.paper_trading = paper_trading
        self.resting_order_manager = resting_order_manager  # Optional RestingOrderManager

        # Track partial exits (ticker -> total contracts already exited by this engine)
        self._partial_exits: Dict[str, int] = {}

        # Edge cache: "ticker:side" -> (edge_value, timestamp)
        self._edge_cache: Dict[str, Tuple[Optional[float], datetime]] = {}

        # Edge history: "ticker:side" -> list of (edge, timestamp) readings
        # Used to compute peak edge and edge velocity
        self._edge_history: Dict[str, List[Tuple[float, datetime]]] = {}

        # Exit failure tracking: ticker -> (consecutive_failures, last_failure_time)
        self._exit_failures: Dict[str, Tuple[int, datetime]] = {}

        # Dead markets: tickers where market has no bid and ask ≤ 2¢.
        # These are unsellable — skip re-evaluation and let settlement handle them.
        self._dead_markets: Set[str] = set()

        # Phase 2: Track which exit stage has completed per ticker (0=none, 1=cost basis, 2=profit take)
        self._exit_stages: Dict[str, int] = {}

        # Weather: strategy cache (ticker -> strategy string from TradeDB)
        self._strategy_cache: Dict[str, Optional[str]] = {}

        # Weather: divergence timeout tracking (ticker -> first_divergence_time)
        self._divergence_start: Dict[str, datetime] = {}

        # Opportunity cost exit: available opportunities from the latest weather scan.
        # Set by main.py before each re-evaluation cycle via set_available_opportunities().
        self._available_opportunities: List[Any] = []

        logger.info(
            f"PositionReEvaluator initialized | deterministic model-based exits | "
            f"min_edge_hold={self.MIN_EDGE_TO_HOLD:.0%} | "
            f"edge_decay_trigger={self.EDGE_DECAY_TRIGGER:.0%} | "
            f"momentum_retrace={self.MOMENTUM_RETRACEMENT_PCT:.0%} | "
            f"hard_stop={self.HARD_STOP_LOSS_PCT:.0%} | "
            f"force_exit={self.FORCE_EXIT_HOURS}h | "
            f"resting_orders={'enabled' if resting_order_manager else 'disabled'} | "
            f"paper={paper_trading}"
        )

    def set_available_opportunities(self, opportunities: List[Any]) -> None:
        """
        Set the latest available weather trading opportunities.

        Called by main.py before each re-evaluation cycle so the opportunity
        cost check can compare held positions against what's available.
        """
        self._available_opportunities = opportunities

    def _check_opportunity_cost(
        self, ticker: str, side: str, quantity: int,
        entry_price: int, current_price: int,
        current_edge: Optional[float],
        pnl_cents: float, pnl_dollars: float, pnl_pct: float,
        hours_held: float,
    ) -> Optional[ExitDecision]:
        """
        Exit if capital could be deployed more profitably elsewhere.

        Rule: If best available opportunity has edge > 1.5x current position's edge,
        AND current position is profitable (don't sell losers just to chase),
        AND best available edge > 8% (only reallocate for strong opportunities),
        recommend full exit to free capital.

        This is the LOWEST priority exit trigger — only fires after all safety
        and strategy-specific exits have been checked.
        """
        if not self._available_opportunities:
            return None

        # Only trigger if position is in profit — never sell losers to chase
        if pnl_cents <= 0:
            return None

        # Need a current edge estimate for comparison
        if current_edge is None or current_edge <= 0:
            return None

        # Find best available edge among opportunities (excluding this ticker)
        best_edge = 0.0
        best_ticker = ""
        for opp in self._available_opportunities:
            opp_edge = getattr(opp, "edge", 0.0)
            opp_ticker = getattr(opp, "ticker", "")
            if opp_ticker != ticker and opp_edge > best_edge:
                best_edge = opp_edge
                best_ticker = opp_ticker

        # Thresholds
        EDGE_MULTIPLIER = 1.5  # Best available must be 1.5x current
        MIN_BEST_EDGE = 0.08   # Only reallocate for strong (8%+) opportunities

        if best_edge > current_edge * EDGE_MULTIPLIER and best_edge > MIN_BEST_EDGE:
            net_profit = self._calculate_net_profit(side, quantity, entry_price, current_price)
            return ExitDecision(
                ticker=ticker, side=side, quantity=quantity, should_exit=True,
                reason=ExitReason.OPPORTUNITY_COST,
                entry_price_cents=entry_price, current_price_cents=current_price,
                unrealized_pnl_dollars=pnl_dollars, unrealized_pnl_pct=pnl_pct,
                net_profit_after_fees=net_profit, model_edge=current_edge,
                hours_held=hours_held,
                reasoning=(
                    f"Opportunity cost: position edge={current_edge:.1%}, "
                    f"best available={best_edge:.1%} ({best_ticker}) "
                    f"— {best_edge/current_edge:.1f}x better, freeing capital"
                ),
            )

        return None

    def evaluate_all_positions(self, skip_tickers: Optional[set] = None) -> List[ExitDecision]:
        """
        Evaluate all open positions, return list of exit decisions.

        Args:
            skip_tickers: Set of ticker prefixes to skip (e.g., crypto tickers
                managed by CryptoExitEngine).

        Returns:
            List of ExitDecision objects where should_exit=True.
        """
        decisions: List[ExitDecision] = []

        with next(get_db_session()) as session:
            # Get all open positions
            positions = session.query(PositionDB).filter(
                PositionDB.quantity > 0
            ).all()

            # Extract data within session to avoid DetachedInstanceError
            position_data = []
            for pos in positions:
                ticker = str(pos.ticker)

                # Skip tickers managed by other exit engines (e.g., crypto)
                if skip_tickers and any(ticker.startswith(p) for p in skip_tickers):
                    continue

                # Skip dead markets (no bid, ask ≤ 2¢) — will settle automatically
                if ticker in self._dead_markets:
                    continue

                # Get current price within session
                current_price = self._get_current_price(session, ticker)

                position_data.append({
                    "ticker": ticker,
                    "side": str(pos.side or "unknown"),
                    "quantity": int(pos.quantity or 0),
                    "entry_price": int(pos.average_price or 0),
                    "created_at": pos.created_at,
                    "current_price": current_price,
                })

        # Evaluate each position outside the session
        for pos_data in position_data:
            if pos_data["quantity"] <= 0 or pos_data["entry_price"] <= 0:
                continue

            decision = self._evaluate_position(pos_data)
            if decision.should_exit:
                decisions.append(decision)

        if decisions:
            logger.info(
                f"Position re-evaluator: {len(decisions)} exits recommended",
                exits=[f"{d.ticker} ({d.reason.value})" for d in decisions],
            )

        return decisions

    def _get_position_strategy(self, ticker: str) -> Optional[str]:
        """Look up the entry strategy for a position from TradeDB."""
        if ticker in self._strategy_cache:
            return self._strategy_cache[ticker]
        try:
            with next(get_db_session()) as session:
                trade = session.query(TradeDB).filter(
                    TradeDB.ticker == ticker,
                    TradeDB.action == "buy",
                ).order_by(TradeDB.timestamp.desc()).first()
                strategy = str(trade.strategy) if trade and trade.strategy else None
                self._strategy_cache[ticker] = strategy
                return strategy
        except Exception:
            return None

    def _is_weather_position(self, ticker: str) -> bool:
        """Check if a position is a weather trade (any weather_* strategy)."""
        strategy = self._get_position_strategy(ticker)
        return strategy is not None and strategy.startswith("weather")

    def _get_forecast_market_trust(self, city: str) -> Tuple[float, float]:
        """
        Sliding trust model: compute forecast vs market weight by time of day.

        Returns (forecast_weight, market_weight) summing to 1.0.
        Morning (before 10am local): 80% forecast, 20% market
        Midday (10am-2pm local):     50% / 50%
        Afternoon (after 2pm local): 20% forecast, 80% market
        """
        try:
            from zoneinfo import ZoneInfo
            from src.data_sources.nws_weather import KALSHI_STATIONS
            tz_name = KALSHI_STATIONS.get(city, {}).get("timezone", "America/New_York")
            local_hour = datetime.now(ZoneInfo(tz_name)).hour
        except Exception:
            local_hour = 12

        if local_hour < 10:
            return 0.80, 0.20
        elif local_hour < 14:
            return 0.50, 0.50
        else:
            return 0.20, 0.80

    def _get_weather_edge_estimate(self, ticker: str, side: str, current_price: int) -> Optional[float]:
        """
        Get edge estimate for a weather position using WeatherStrategy.

        Parses the ticker, fetches fresh NWS forecast, computes probability,
        returns edge. More accurate than generic forecaster for weather markets.
        """
        try:
            from src.strategy.weather_strategy import get_weather_strategy
            strategy = get_weather_strategy()
            parsed = strategy.parse_ticker_with_direction_fix(ticker)
            if not parsed:
                return None

            city = parsed["city"]
            forecast_date = parsed["date"]

            # Skip forecast for past dates (e.g. monthly tickers like
            # KXBOSSNOWM-26FEB parse to Feb 1 which may already be past)
            from datetime import date as _date_type
            if forecast_date < _date_type.today():
                return None

            forecast = strategy.nws_client.get_forecast(city, forecast_date)
            if not forecast:
                return None

            market_type = parsed["market_type"]
            if market_type == "temperature":
                our_prob, _ = strategy._calculate_probability(
                    forecast, parsed["threshold"], parsed["type"],
                    is_bracket=parsed.get("is_bracket", False),
                    temp_series=parsed.get("temp_series"),
                )
            elif market_type == "snow":
                our_prob, _ = strategy._calculate_snow_probability(
                    forecast, parsed["threshold"], parsed.get("is_monthly", False)
                )
            elif market_type == "rain":
                our_prob, _ = strategy._calculate_rain_probability(
                    forecast, parsed["threshold"], parsed.get("is_monthly", False)
                )
            else:
                return None

            market_price = current_price / 100.0
            if side == "yes":
                return our_prob - market_price
            else:
                return (1.0 - our_prob) - (1.0 - market_price)
        except Exception as e:
            logger.debug(f"Weather edge estimate failed for {ticker}: {e}")
            return None

    def _get_weather_city(self, ticker: str) -> Optional[str]:
        """Extract NWS city name from a weather ticker."""
        try:
            from src.strategy.weather_strategy import get_weather_strategy
            strategy = get_weather_strategy()
            parsed = strategy._parse_weather_ticker(ticker)
            return parsed["city"] if parsed else None
        except Exception:
            return None

    def _get_peak_price(self, ticker: str, side: str, entry_price: int) -> int:
        """Get peak price since entry for a position from PriceDB."""
        try:
            with next(get_db_session()) as session:
                if side == "yes":
                    peak = session.query(func.max(PriceDB.yes_bid)).filter(
                        PriceDB.ticker == ticker
                    ).scalar()
                    return max(peak or entry_price, entry_price)
                else:
                    min_ask = session.query(func.min(PriceDB.yes_ask)).filter(
                        PriceDB.ticker == ticker
                    ).scalar()
                    return max(100 - (min_ask or (100 - entry_price)), entry_price)
        except Exception:
            return entry_price

    def _evaluate_weather_position(self, pos_data: Dict[str, Any]) -> ExitDecision:
        """
        Weather-specific exit evaluation with sliding trust model.

        Completely replaces the generic exit chain for weather positions.

        For weather_yes_convergence:
        - Progressive take-profit: +5c(25%), +10c(50%), +15c(100%)
        - Edge+momentum combination matrix
        - Hard exit before market close (NEVER hold YES to resolution)

        For weather_no_hold:
        - Default: HOLD to resolution
        - Trim if forecast shifts toward bracket (within ±2F)
        - Free capital if NO priced at 90c+
        """
        ticker = pos_data["ticker"]
        side = pos_data["side"]
        quantity = pos_data["quantity"]
        entry_price = pos_data["entry_price"]
        created_at = pos_data["created_at"]
        current_price = pos_data["current_price"]

        if current_price is None:
            return ExitDecision(
                ticker=ticker, side=side, quantity=quantity,
                should_exit=False, reasoning="No current price available",
            )

        pnl_cents = self._calculate_pnl_cents(side, entry_price, current_price)
        pnl_dollars = quantity * (pnl_cents / 100.0)
        pnl_pct = (pnl_cents / entry_price) if entry_price > 0 else 0.0
        hours_held = (datetime.now(timezone.utc) - created_at).total_seconds() / 3600

        # ── 1. CHECK IF MARKET HAS CLOSED — must be FIRST to avoid ──
        # ── 409 market_closed errors on exit attempts for past markets ──
        try:
            with next(get_db_session()) as session:
                market = session.query(MarketDB).filter(
                    MarketDB.ticker == ticker
                ).first()
                if market and market.close_time:
                    close_time = market.close_time
                    if close_time.tzinfo is None:
                        close_time = close_time.replace(tzinfo=timezone.utc)
                    if datetime.now(timezone.utc) > close_time:
                        # Market has closed — NEVER try to trade
                        if market.result is not None:
                            # Market is settled — position sync will clean this up
                            return ExitDecision(
                                ticker=ticker, side=side, quantity=quantity,
                                should_exit=False,
                                reasoning=(
                                    f"Market settled (result={market.result}), "
                                    f"awaiting API sync cleanup"
                                ),
                            )
                        else:
                            # Market closed but not yet settled — hold for resolution
                            return ExitDecision(
                                ticker=ticker, side=side, quantity=quantity,
                                should_exit=False,
                                reasoning=(
                                    f"Market closed, awaiting resolution "
                                    f"(held {hours_held:.1f}h, pnl={pnl_cents:+.0f}c)"
                                ),
                            )
        except Exception as e:
            logger.debug(f"Market close check failed for {ticker}: {e}")

        # ── 1b. TICKER DATE CHECK — quick filter for obviously past markets ──
        # Parse date from ticker (e.g., KXHIGHNY-26FEB12-T32 → Feb 12, 2026)
        try:
            from datetime import date as _date_type
            ticker_date = parse_ticker_date(ticker)
            if ticker_date is not None:
                if ticker_date < _date_type.today():
                    logger.debug(
                        f"[REEVAL] Skipping past-date ticker {ticker} "
                        f"(date={ticker_date}, today={_date_type.today()})"
                    )
                    return ExitDecision(
                        ticker=ticker, side=side, quantity=quantity,
                        should_exit=False,
                        reasoning=(
                            f"Past-date market ({ticker_date}), "
                            f"awaiting settlement via API sync"
                        ),
                    )
        except Exception:
            pass  # If parsing fails, continue with normal evaluation

        # ── 2. GRACE PERIOD ──
        if hours_held < (self.MIN_HOLD_MINUTES / 60.0):
            return ExitDecision(
                ticker=ticker, side=side, quantity=quantity, should_exit=False,
                reasoning=f"Grace period: held {hours_held*60:.0f}min < {self.MIN_HOLD_MINUTES}min",
            )

        # ── 3. HARD SAFETY STOP-LOSS ──
        if pnl_pct <= -self.HARD_STOP_LOSS_PCT:
            net_profit = self._calculate_net_profit(side, quantity, entry_price, current_price)
            return ExitDecision(
                ticker=ticker, side=side, quantity=quantity, should_exit=True,
                reason=ExitReason.STOP_LOSS,
                entry_price_cents=entry_price, current_price_cents=current_price,
                unrealized_pnl_dollars=pnl_dollars, unrealized_pnl_pct=pnl_pct,
                net_profit_after_fees=net_profit, hours_held=hours_held,
                reasoning=f"Hard stop-loss: {pnl_pct:.0%} loss exceeds -{self.HARD_STOP_LOSS_PCT:.0%}",
            )

        # ── 4. STRATEGY-SPECIFIC LOGIC ──
        strategy = self._get_position_strategy(ticker)

        # YES strategies → dynamic cost-basis exit (Phase 2)
        yes_strategies = (
            "weather_forecast_yes", "weather_near_yes",
            "weather_yes_convergence",  # backward compat
        )
        # NO strategies → hold to resolution
        no_strategies = (
            "weather_no_hold", "weather_far_no", "weather_medium_no",
            "weather_very_far_no",
        )

        if strategy in yes_strategies:
            return self._evaluate_yes_dynamic(
                ticker, side, quantity, entry_price, current_price,
                pnl_cents, pnl_dollars, pnl_pct, hours_held,
            )
        elif strategy in no_strategies:
            return self._evaluate_no_hold(
                ticker, side, quantity, entry_price, current_price,
                pnl_cents, pnl_dollars, pnl_pct, hours_held,
            )
        else:
            # Fallback for legacy "weather" strategy tag
            if side == "yes":
                return self._evaluate_yes_dynamic(
                    ticker, side, quantity, entry_price, current_price,
                    pnl_cents, pnl_dollars, pnl_pct, hours_held,
                )
            else:
                return self._evaluate_no_hold(
                    ticker, side, quantity, entry_price, current_price,
                    pnl_cents, pnl_dollars, pnl_pct, hours_held,
                )

    def _is_obs_confirmed(self, ticker: str) -> bool:
        """Check if observed temperature confirms our YES bracket.

        If the live observation shows the high/low IS in our bracket AND
        we're past peak hours, this is a near-guaranteed win — hold.
        """
        try:
            from src.strategy.weather_strategy import get_weather_strategy
            from src.data_sources.nws_weather import NWSClient

            strategy = get_weather_strategy()
            parsed = strategy._parse_weather_ticker(ticker)
            if not parsed or not parsed.get("is_bracket"):
                return False

            city = parsed["city"]
            nws = strategy.nws_client
            if not hasattr(nws, "get_current_observation"):
                return False

            obs = nws.get_current_observation(city)
            if obs is None:
                return False

            threshold = parsed["threshold"]
            bracket_lower = threshold - 1.0
            bracket_upper = threshold + 1.0

            if parsed["type"] == "above":
                observed = obs.observed_high_f
                past_peak = obs.is_past_peak_high
            else:
                observed = obs.observed_low_f
                past_peak = getattr(obs, "is_past_sunrise", False)

            if bracket_lower <= observed < bracket_upper and past_peak:
                return True

            return False
        except Exception:
            return False

    def _evaluate_yes_dynamic(
        self, ticker: str, side: str, quantity: int, entry_price: int,
        current_price: int, pnl_cents: float, pnl_dollars: float,
        pnl_pct: float, hours_held: float,
    ) -> ExitDecision:
        """
        Dynamic YES exit: cover cost basis, then pure profit, then hold.

        3-stage cost-basis-first approach (replaces rigid cent tiers):
        1. Obs-confirmed → HOLD (near-guaranteed win)
        2. Edge flipped → EXIT immediately
        3. Edge gone → EXIT
        4. Stage 1: Sell enough contracts to recover total position cost
        5. Stage 2: Sell 50% of remainder at 3x entry or edge-gone
        6. Stage 3: Hold remainder unless time pressure
        7. Hard exit: >12h held without obs confirmation
        8. Default: HOLD
        """
        import math

        city = self._get_weather_city(ticker)
        stage = self._exit_stages.get(ticker, 0)

        # ── Obs-confirmed → HOLD (overrides everything below) ──
        if self._is_obs_confirmed(ticker):
            return ExitDecision(
                ticker=ticker, side=side, quantity=quantity, should_exit=False,
                reasoning=(
                    f"OBS CONFIRMED: observed temp is in bracket, past peak — "
                    f"holding to settlement (stage={stage}, pnl={pnl_cents:+.0f}c)"
                ),
            )

        # ── Forecast shift: bracket role changed from YES to NO → EXIT ──
        # If the forecast moved and this bracket is now classified as a NO role
        # (medium_no, far_no, very_far_no), we're holding YES on the wrong side.
        try:
            from src.strategy.city_bracket_portfolio import (
                CityBracketPortfolioBuilder, BracketRole,
            )
            from src.strategy.weather_strategy import get_weather_strategy
            _ws = get_weather_strategy()
            _parsed = _ws._parse_weather_ticker(ticker)
            if _parsed and _parsed.get("is_bracket"):
                from datetime import date as _date_type
                if _parsed["date"] >= _date_type.today():
                    _fc = _ws.nws_client.get_forecast(
                        _parsed["city"], _parsed["date"],
                    )
                    if _fc:
                        _fc_temp = (
                            _fc.high_f if _parsed["type"] == "above"
                            else _fc.low_f
                        )
                        _threshold = _parsed["threshold"]
                        _bl = _threshold - 1.0
                        _bu = _threshold + 1.0
                        _builder = CityBracketPortfolioBuilder()
                        _cur_role = _builder.classify_bracket(_bl, _bu, _fc_temp)
                        _NO_ROLES = (
                            BracketRole.MEDIUM_NO,
                            BracketRole.FAR_NO,
                            BracketRole.VERY_FAR_NO,
                        )
                        if _cur_role in _NO_ROLES:
                            net_profit = self._calculate_net_profit(
                                side, quantity, entry_price, current_price,
                            )
                            logger.info(
                                f"🔄 YES ROLE SHIFT: {ticker} | bracket "
                                f"{_threshold}F now {_cur_role.value} "
                                f"(forecast={_fc_temp:.0f}F) — wrong side"
                            )
                            return ExitDecision(
                                ticker=ticker, side=side, quantity=quantity,
                                should_exit=True,
                                reason=ExitReason.WEATHER_NO_FORECAST_SHIFT,
                                entry_price_cents=entry_price,
                                current_price_cents=current_price,
                                unrealized_pnl_dollars=pnl_dollars,
                                unrealized_pnl_pct=pnl_pct,
                                net_profit_after_fees=net_profit,
                                hours_held=hours_held,
                                reasoning=(
                                    f"YES position role shifted: bracket "
                                    f"{_threshold}F now {_cur_role.value} "
                                    f"(forecast={_fc_temp:.0f}F) — exiting "
                                    f"wrong-side position"
                                ),
                            )
        except Exception as _rse:
            logger.debug(f"Role shift check failed for {ticker}: {_rse}")

        # ── Get edge estimate ──
        edge = self._get_weather_edge_estimate(ticker, side, current_price)
        if edge is not None:
            self._record_edge(ticker, side, edge)

            # ── Edge FLIPPED → EXIT immediately ──
            if edge < -0.02:
                net_profit = self._calculate_net_profit(side, quantity, entry_price, current_price)
                logger.info(
                    f"🔴 YES DYNAMIC EXIT: {ticker} | edge flipped to {edge:+.1%} | "
                    f"stage={stage} | selling all {quantity}"
                )
                return ExitDecision(
                    ticker=ticker, side=side, quantity=quantity, should_exit=True,
                    reason=ExitReason.EDGE_FLIPPED,
                    entry_price_cents=entry_price, current_price_cents=current_price,
                    unrealized_pnl_dollars=pnl_dollars, unrealized_pnl_pct=pnl_pct,
                    net_profit_after_fees=net_profit, model_edge=edge, hours_held=hours_held,
                    reasoning=f"YES dynamic: edge flipped to {edge:+.1%} — thesis broken",
                )

            # ── Edge GONE → EXIT ──
            if edge >= 0 and edge < self.WEATHER_YES_MIN_EDGE_STAGE2:
                net_profit = self._calculate_net_profit(side, quantity, entry_price, current_price)
                logger.info(
                    f"🟡 YES DYNAMIC EXIT: {ticker} | edge gone ({edge:+.1%}) | "
                    f"stage={stage} | selling all {quantity}"
                )
                return ExitDecision(
                    ticker=ticker, side=side, quantity=quantity, should_exit=True,
                    reason=ExitReason.WEATHER_YES_EDGE_GONE,
                    entry_price_cents=entry_price, current_price_cents=current_price,
                    unrealized_pnl_dollars=pnl_dollars, unrealized_pnl_pct=pnl_pct,
                    net_profit_after_fees=net_profit, model_edge=edge, hours_held=hours_held,
                    reasoning=f"YES dynamic: edge gone ({edge:+.1%}) — market caught up",
                )

        # ── Stage 1: Cover cost basis ──
        if stage < 1 and current_price >= entry_price * self.WEATHER_YES_STAGE1_MIN_MULT:
            # Calculate how many contracts to sell to cover total cost
            total_cost_cents = entry_price * quantity
            contracts_to_cover = math.ceil(total_cost_cents / current_price)
            contracts_to_cover = min(contracts_to_cover, quantity - 1)  # Keep at least 1

            if contracts_to_cover > 0:
                can_recover = contracts_to_cover * current_price
                if can_recover >= total_cost_cents:
                    remaining_after = quantity - contracts_to_cover
                    net_profit = self._calculate_net_profit(
                        side, contracts_to_cover, entry_price, current_price,
                    )
                    logger.info(
                        f"💰 YES STAGE 1: {ticker} | sell {contracts_to_cover} @ "
                        f"{current_price}c to cover {total_cost_cents}c cost | "
                        f"{remaining_after} contracts now FREE"
                    )
                    self._exit_stages[ticker] = 1
                    return ExitDecision(
                        ticker=ticker, side=side, quantity=contracts_to_cover,
                        should_exit=True,
                        reason=ExitReason.WEATHER_YES_COST_BASIS,
                        entry_price_cents=entry_price, current_price_cents=current_price,
                        unrealized_pnl_dollars=pnl_dollars, unrealized_pnl_pct=pnl_pct,
                        net_profit_after_fees=net_profit, model_edge=edge,
                        hours_held=hours_held,
                        reasoning=(
                            f"Stage 1: sell {contracts_to_cover} @ {current_price}c "
                            f"to cover {total_cost_cents}c cost basis. "
                            f"{remaining_after} contracts now risk-free."
                        ),
                    )

        # ── Stage 2: Pure profit take (after Stage 1 complete) ──
        if stage == 1:
            take_profit = False
            reason_text = ""

            if current_price >= entry_price * self.WEATHER_YES_STAGE2_MULT:
                take_profit = True
                reason_text = f"price {current_price}c >= {self.WEATHER_YES_STAGE2_MULT}x entry"
            elif edge is not None and edge < self.WEATHER_YES_MIN_EDGE_STAGE2:
                take_profit = True
                reason_text = f"edge {edge:+.1%} < {self.WEATHER_YES_MIN_EDGE_STAGE2:.0%}"

            if take_profit:
                sell_qty = max(1, int(quantity * self.WEATHER_YES_STAGE2_SELL_FRAC))
                net_profit = self._calculate_net_profit(
                    side, sell_qty, entry_price, current_price,
                )
                logger.info(
                    f"💰 YES STAGE 2: {ticker} | sell {sell_qty} of {quantity} "
                    f"(pure profit) | {reason_text}"
                )
                self._exit_stages[ticker] = 2
                return ExitDecision(
                    ticker=ticker, side=side, quantity=sell_qty, should_exit=True,
                    reason=ExitReason.WEATHER_YES_PURE_PROFIT,
                    entry_price_cents=entry_price, current_price_cents=current_price,
                    unrealized_pnl_dollars=pnl_dollars, unrealized_pnl_pct=pnl_pct,
                    net_profit_after_fees=net_profit, model_edge=edge,
                    hours_held=hours_held,
                    reasoning=f"Stage 2: sell {sell_qty} — {reason_text}",
                )

        # ── Stage 3: Hold remainder unless time pressure ──
        if stage >= 2 and hours_held > 10:
            # Check if price is declining in last 2 hours before close
            peak_price = self._get_peak_price(ticker, side, entry_price)
            if peak_price > current_price:
                decline_pct = (peak_price - current_price) / peak_price
                if decline_pct >= 0.15:
                    net_profit = self._calculate_net_profit(
                        side, quantity, entry_price, current_price,
                    )
                    logger.info(
                        f"💰 YES STAGE 3: {ticker} | exit {quantity} remaining | "
                        f"time pressure + price declining {decline_pct:.0%}"
                    )
                    return ExitDecision(
                        ticker=ticker, side=side, quantity=quantity, should_exit=True,
                        reason=ExitReason.WEATHER_YES_REMAINDER_EXIT,
                        entry_price_cents=entry_price, current_price_cents=current_price,
                        unrealized_pnl_dollars=pnl_dollars, unrealized_pnl_pct=pnl_pct,
                        net_profit_after_fees=net_profit, hours_held=hours_held,
                        reasoning=(
                            f"Stage 3: time pressure ({hours_held:.0f}h) + "
                            f"price declining {decline_pct:.0%} — exit remainder"
                        ),
                    )

        # ── Hard exit: >12h without obs confirmation ──
        if hours_held > 12:
            net_profit = self._calculate_net_profit(side, quantity, entry_price, current_price)
            logger.info(
                f"⏰ YES HARD EXIT: {ticker} | held {hours_held:.0f}h (>12h) | "
                f"stage={stage} | pnl={pnl_cents:+.0f}c"
            )
            return ExitDecision(
                ticker=ticker, side=side, quantity=quantity, should_exit=True,
                reason=ExitReason.WEATHER_YES_HARD_EXIT,
                entry_price_cents=entry_price, current_price_cents=current_price,
                unrealized_pnl_dollars=pnl_dollars, unrealized_pnl_pct=pnl_pct,
                net_profit_after_fees=net_profit, hours_held=hours_held,
                reasoning=(
                    f"Hard exit: held {hours_held:.0f}h > 12h without obs confirmation "
                    f"(stage={stage}, pnl={pnl_cents:+.0f}c)"
                ),
            )

        # ── Default: HOLD ──
        return ExitDecision(
            ticker=ticker, side=side, quantity=quantity, should_exit=False,
            reasoning=(
                f"YES dynamic: holding (stage={stage}, pnl={pnl_cents:+.0f}c, "
                f"price={current_price}c, edge={edge or 'N/A'})"
            ),
        )

    # Keep old method name as alias for backward compatibility
    _evaluate_yes_convergence = _evaluate_yes_dynamic

    def _evaluate_no_hold(
        self, ticker: str, side: str, quantity: int, entry_price: int,
        current_price: int, pnl_cents: float, pnl_dollars: float,
        pnl_pct: float, hours_held: float,
    ) -> ExitDecision:
        """
        NO hold position: hold to resolution by default.

        Only exit if:
        1. Forecast shifts toward bracket (thesis broken)
        2. NO priced at 90c+ (free capital for next event)
        """
        # ── Forecast shift check ──
        try:
            from src.strategy.weather_strategy import get_weather_strategy
            strategy = get_weather_strategy()
            parsed = strategy._parse_weather_ticker(ticker)
            if parsed:
                # Skip forecast for past dates (monthly tickers parse to 1st of month)
                from datetime import date as _date_type
                forecast = None
                if parsed["date"] >= _date_type.today():
                    forecast = strategy.nws_client.get_forecast(parsed["city"], parsed["date"])
                if forecast:
                    if parsed["market_type"] == "temperature":
                        forecast_temp = forecast.high_f if parsed["type"] == "above" else forecast.low_f
                        distance = abs(parsed["threshold"] - forecast_temp)

                        # Bracket now within ±2F of forecast → EXIT (thesis broken)
                        if distance <= self.WEATHER_NO_TRIM_DISTANCE_F:
                            net_profit = self._calculate_net_profit(side, quantity, entry_price, current_price)
                            return ExitDecision(
                                ticker=ticker, side=side, quantity=quantity, should_exit=True,
                                reason=ExitReason.WEATHER_NO_FORECAST_SHIFT,
                                entry_price_cents=entry_price, current_price_cents=current_price,
                                unrealized_pnl_dollars=pnl_dollars, unrealized_pnl_pct=pnl_pct,
                                net_profit_after_fees=net_profit, hours_held=hours_held,
                                reasoning=(
                                    f"NO hold: forecast shifted to {forecast_temp}F, "
                                    f"bracket {parsed['threshold']}F now within "
                                    f"±{self.WEATHER_NO_TRIM_DISTANCE_F:.0f}F — thesis broken"
                                ),
                            )

                        # Forecast shifted partway — trim 50% if distance shrunk significantly
                        edge = self._get_weather_edge_estimate(ticker, side, current_price)
                        if edge is not None and edge < 0.03 and distance <= 4:
                            sell_qty = max(1, quantity // 2)
                            net_profit = self._calculate_net_profit(side, sell_qty, entry_price, current_price)
                            return ExitDecision(
                                ticker=ticker, side=side, quantity=sell_qty, should_exit=True,
                                reason=ExitReason.WEATHER_NO_FORECAST_SHIFT,
                                entry_price_cents=entry_price, current_price_cents=current_price,
                                unrealized_pnl_dollars=pnl_dollars, unrealized_pnl_pct=pnl_pct,
                                net_profit_after_fees=net_profit, model_edge=edge, hours_held=hours_held,
                                reasoning=(
                                    f"NO hold: forecast closing in ({distance:.0f}F away, "
                                    f"edge={edge:.1%}) — trim 50%"
                                ),
                            )
        except Exception as e:
            logger.warning(f"⚠️ NO hold forecast check FAILED for {ticker}: {e} — cannot verify thesis, backup checks will run")

        # ── Free capital: NO priced at 90c+ ──
        # current_price is the YES mid-price from PriceDB.
        # NO price = 100 - YES price. Convert before comparing.
        if side == "no":
            no_price = (100 - current_price) / 100.0
            if no_price >= self.WEATHER_NO_FREE_CAPITAL_THRESHOLD:
                net_profit = self._calculate_net_profit(side, quantity, entry_price, current_price)
                return ExitDecision(
                    ticker=ticker, side=side, quantity=quantity, should_exit=True,
                    reason=ExitReason.WEATHER_YES_EDGE_GONE,
                    entry_price_cents=entry_price, current_price_cents=current_price,
                    unrealized_pnl_dollars=pnl_dollars, unrealized_pnl_pct=pnl_pct,
                    net_profit_after_fees=net_profit, hours_held=hours_held,
                    reasoning=(
                        f"NO hold: priced at {no_price:.0%} — free capital "
                        f"(threshold {self.WEATHER_NO_FREE_CAPITAL_THRESHOLD:.0%})"
                    ),
                )

        # ── Opportunity cost: exit if much better opportunity available ──
        edge = self._get_weather_edge_estimate(ticker, side, current_price)
        opp_cost = self._check_opportunity_cost(
            ticker, side, quantity, entry_price, current_price,
            edge, pnl_cents, pnl_dollars, pnl_pct, hours_held,
        )
        if opp_cost is not None:
            return opp_cost

        # Default: HOLD to resolution
        return ExitDecision(
            ticker=ticker, side=side, quantity=quantity, should_exit=False,
            reasoning=f"NO hold: holding to resolution (pnl={pnl_cents:+.0f}c, hours={hours_held:.1f}h)",
        )

    def _evaluate_position(self, pos_data: Dict[str, Any]) -> ExitDecision:
        """
        Evaluate a single position using model edge + momentum analysis.

        Priority order:
        0. Weather positions → specialized weather exit logic
        1. Grace period (skip very new positions)
        2. Hard safety stop-loss (catastrophic protection, 50% of entry)
        3. Model-based edge evaluation:
           a. Edge < 0 → EXIT (model disagrees with position)
           b. Edge < 3% → EXIT (below transaction cost floor)
           c. Edge decaying + momentum reversed + in profit → SMART TAKE-PROFIT
           d. Edge strong → HOLD (regardless of unrealized P&L)
        4. No model data: momentum-only fallback + time decay
        5. Default: HOLD
        """
        ticker = pos_data["ticker"]

        # Weather positions use specialized exit logic
        if self._is_weather_position(ticker):
            return self._evaluate_weather_position(pos_data)
        side = pos_data["side"]
        quantity = pos_data["quantity"]
        entry_price = pos_data["entry_price"]
        created_at = pos_data["created_at"]
        current_price = pos_data["current_price"]

        # No price available — can't evaluate
        if current_price is None:
            return ExitDecision(
                ticker=ticker, side=side, quantity=quantity,
                should_exit=False, reasoning="No current price available",
            )

        # Calculate P&L (for reporting and momentum, NOT primary exit trigger)
        pnl_cents = self._calculate_pnl_cents(side, entry_price, current_price)
        pnl_dollars = quantity * (pnl_cents / 100.0)
        pnl_pct = (pnl_cents / entry_price) if entry_price > 0 else 0.0

        hours_held = (datetime.now(timezone.utc) - created_at).total_seconds() / 3600

        # ── 1. GRACE PERIOD ──
        if hours_held < (self.MIN_HOLD_MINUTES / 60.0):
            return ExitDecision(
                ticker=ticker, side=side, quantity=quantity, should_exit=False,
                reasoning=f"Grace period: held {hours_held*60:.0f}min < {self.MIN_HOLD_MINUTES}min",
            )

        # ── 2. HARD SAFETY STOP-LOSS (50% of entry) ──
        if pnl_pct <= -self.HARD_STOP_LOSS_PCT:
            net_profit = self._calculate_net_profit(side, quantity, entry_price, current_price)
            return ExitDecision(
                ticker=ticker, side=side, quantity=quantity, should_exit=True,
                reason=ExitReason.STOP_LOSS,
                entry_price_cents=entry_price, current_price_cents=current_price,
                unrealized_pnl_dollars=pnl_dollars, unrealized_pnl_pct=pnl_pct,
                net_profit_after_fees=net_profit, hours_held=hours_held,
                reasoning=f"Hard stop-loss: {pnl_pct:.0%} loss exceeds -{self.HARD_STOP_LOSS_PCT:.0%} threshold",
            )

        # ── 3. MODEL-BASED EDGE EVALUATION ──
        edge = self._get_cached_edge_estimate(ticker, side, current_price)

        if edge is not None:
            # Record edge history for decay tracking
            self._record_edge(ticker, side, edge)

            # 3a. Edge FLIPPED negative → EXIT immediately
            if edge < 0:
                net_profit = self._calculate_net_profit(side, quantity, entry_price, current_price)
                return ExitDecision(
                    ticker=ticker, side=side, quantity=quantity, should_exit=True,
                    reason=ExitReason.EDGE_FLIPPED,
                    entry_price_cents=entry_price, current_price_cents=current_price,
                    unrealized_pnl_dollars=pnl_dollars, unrealized_pnl_pct=pnl_pct,
                    net_profit_after_fees=net_profit, model_edge=edge, hours_held=hours_held,
                    reasoning=f"Edge flipped: {edge:+.1%} (model now disagrees with {side} position)",
                )

            # 3b. Edge ERODED below transaction cost floor → EXIT
            if edge < self.MIN_EDGE_TO_HOLD:
                net_profit = self._calculate_net_profit(side, quantity, entry_price, current_price)
                return ExitDecision(
                    ticker=ticker, side=side, quantity=quantity, should_exit=True,
                    reason=ExitReason.EDGE_ERODED,
                    entry_price_cents=entry_price, current_price_cents=current_price,
                    unrealized_pnl_dollars=pnl_dollars, unrealized_pnl_pct=pnl_pct,
                    net_profit_after_fees=net_profit, model_edge=edge, hours_held=hours_held,
                    reasoning=(
                        f"Edge eroded: {edge:.1%} < {self.MIN_EDGE_TO_HOLD:.0%} "
                        f"(Kalshi 2% fee + ~1% spread) | pnl={pnl_cents:+d}c"
                    ),
                )

            # 3c. SMART TAKE-PROFIT: edge decaying + momentum reversing + in profit
            # Only check when we're in profit — no point taking a "profit" at a loss
            if pnl_cents > 0:
                take_profit = self._check_edge_decay_momentum(
                    ticker, side, quantity, entry_price, current_price,
                    edge, pnl_cents, pnl_dollars, pnl_pct, hours_held, created_at,
                )
                if take_profit is not None:
                    return take_profit

            # 3d. Edge STILL STRONG → HOLD
            return ExitDecision(
                ticker=ticker, side=side, quantity=quantity, should_exit=False,
                model_edge=edge,
                reasoning=f"Hold: edge={edge:.1%} > {self.MIN_EDGE_TO_HOLD:.0%} | pnl={pnl_cents:+d}c, held {hours_held:.1f}h",
            )

        # ── 4. NO MODEL DATA: momentum-only fallback + time decay ──
        # When the forecaster can't re-estimate (API failure, unsupported market),
        # fall back to pure momentum analysis for positions in profit.

        if pnl_cents > 0:
            momentum_exit = self._check_momentum_only_exit(
                ticker, side, quantity, entry_price, current_price,
                pnl_cents, pnl_dollars, pnl_pct, hours_held, created_at,
            )
            if momentum_exit is not None:
                return momentum_exit

        # Force exit at 48h+ with unknown edge
        if hours_held >= self.FORCE_EXIT_HOURS:
            net_profit = self._calculate_net_profit(side, quantity, entry_price, current_price)
            return ExitDecision(
                ticker=ticker, side=side, quantity=quantity, should_exit=True,
                reason=ExitReason.TIME_DECAY_48H,
                entry_price_cents=entry_price, current_price_cents=current_price,
                unrealized_pnl_dollars=pnl_dollars, unrealized_pnl_pct=pnl_pct,
                net_profit_after_fees=net_profit, hours_held=hours_held,
                reasoning=f"Force exit: {hours_held:.0f}h held (>{self.FORCE_EXIT_HOURS}h), edge=unknown",
            )

        # ── 5. OPPORTUNITY COST ──
        opp_cost = self._check_opportunity_cost(
            ticker, side, quantity, entry_price, current_price,
            edge, pnl_cents, pnl_dollars, pnl_pct, hours_held,
        )
        if opp_cost is not None:
            return opp_cost

        # ── 6. DEFAULT: HOLD ──
        return ExitDecision(
            ticker=ticker, side=side, quantity=quantity, should_exit=False,
            reasoning=f"Hold (no model): pnl={pnl_cents:+d}c, held {hours_held:.1f}h, edge=unavailable",
        )

    # ─── Smart take-profit: edge decay + momentum ────────────────────────

    def _check_edge_decay_momentum(
        self,
        ticker: str, side: str, quantity: int,
        entry_price: int, current_price: int,
        edge: float,
        pnl_cents: int, pnl_dollars: float, pnl_pct: float,
        hours_held: float, created_at: datetime,
    ) -> Optional[ExitDecision]:
        """
        Smart take-profit: exit when edge is decaying AND price momentum reversed.

        Math:
        - edge_retention = current_edge / peak_edge (1.0 = no decay, 0.0 = fully gone)
        - retracement = (peak_price - current_price) / (peak_price - entry_price)
        - When edge_retention < EDGE_DECAY_TRIGGER (50%) AND retracement > MOMENTUM_RETRACEMENT_PCT (30%):
          → Edge advantage is dissolving AND market is moving against us → take profits

        The quantity to sell scales with severity:
        - sell_fraction = (1 - edge_retention) × (retracement / MOMENTUM_RETRACEMENT_PCT)
        - More decay + more retracement = sell more (up to 100%)
        """
        # Get peak edge from history
        peak_edge = self._get_peak_edge(ticker, side)
        if peak_edge is None or peak_edge <= 0:
            return None

        edge_retention = edge / peak_edge  # How much edge remains vs peak

        # Edge hasn't decayed enough — keep holding
        if edge_retention >= self.EDGE_DECAY_TRIGGER:
            return None

        # Check momentum retracement
        retracement = self._get_price_retracement(ticker, side, entry_price, current_price, created_at)
        if retracement is None:
            return None

        # Momentum hasn't reversed enough — keep holding
        if retracement < self.MOMENTUM_RETRACEMENT_PCT:
            return None

        # Both conditions met: edge decaying + momentum reversed → smart take-profit
        # Scale sell quantity by severity (more decay & retracement = sell more)
        decay_severity = 1.0 - edge_retention  # 0.5 → 1.0 range when triggered
        retracement_severity = min(2.0, retracement / self.MOMENTUM_RETRACEMENT_PCT)  # 1.0 → 2.0+
        sell_fraction = min(1.0, decay_severity * retracement_severity)
        sell_qty = max(1, int(quantity * sell_fraction))

        net_profit = self._calculate_net_profit(side, sell_qty, entry_price, current_price)

        return ExitDecision(
            ticker=ticker, side=side, quantity=sell_qty, should_exit=True,
            reason=ExitReason.EDGE_DECAY_MOMENTUM,
            entry_price_cents=entry_price, current_price_cents=current_price,
            unrealized_pnl_dollars=pnl_dollars, unrealized_pnl_pct=pnl_pct,
            net_profit_after_fees=net_profit, model_edge=edge, hours_held=hours_held,
            reasoning=(
                f"Smart take-profit: edge decayed to {edge_retention:.0%} of peak "
                f"({edge:.1%} vs peak {peak_edge:.1%}) + price retraced {retracement:.0%} "
                f"of move | selling {sell_qty}/{quantity} contracts"
            ),
        )

    def _check_momentum_only_exit(
        self,
        ticker: str, side: str, quantity: int,
        entry_price: int, current_price: int,
        pnl_cents: int, pnl_dollars: float, pnl_pct: float,
        hours_held: float, created_at: datetime,
    ) -> Optional[ExitDecision]:
        """
        Momentum-only exit fallback when model is unavailable.

        Uses a stricter retracement threshold (50%) since we don't have
        edge data to confirm the move is fading. Only triggers for
        positions in profit that have been held >1 hour.
        """
        if hours_held < 1.0:
            return None

        retracement = self._get_price_retracement(ticker, side, entry_price, current_price, created_at)
        if retracement is None:
            return None

        # Stricter threshold without model confirmation: 50% retracement
        MOMENTUM_ONLY_RETRACEMENT = 0.50

        if retracement >= MOMENTUM_ONLY_RETRACEMENT:
            net_profit = self._calculate_net_profit(side, quantity, entry_price, current_price)
            return ExitDecision(
                ticker=ticker, side=side, quantity=quantity, should_exit=True,
                reason=ExitReason.MOMENTUM_REVERSAL,
                entry_price_cents=entry_price, current_price_cents=current_price,
                unrealized_pnl_dollars=pnl_dollars, unrealized_pnl_pct=pnl_pct,
                net_profit_after_fees=net_profit, hours_held=hours_held,
                reasoning=(
                    f"Momentum reversal (no model): price retraced {retracement:.0%} "
                    f"of favorable move (>{MOMENTUM_ONLY_RETRACEMENT:.0%} threshold) | "
                    f"pnl={pnl_cents:+d}c"
                ),
            )

        return None

    # ─── Edge tracking ────────────────────────────────────────────────────

    def _record_edge(self, ticker: str, side: str, edge: float) -> None:
        """Record an edge reading for decay tracking."""
        cache_key = f"{ticker}:{side}"
        now = datetime.now(timezone.utc)

        if cache_key not in self._edge_history:
            self._edge_history[cache_key] = []

        self._edge_history[cache_key].append((edge, now))

        # Keep last 50 readings (covers ~4 hours at 5-min intervals)
        if len(self._edge_history[cache_key]) > 50:
            self._edge_history[cache_key] = self._edge_history[cache_key][-50:]

    def _get_peak_edge(self, ticker: str, side: str) -> Optional[float]:
        """Get the peak (maximum) edge recorded for this position."""
        cache_key = f"{ticker}:{side}"
        history = self._edge_history.get(cache_key, [])
        if not history:
            return None
        return max(edge for edge, _ in history)

    def _get_cached_edge_estimate(
        self, ticker: str, side: str, current_price: int
    ) -> Optional[float]:
        """
        Get edge estimate with caching to avoid hammering weather APIs.

        Caches edge per ticker for EDGE_CACHE_TTL_SECONDS (default 5 min).
        """
        now = datetime.now(timezone.utc)
        cache_key = f"{ticker}:{side}"

        # Check cache
        if cache_key in self._edge_cache:
            cached_edge, cached_time = self._edge_cache[cache_key]
            age_seconds = (now - cached_time).total_seconds()
            if age_seconds < self.EDGE_CACHE_TTL_SECONDS:
                return cached_edge

        # Cache miss or expired — re-forecast
        edge = self._get_edge_estimate(ticker, side, current_price)

        # Store in cache (even None results, to avoid repeated failures)
        self._edge_cache[cache_key] = (edge, now)

        return edge

    # ─── Momentum analysis ───────────────────────────────────────────────

    def _get_price_retracement(
        self,
        ticker: str, side: str,
        entry_price: int, current_price: int,
        created_at: datetime,
    ) -> Optional[float]:
        """
        Calculate how much the price has retraced from its peak favorable move.

        Returns a float from 0.0 (at peak) to 1.0+ (fully retraced or worse).
        Returns None if no peak data or no favorable move occurred.

        For YES positions: favorable = price going UP
          retracement = (peak_yes_bid - current_price) / (peak_yes_bid - entry_price)
        For NO positions: favorable = YES price going DOWN (NO value going up)
          retracement = (peak_no_value - current_no_value) / (peak_no_value - entry_no_value)
        """
        peak_favorable = self._get_peak_price_since_entry(ticker, side, created_at)
        if peak_favorable is None:
            return None

        if side == "yes":
            total_move = peak_favorable - entry_price  # Peak favorable move from entry
            pullback = peak_favorable - current_price   # How much pulled back from peak
        else:
            # For NO: peak_favorable is the best NO value (100 - min yes_ask)
            entry_no_value = 100 - entry_price  # What we think our NO is worth at entry
            current_no_value = 100 - current_price
            total_move = peak_favorable - entry_no_value
            pullback = peak_favorable - current_no_value

        if total_move <= 0:
            return None  # Price never moved in our favor

        return pullback / total_move

    def _get_peak_price_since_entry(
        self, ticker: str, side: str, created_at: datetime
    ) -> Optional[int]:
        """
        Get the peak favorable price since position entry.

        For YES side: max yes_bid since entry (highest we could have sold at).
        For NO side: max NO value = 100 - min(yes_ask) since entry.
        """
        entry_time = created_at
        if entry_time.tzinfo is None:
            entry_time = entry_time.replace(tzinfo=timezone.utc)

        try:
            with next(get_db_session()) as session:
                if side == "yes":
                    result = (
                        session.query(func.max(PriceDB.yes_bid))
                        .filter(
                            PriceDB.ticker == ticker,
                            PriceDB.timestamp >= entry_time,
                        )
                        .scalar()
                    )
                    return int(result) if result is not None else None
                else:
                    # NO side: best NO value = lowest yes_ask
                    result = (
                        session.query(func.min(PriceDB.yes_ask))
                        .filter(
                            PriceDB.ticker == ticker,
                            PriceDB.timestamp >= entry_time,
                            PriceDB.yes_ask.isnot(None),
                        )
                        .scalar()
                    )
                    if result is not None:
                        return 100 - int(result)  # Convert to NO value
                    return None

        except Exception as e:
            logger.debug(f"Peak price lookup failed for {ticker}: {e}")
            return None

    # ─── Exit failure cooldown helpers ───────────────────────────────────

    def _is_exit_on_cooldown(self, ticker: str) -> bool:
        """Check if a ticker's exit is on cooldown after repeated failures."""
        if ticker not in self._exit_failures:
            return False
        failures, last_fail = self._exit_failures[ticker]
        if failures < self.EXIT_FAILURE_MAX_RETRIES:
            return False
        elapsed = (datetime.now(timezone.utc) - last_fail).total_seconds() / 60.0
        if elapsed >= self.EXIT_FAILURE_COOLDOWN_MINUTES:
            # Cooldown expired, reset and allow retry
            self._exit_failures.pop(ticker, None)
            logger.info(f"[REEVAL] Cooldown expired for {ticker}, allowing retry")
            return False
        return True

    def _record_exit_failure(self, ticker: str) -> None:
        """Record an exit failure, incrementing the counter."""
        failures, _ = self._exit_failures.get(ticker, (0, datetime.now(timezone.utc)))
        failures += 1
        self._exit_failures[ticker] = (failures, datetime.now(timezone.utc))
        if failures >= self.EXIT_FAILURE_MAX_RETRIES:
            logger.warning(
                f"[REEVAL] {ticker} has failed {failures} consecutive exits, "
                f"cooldown for {self.EXIT_FAILURE_COOLDOWN_MINUTES}min"
            )

    def _record_exit_success(self, ticker: str) -> None:
        """Clear exit failure tracking for a ticker."""
        self._exit_failures.pop(ticker, None)

    # ─── Exit execution ──────────────────────────────────────────────────

    def execute_exit(self, decision: ExitDecision) -> bool:
        """
        Execute an exit decision by selling the position.

        Paper mode: Creates a "sell" TradeDB entry and updates PositionDB.
        Live mode: Places a real sell order on Kalshi, then updates DB.

        Returns True if exit succeeded.
        """
        # Check cooldown before attempting exit
        if self._is_exit_on_cooldown(decision.ticker):
            return False  # Silently skip — already logged when cooldown started

        if self.paper_trading:
            return self._execute_exit_paper(decision)
        else:
            result = self._execute_exit_live(decision)
            if result:
                self._record_exit_success(decision.ticker)
            else:
                self._record_exit_failure(decision.ticker)
            return result

    def _execute_exit_paper(self, decision: ExitDecision) -> bool:
        """Execute exit in paper trading mode (DB update only)."""
        try:
            with next(get_db_session()) as session:
                position = session.query(PositionDB).filter(
                    PositionDB.ticker == decision.ticker,
                    PositionDB.quantity > 0,
                ).first()

                if not position:
                    logger.warning(f"No open position found for {decision.ticker}")
                    return False

                exit_price = decision.current_price_cents
                if exit_price is None:
                    exit_price = self._get_current_price(session, decision.ticker)
                if exit_price is None:
                    logger.warning(f"No price data for {decision.ticker}")
                    return False

                entry_price = int(position.average_price or 0)
                qty = min(decision.quantity, int(position.quantity or 0))

                # exit_price = YES mid from _get_current_price().
                # For NO: entry_price = NO cost, NO exit value = 100 - exit_price.
                if decision.side == "yes":
                    gross_pnl = qty * (exit_price - entry_price) / 100.0
                else:
                    gross_pnl = qty * ((100 - exit_price) - entry_price) / 100.0

                fees = gross_pnl * KALSHI_WINNER_FEE_RATE if gross_pnl > 0 else 0.0
                net_profit = gross_pnl - fees

                reason_str = decision.reason.value if decision.reason else "unknown"
                order_id = f"REEVAL-{reason_str.upper()}-{uuid.uuid4().hex[:8].upper()}"
                trade = TradeDB(
                    order_id=order_id,
                    fill_id=f"FILL-{order_id}",
                    ticker=decision.ticker,
                    side=decision.side,
                    action="sell",
                    quantity=qty,
                    price=exit_price,
                    fee=round(fees, 2),
                    status="simulated",
                    timestamp=datetime.now(timezone.utc),
                    strategy=f"reeval_{reason_str}",
                    pnl=round(net_profit, 2),
                    pnl_percent=round(
                        (net_profit / (qty * entry_price / 100.0)) * 100 if entry_price > 0 else 0, 2
                    ),
                    resolved=1,
                    outcome="win" if net_profit > 0 else "loss",
                    resolved_at=datetime.now(timezone.utc),
                )
                session.add(trade)

                new_qty = max(0, int(position.quantity or 0) - qty)
                if new_qty == 0:
                    session.delete(position)
                else:
                    position.quantity = new_qty

                self._partial_exits[decision.ticker] = (
                    self._partial_exits.get(decision.ticker, 0) + qty
                )

                edge_str = f"edge={decision.model_edge:.1%}" if decision.model_edge is not None else "edge=N/A"
                logger.info(
                    f"[REEVAL] Paper exit | {decision.ticker} {decision.side} "
                    f"x{qty} @{exit_price}c | {reason_str} | {edge_str} | "
                    f"net=${net_profit:.2f}"
                )

                return True

        except Exception as e:
            logger.error(f"Failed to execute paper exit for {decision.ticker}: {e}")
            return False

    def _execute_exit_live(self, decision: ExitDecision) -> bool:
        """
        Execute exit in live mode by placing a real sell order on Kalshi.

        Uses the same async-in-thread pattern as executor._submit_to_kalshi().
        """
        from src.api.kalshi_client import KalshiClient

        sell_price = self._get_sell_price(decision.ticker, decision.side)
        if sell_price is None or sell_price <= 0:
            # No bid available — check if market is dead before posting resting ask
            # If YES ask is 1c (or 0c), the market has moved to ~100% NO.
            # Don't waste API calls posting asks nobody will buy — let it settle.
            try:
                from src.data.models import PriceDB
                with next(get_db_session()) as session:
                    latest_price = (
                        session.query(PriceDB)
                        .filter(PriceDB.ticker == decision.ticker)
                        .order_by(PriceDB.created_at.desc())
                        .first()
                    )
                    if latest_price:
                        yes_ask = latest_price.yes_ask or 0
                        yes_bid = latest_price.yes_bid or 0
                        if yes_ask <= 2 and yes_bid == 0:
                            logger.info(
                                f"[REEVAL] Market dead for {decision.ticker}: "
                                f"YES bid={yes_bid}c, ask={yes_ask}c — "
                                f"adding to dead_markets, will settle automatically"
                            )
                            # Mark as dead so we skip future re-evaluations
                            self._dead_markets.add(decision.ticker)
                            self._exit_failures.pop(decision.ticker, None)
                            return False  # Not a real exit — don't trigger alert
            except Exception as e:
                logger.debug(f"[REEVAL] Could not check market price for {decision.ticker}: {e}")

            # Market still has some life — try posting a resting ask
            if self.resting_order_manager is not None:
                # Don't duplicate if we already have a resting order for this ticker
                if self.resting_order_manager.has_resting_order(decision.ticker):
                    logger.debug(
                        f"[REEVAL] Already have resting order for {decision.ticker}, "
                        f"waiting for fill"
                    )
                    return False

                # Get entry price and current market ask to set a reasonable price
                entry_price = 0
                market_ask = 99
                try:
                    with next(get_db_session()) as session:
                        position = session.query(PositionDB).filter(
                            PositionDB.ticker == decision.ticker,
                            PositionDB.quantity > 0,
                        ).first()
                        if position:
                            entry_price = int(position.average_price or 0)

                        # Get current YES ask to cap our price at the market
                        from src.data.models import PriceDB
                        latest = (
                            session.query(PriceDB)
                            .filter(PriceDB.ticker == decision.ticker)
                            .order_by(PriceDB.created_at.desc())
                            .first()
                        )
                        if latest and latest.yes_ask:
                            market_ask = int(latest.yes_ask)
                except Exception:
                    pass

                # Price the ask at or below the current market ask
                # Never post above the market — it won't fill and may require margin
                if entry_price > 0:
                    initial_ask = min(entry_price, market_ask)
                else:
                    initial_ask = min(50, market_ask)
                initial_ask = max(1, initial_ask)

                logger.info(
                    f"[REEVAL] No bid for {decision.ticker} — posting resting ask "
                    f"at {initial_ask}c (entry={entry_price}c, market_ask={market_ask}c)"
                )

                import asyncio
                import concurrent.futures

                async def _post_resting():
                    return await self.resting_order_manager.post_resting_ask(
                        ticker=decision.ticker,
                        side=decision.side,
                        quantity=decision.quantity,
                        initial_price=initial_ask,
                        entry_price=entry_price,
                        reason=decision.reason.value if decision.reason else "unknown",
                    )

                try:
                    try:
                        loop = asyncio.get_running_loop()
                    except RuntimeError:
                        loop = None

                    if loop and loop.is_running():
                        with concurrent.futures.ThreadPoolExecutor() as pool:
                            future = pool.submit(asyncio.run, _post_resting())
                            posted = future.result(timeout=15)
                    else:
                        posted = asyncio.run(_post_resting())

                    return posted
                except Exception as e:
                    err_str = str(e).lower()
                    if "409" in err_str and "market_closed" in err_str:
                        logger.info(
                            f"[REEVAL] Market closed — skipping resting ask for "
                            f"{decision.ticker} (will settle automatically)"
                        )
                        self._exit_failures.pop(decision.ticker, None)
                        return True
                    logger.error(
                        f"[REEVAL] Failed to post resting ask for {decision.ticker}: {e}"
                    )
                    return False
            else:
                logger.warning(
                    f"[REEVAL] Cannot exit {decision.ticker}: no bid price available "
                    f"(illiquid market, will retry next cycle)"
                )
                return False

        qty = decision.quantity
        reason_str = decision.reason.value if decision.reason else "unknown"

        async def _place_sell_order():
            async with KalshiClient() as client:
                return await client.place_order(
                    ticker=decision.ticker,
                    side=decision.side,
                    action="sell",
                    quantity=qty,
                    order_type="limit",
                    price=sell_price,
                )

        try:
            import asyncio
            import concurrent.futures

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop and loop.is_running():
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future = pool.submit(asyncio.run, _place_sell_order())
                    api_order = future.result(timeout=30)
            else:
                api_order = asyncio.run(_place_sell_order())

            if api_order and hasattr(api_order, "order_id"):
                with next(get_db_session()) as session:
                    position = session.query(PositionDB).filter(
                        PositionDB.ticker == decision.ticker,
                        PositionDB.quantity > 0,
                    ).first()

                    if position:
                        entry_price = int(position.average_price or 0)
                        actual_qty = min(qty, int(position.quantity or 0))

                        # sell_price from _get_sell_price() is already in the correct
                        # scale: YES bid for YES, NO bid (100-yes_ask) for NO.
                        # Both entry_price and sell_price are in the same scale,
                        # so PnL = sell_price - entry_price for BOTH sides.
                        gross_pnl = actual_qty * (sell_price - entry_price) / 100.0

                        fees = gross_pnl * KALSHI_WINNER_FEE_RATE if gross_pnl > 0 else 0.0
                        net_profit = gross_pnl - fees

                        order_id = f"REEVAL-{reason_str.upper()}-{uuid.uuid4().hex[:8].upper()}"
                        trade = TradeDB(
                            order_id=order_id,
                            fill_id=str(api_order.order_id),
                            ticker=decision.ticker,
                            side=decision.side,
                            action="sell",
                            quantity=actual_qty,
                            price=sell_price,
                            fee=round(fees, 2),
                            status="filled",
                            timestamp=datetime.now(timezone.utc),
                            strategy=f"reeval_{reason_str}",
                            pnl=round(net_profit, 2),
                            pnl_percent=round(
                                (net_profit / (actual_qty * entry_price / 100.0)) * 100
                                if entry_price > 0 else 0, 2
                            ),
                            resolved=1,
                            outcome="win" if net_profit > 0 else "loss",
                            resolved_at=datetime.now(timezone.utc),
                        )
                        session.add(trade)

                        new_qty = max(0, int(position.quantity or 0) - actual_qty)
                        if new_qty == 0:
                            session.delete(position)
                        else:
                            position.quantity = new_qty

                        self._partial_exits[decision.ticker] = (
                            self._partial_exits.get(decision.ticker, 0) + actual_qty
                        )

                        edge_str = f"edge={decision.model_edge:.1%}" if decision.model_edge is not None else "edge=N/A"
                        logger.info(
                            f"[REEVAL] LIVE exit submitted | {decision.ticker} {decision.side} "
                            f"x{actual_qty} @{sell_price}c | {reason_str} | {edge_str} | "
                            f"net=${net_profit:.2f} | exchange_id={api_order.order_id}"
                        )

                return True
            else:
                logger.warning(f"[REEVAL] Kalshi returned no order response for {decision.ticker} sell")
                return False

        except Exception as e:
            err_str = str(e).lower()
            # Detect 409 "market_closed" — stop retrying, market will settle via API
            if "409" in err_str and "market_closed" in err_str:
                logger.info(
                    f"[REEVAL] Market closed — skipping exit for {decision.ticker} "
                    f"(will settle automatically via Kalshi)"
                )
                # Clear failure counter so we don't keep retrying
                self._exit_failures.pop(decision.ticker, None)
                return True  # Return True to prevent re-queuing
            # Detect 404 — market delisted/resolved, stop retrying
            # httpx HTTPStatusError str() is "Client error '404 Not Found' for url ..."
            # The "market_not_found" text is in the response body, not in str(e)
            if "404" in err_str:
                logger.info(
                    f"[REEVAL] Market not found (404) — removing ghost position for {decision.ticker} "
                    f"(already resolved/delisted by Kalshi)"
                )
                # Clear failure counter and mark position resolved in DB
                self._exit_failures.pop(decision.ticker, None)
                try:
                    with next(get_db_session()) as session:
                        pos = session.query(PositionDB).filter(
                            PositionDB.ticker == decision.ticker,
                            PositionDB.quantity > 0,
                        ).first()
                        if pos:
                            pos.quantity = 0
                            pos.status = "resolved_404"
                            pos.updated_at = datetime.now(timezone.utc)
                            session.commit()
                            logger.info(f"[REEVAL] Ghost position zeroed: {decision.ticker}")
                except Exception as db_err:
                    logger.warning(f"[REEVAL] Failed to zero ghost position {decision.ticker}: {db_err}")
                return True  # Return True to prevent re-queuing
            # Detect 400 "insufficient_balance" — position already sold or can't cover fees.
            # Retrying is pointless: zero the ghost position and stop.
            # Note: httpx HTTPStatusError str() only has "400 Bad Request" — the
            # "insufficient_balance" code is in the response body, not str(e).
            _resp_body = ""
            try:
                if hasattr(e, "response") and hasattr(e.response, "text"):
                    _resp_body = e.response.text.lower()
            except Exception:
                pass
            if "400" in err_str and "insufficient_balance" in _resp_body:
                logger.info(
                    f"[REEVAL] Insufficient balance for exit — removing ghost position for {decision.ticker} "
                    f"(likely already sold manually or fees exceed balance)"
                )
                self._exit_failures.pop(decision.ticker, None)
                try:
                    with next(get_db_session()) as session:
                        pos = session.query(PositionDB).filter(
                            PositionDB.ticker == decision.ticker,
                            PositionDB.quantity > 0,
                        ).first()
                        if pos:
                            pos.quantity = 0
                            pos.status = "resolved_insuff_balance"
                            pos.updated_at = datetime.now(timezone.utc)
                            session.commit()
                            logger.info(f"[REEVAL] Ghost position zeroed: {decision.ticker}")
                except Exception as db_err:
                    logger.warning(f"[REEVAL] Failed to zero ghost position {decision.ticker}: {db_err}")
                return True  # Return True to prevent re-queuing
            logger.error(
                f"[REEVAL] Live exit failed | {decision.ticker} {decision.side} "
                f"x{qty} @{sell_price}c | {reason_str} | {type(e).__name__}: {e}"
            )
            return False

    # ─── Stub for backwards compatibility ────────────────────────────────

    def run_llm_batch_reeval(self) -> List[ExitDecision]:
        """No-op: LLM re-evaluation disabled (deterministic-only mode)."""
        return []

    # ─── Helper methods ─────────────────────────────────────────────────

    @staticmethod
    def _get_current_price(session, ticker: str) -> Optional[int]:
        """Get current mid-price for a ticker from latest PriceDB entry."""
        latest_price = (
            session.query(PriceDB)
            .filter(PriceDB.ticker == ticker)
            .order_by(PriceDB.timestamp.desc())
            .first()
        )
        if latest_price:
            yes_bid = latest_price.yes_bid or 0
            yes_ask = latest_price.yes_ask or 0
            if yes_bid > 0 and yes_ask > 0:
                return int((yes_bid + yes_ask) / 2)
            elif yes_bid > 0:
                return int(yes_bid)
            elif yes_ask > 0:
                return int(yes_ask)
        return None

    @staticmethod
    def _get_sell_price(ticker: str, side: str) -> Optional[int]:
        """
        Get the current bid price for selling a position.

        For YES positions: sell at yes_bid (what buyers will pay for YES)
        For NO positions: sell at no_bid = 100 - yes_ask
        """
        try:
            with next(get_db_session()) as session:
                latest_price = (
                    session.query(PriceDB)
                    .filter(PriceDB.ticker == ticker)
                    .order_by(PriceDB.timestamp.desc())
                    .first()
                )
                if latest_price:
                    if side == "yes":
                        bid = latest_price.yes_bid
                        return int(bid) if bid and bid > 0 else None
                    else:
                        yes_ask = latest_price.yes_ask
                        if yes_ask and yes_ask > 0:
                            return max(1, 100 - int(yes_ask))
                        return None
        except Exception as e:
            logger.debug(f"Sell price lookup failed for {ticker}: {e}")
        return None

    @staticmethod
    def _calculate_pnl_cents(side: str, entry_price: int, current_price: int) -> int:
        """Calculate unrealized P&L in cents.

        Note: entry_price is cost paid (NO cost for NO positions, e.g. 98¢).
        current_price is always the YES mid-price from _get_current_price().
        For NO positions: current NO value = 100 - current_price (YES mid).
        """
        if side == "yes":
            return current_price - entry_price
        else:
            # entry_price = NO cost (e.g. 98¢), current_price = YES mid (e.g. 2¢)
            # NO value now = 100 - YES_mid. PnL = NO_value_now - NO_entry_cost
            return (100 - current_price) - entry_price

    @staticmethod
    def _calculate_net_profit(
        side: str, quantity: int, entry_price: int, exit_price: int
    ) -> float:
        """Calculate net profit after Kalshi 2% winner fee.

        Note: exit_price is the YES mid-price from _get_current_price().
        For NO positions: NO exit value = 100 - exit_price (YES mid).
        """
        if side == "yes":
            gross_pnl = quantity * (exit_price - entry_price) / 100.0
        else:
            # entry_price = NO cost, exit_price = YES mid
            # NO exit value = 100 - YES_mid
            gross_pnl = quantity * ((100 - exit_price) - entry_price) / 100.0

        if gross_pnl > 0:
            fees = gross_pnl * KALSHI_WINNER_FEE_RATE
        else:
            fees = 0.0

        return gross_pnl - fees

    def _get_edge_estimate(
        self, ticker: str, side: str, current_price: int
    ) -> Optional[float]:
        """
        Get edge estimate using the default forecaster.

        Returns edge as fraction (e.g. 0.05 = 5% edge), or None on failure.
        """
        try:
            from src.analysis.forecaster import get_default_forecaster

            forecaster = get_default_forecaster()
            market_title = self._get_market_title(ticker)

            forecast = forecaster.forecast(
                market_ticker=ticker,
                market_title=market_title,
                current_price=current_price,
            )

            our_prob = forecast.probability_yes
            market_prob = current_price / 100.0

            if side == "yes":
                return our_prob - market_prob
            else:
                return market_prob - our_prob

        except Exception as e:
            logger.debug(f"Edge estimate failed for {ticker}: {e}")
            return None

    @staticmethod
    def _get_market_title(ticker: str) -> str:
        """Look up market title from DB."""
        try:
            with next(get_db_session()) as session:
                market = session.query(MarketDB).filter(
                    MarketDB.ticker == ticker
                ).first()
                if market:
                    return str(market.title or ticker)
        except Exception:
            pass
        return ticker


# ─── Global instance ────────────────────────────────────────────────────

_reevaluator: Optional[PositionReEvaluator] = None


def get_position_reevaluator(paper_trading: bool = True) -> PositionReEvaluator:
    """Get or create the global position re-evaluator."""
    global _reevaluator
    if _reevaluator is None:
        _reevaluator = PositionReEvaluator(paper_trading=paper_trading)
    return _reevaluator
