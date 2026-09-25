"""
Main trading loop for the Kalshi prediction market system.

Orchestrates the complete trading cycle:
1. Data collection (markets, prices, news)
2. Analysis (forecasting, edge calculation)
3. Execution (position sizing, order placement)

Weather-only mode: trades exclusively on weather prediction markets.
Supports both live and paper trading modes.
"""
import asyncio
import os
import signal
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

# Load .env into os.environ so all modules can access env vars directly
from dotenv import load_dotenv
load_dotenv()

from src.analysis.edge import EdgeCalculator, TradingOpportunity
from src.data.collector import MarketDataCollector
from src.data.database import get_db_session
from src.data.models import MarketDB, PositionDB, PriceDB, TradeDB
from src.execution.executor import OrderExecutor, get_executor
from src.execution.portfolio import PortfolioTracker, get_portfolio_tracker
from src.execution.position_sizer import PositionSizer, get_position_sizer
from src.execution.risk_manager import RiskManager
from src.utils.datetime_utils import ensure_utc
from src.strategy.weather_strategy import get_weather_strategy
from src.execution.position_reevaluator import (
    get_position_reevaluator,
    ExitReason as ReEvalExitReason,
)
from src.analytics.model_health import ModelHealthMonitor
from src.monitoring.system_monitor import SystemMonitor, get_system_monitor
from src.utils.alerts import AlertManager, get_alert_manager
from src.utils.fees import KALSHI_WINNER_FEE_RATE, calculate_fee, is_profitable_after_fees
from src.monitoring.session_stats import get_session_stats
from src.utils.weather_stats import get_weather_stats
from src.utils.logging import logger
from src.execution.city_budget_allocator import CityBudgetAllocator
from src.execution.anti_loop_detector import AntiLoopDetector
from src.execution.resting_order_manager import RestingOrderManager
from src.analytics.edge_realization import (
    format_edge_realization_log,
    get_edge_realization_summary,
    record_edge_realizations,
)
from src.analytics.forecast_audit import get_forecast_audit
from src.analysis.edge_score import get_edge_score_calculator
from src.strategy.observation_scanner import ObservationSettledScanner
from src.strategy.forecast_reshaper import get_forecast_reshaper, ReshapeAction
from src.execution.settlement_manager import SettlementManager, get_settlement_manager
import uuid


class TradingSystem:
    """
    Main trading system orchestrator.

    Manages the complete trading cycle with configurable intervals
    and supports graceful shutdown.
    """

    # Default intervals (in seconds)
    DEFAULT_MARKET_SCAN_INTERVAL = 120  # 2 min
    DEFAULT_PRICE_UPDATE_INTERVAL = 60  # 1 min
    DEFAULT_PORTFOLIO_SNAPSHOT_INTERVAL = 1800  # 30 min

    def __init__(
        self,
        paper_trading: bool = True,
        market_scan_interval: int = 120,  # 2 min
        price_update_interval: int = 60,  # 1 min
        min_edge_threshold: float = 0.01,  # Paper: 1% edge minimum
        max_positions: int = 500,  # Paper: 500 concurrent positions
        **kwargs,
    ) -> None:
        """
        Initialize the weather-only trading system.

        Args:
            paper_trading: If True, simulate trades without real execution.
            market_scan_interval: Seconds between market scans.
            price_update_interval: Seconds between price updates.
            min_edge_threshold: Minimum edge to consider trading.
            max_positions: Maximum concurrent positions.
        """
        self.paper_trading = paper_trading
        self.market_scan_interval = market_scan_interval
        self.price_update_interval = price_update_interval
        self.min_edge_threshold = min_edge_threshold
        self.max_positions = max_positions
        self._cash_balance_cents: int = 0  # Live cash from Kalshi API, refreshed periodically

        # Components
        from src.api.kalshi_client import KalshiClient
        self._api_client = KalshiClient()
        self._api_client.ensure_client()

        # For live trading, create a Kalshi client for order execution
        if paper_trading:
            self.executor = get_executor(paper_trading=True)
        else:
            kalshi_client = KalshiClient()  # Uses settings from .env
            kalshi_client.ensure_client()  # Initialize HTTP client without context manager
            self.executor = get_executor(paper_trading=False, kalshi_client=kalshi_client)
            logger.info("Live trading: Kalshi client configured for order execution")
        self.portfolio = get_portfolio_tracker()

        # Risk manager
        if paper_trading:
            self.risk_manager = RiskManager(
                daily_loss_limit_pct=0.90,
                max_position_pct=0.50,
                max_exposure_pct=1.00,
                max_drawdown_pct=0.90,
                initial_bankroll=10000.0,
            )
            self.risk_manager.max_daily_trades = 2000
        else:
            # FAIL-SAFE defaults — sync_phase_limits() overwrites these
            # immediately, but if that call ever fails these keep us protected.
            self.risk_manager = RiskManager(
                daily_loss_limit_pct=0.08,   # Survival-phase 8% daily loss
                max_position_pct=0.10,       # Survival-phase 10% per position
                max_exposure_pct=0.60,       # Survival-phase 60% total
                max_drawdown_pct=0.25,       # 25% max drawdown
                initial_bankroll=50.0,
            )
            from config.settings import settings
            self.risk_manager.max_daily_trades = settings.max_daily_trades

        # Set as global instance so executor uses our configured settings
        from src.execution.risk_manager import set_risk_manager
        set_risk_manager(self.risk_manager)

        # Initialize bankroll phase limits
        self.risk_manager.sync_phase_limits()

        self.position_sizer = get_position_sizer()
        self.edge_calculator = EdgeCalculator(min_edge=min_edge_threshold)
        self.alert_manager = get_alert_manager()

        # Data collector and weather strategy
        self.data_collector = MarketDataCollector(
            markets_interval=market_scan_interval,
            prices_interval=price_update_interval,
            track_all_active=True,
        )
        self.weather_strategy = get_weather_strategy(min_edge=0.05)

        # Position re-evaluator for deterministic exit engine
        self.position_reevaluator = get_position_reevaluator(paper_trading=paper_trading)
        self._last_reeval_check: Optional[datetime] = None

        # Model health monitor
        self.health_monitor = ModelHealthMonitor()
        self._position_multiplier = 1.0

        # System monitor with automated alerting
        self.system_monitor = get_system_monitor()

        # State
        self._running = False
        self._shutdown_requested = False
        self._cycle_count = 0
        self._last_weather_scan: Optional[datetime] = None
        self._last_snapshot: Optional[datetime] = None
        self._last_bankroll_sync: Optional[datetime] = None
        self.BANKROLL_SYNC_INTERVAL = 300  # 5 minutes

        # Weather event-level budget tracking
        self._weather_event_spend: Dict[str, float] = {}
        self.WEATHER_MAX_PER_EVENT = float(os.environ.get("WEATHER_MAX_PER_EVENT", "20.0"))

        # City-based budget allocation + anti-loop + resting orders
        self.city_budget_allocator = CityBudgetAllocator()
        self.anti_loop_detector = AntiLoopDetector()

        # Resting order manager for illiquid exits
        def _kalshi_client_factory():
            from src.api.kalshi_client import KalshiClient as _KC
            return _KC()
        self.resting_order_manager = RestingOrderManager(
            kalshi_client_factory=_kalshi_client_factory,
            paper_trading=paper_trading,
        )
        # Wire resting order manager into position reevaluator
        self.position_reevaluator.resting_order_manager = self.resting_order_manager

        # Total portfolio value (cash + positions), updated by _sync_bankroll_from_api
        self._total_portfolio_dollars: float = 0.0

        # Settlement manager (Phase 5) — detects resolved markets, calculates PnL
        self.settlement_manager = get_settlement_manager(
            risk_manager=self.risk_manager,
            alert_manager=self.alert_manager,
            paper_trading=paper_trading,
        )
        self._last_settlement_manager_check: Optional[datetime] = None

        # Observation-settled fast scanner (60-second cycle)
        # Pass risk_manager so scanner uses obs pool for capital sizing
        self.obs_scanner = ObservationSettledScanner(risk_manager=self.risk_manager)
        self._last_obs_scan: Optional[datetime] = None
        self.OBS_SCAN_INTERVAL = int(os.environ.get("OBS_SCAN_INTERVAL", "60"))  # seconds

        # Load persisted obs pool state (survives restarts)
        self.risk_manager.load_obs_pool_state()

        # Forecast update monitoring (every 30 min)
        self._last_forecast_check: Optional[datetime] = None

        # Statistics
        self._stats: Dict[str, Any] = {
            "cycles_completed": 0,
            "opportunities_found": 0,
            "trades_executed": 0,
            "start_time": None,
            "last_cycle_time": None,
        }

        # Latest weather opportunities (passed to reevaluator for opportunity cost exits)
        self._latest_weather_opportunities: List = []

        # Heartbeat tracking
        self._last_heartbeat: Optional[datetime] = None
        self._heartbeat_interval = 3600  # 1 hour

        # Session stats (observability)
        self.session_stats = get_session_stats()
        self.weather_stats = get_weather_stats()
        self.forecast_audit = get_forecast_audit()
        self._last_settlement_check: Optional[datetime] = None
        self._last_summary: Optional[datetime] = None
        self._summary_interval = 1800  # 30 minutes

        logger.info(
            "Trading system initialized (weather-only)",
            paper_trading=paper_trading,
            market_scan_interval=market_scan_interval,
            min_edge_threshold=min_edge_threshold,
        )

    def _record_trade(self, strategy: str, price_cents: int = 0) -> None:
        """Record a trade execution in both legacy stats and session stats."""
        self._stats["trades_executed"] += 1
        self.session_stats.record_trade(strategy, price_cents)

    def _setup_signal_handlers(self) -> None:
        """Setup signal handlers for graceful shutdown."""

        def signal_handler(signum: int, frame: Any) -> None:
            logger.info("Shutdown signal received", signal=signum)
            self._shutdown_requested = True

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

    # ── Weather-specific helper methods ──────────────────────────────────

    def _get_weather_event_key(ticker: str) -> str:
        """
        Extract the weather event key from a ticker.

        Examples:
            KXHIGHNY-26FEB10-T39   -> KXHIGHNY-26FEB10
            KXLOWTNYC-26FEB10-B34.5 -> KXLOWTNYC-26FEB10
            KXBOSSNOWM-26FEB-15.0  -> KXBOSSNOWM-26FEB
            KXRAINNYCM-26FEB-3     -> KXRAINNYCM-26FEB
        """
        import re
        # Strip the last segment if it looks like a bracket threshold (-T39, -B34.5, -15.0, -3)
        m = re.match(r'^(.+)-[BT]?[\d.]+$', ticker)
        return m.group(1) if m else ticker

    def _get_weather_event_total(self, event_key: str) -> float:
        """
        Get total dollars deployed on a weather event (across all brackets).

        Checks both in-memory spend tracking (this session) and existing
        unresolved positions in the DB (from previous sessions).
        """
        # In-memory tracking from this session
        session_spend = self._weather_event_spend.get(event_key, 0.0)

        # DB: sum cost of all unresolved weather trades on this event
        db_spend = 0.0
        try:
            with next(get_db_session()) as session:
                trades = session.query(TradeDB).filter(
                    TradeDB.ticker.like(f"{event_key}%"),
                    TradeDB.action == "buy",
                    TradeDB.resolved == 0,
                    TradeDB.strategy == "weather",
                ).all()
                for t in trades:
                    db_spend += (t.quantity or 0) * (t.price or 0) / 100.0
        except Exception:
            pass

        return max(session_spend, db_spend)

    def _score_opportunity(self, opp) -> Optional[dict]:
        """
        Score a weather opportunity for ranking using EdgeScoreCalculator.

        Returns a dict with scoring components and the final composite score,
        or None if the opportunity should be filtered out entirely.
        """
        calculator = get_edge_score_calculator()
        result = calculator.score(opp)
        if result is None:
            return None
        d = result.to_dict()
        d["opp"] = opp
        return d

    def verify_edge_before_execution(
        self,
        opp,
        side: str,
        original_edge: float,
    ) -> tuple:
        """
        Pre-execution edge verification: check if edge still exists at current prices.

        Fetches the latest price from PriceDB and recalculates edge using the
        SAME model probability from the original signal. If the market has moved
        too far toward our model, the edge may be gone.

        Args:
            opp: WeatherOpportunity with our_probability and edge.
            side: "yes" or "no".
            original_edge: The edge calculated at signal time.

        Returns:
            Tuple of (still_valid: bool, current_edge: float).
            If price fetch fails, returns (True, original_edge) — fail open.
        """
        try:
            with next(get_db_session()) as session:
                latest_price = (
                    session.query(PriceDB)
                    .filter(PriceDB.ticker == opp.ticker)
                    .order_by(PriceDB.timestamp.desc())
                    .first()
                )

                if latest_price is None:
                    # No price data — fail open, proceed with trade
                    return (True, original_edge)

                # Get current ask price for our side
                if side == "yes":
                    current_ask = latest_price.yes_ask
                    if current_ask is None or current_ask <= 0:
                        return (True, original_edge)
                    current_edge = opp.our_probability - (current_ask / 100.0)
                else:  # "no"
                    current_ask = latest_price.no_ask
                    if current_ask is None or current_ask <= 0:
                        return (True, original_edge)
                    current_edge = (1.0 - opp.our_probability) - (current_ask / 100.0)

            # Edge must be directional (positive) — never use abs()
            # Check 1: edge must be at least 50% of original
            if current_edge < original_edge * 0.50:
                return (False, current_edge)

            # Check 2: absolute floor of 3% edge
            if current_edge < 0.03:
                return (False, current_edge)

            return (True, current_edge)

        except Exception as e:
            # Fail open — stale price check should not block a valid trade
            logger.warning(f"Pre-exec edge check failed for {opp.ticker}: {e}")
            return (True, original_edge)

    async def _check_settlements(self) -> None:
        """
        Check for recently resolved weather markets and log settlement values.

        Runs every hour. Finds markets that resolved in the last 24 hours
        and records their actual settlement temperature for forecast audit.
        """
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
            with next(get_db_session()) as session:
                # Find recently resolved markets
                resolved = session.query(MarketDB).filter(
                    MarketDB.result.isnot(None),
                    MarketDB.close_time >= cutoff,
                ).all()

                if not resolved:
                    return

                weather_strategy = get_weather_strategy()
                if weather_strategy is None:
                    return

                logged = 0
                for mkt in resolved:
                    ticker = str(mkt.ticker)
                    parsed = weather_strategy._parse_weather_ticker(ticker)
                    if not parsed:
                        continue

                    city = parsed.get("city", "")
                    market_date = parsed.get("date")
                    if not city or not market_date:
                        continue

                    # For temperature markets, the settlement temp is the observed value.
                    # We can infer it from whether the market resolved YES or NO:
                    # If threshold_type=above, resolved YES means actual >= threshold
                    # We use NWS observed data if available, otherwise skip.
                    try:
                        from src.data_sources.nws_weather import (
                            fetch_nws_observations,
                            KALSHI_STATIONS,
                        )
                        station_info = KALSHI_STATIONS.get(city, {})
                        station_id = station_info.get("station_id")
                        if not station_id:
                            continue

                        obs = fetch_nws_observations(station_id, market_date)
                        if obs and obs.get("temperature_f") is not None:
                            self.forecast_audit.log_settlement(
                                city=city,
                                market_date=market_date,
                                settlement_temp=float(obs["temperature_f"]),
                                settlement_source="nws_observation",
                            )
                            logged += 1
                    except ImportError:
                        break  # NWS module not available
                    except Exception:
                        continue

                if logged:
                    logger.info(f"Forecast audit: logged {logged} settlement observations")

        except Exception as e:
            logger.debug(f"Settlement check failed: {e}")

    async def _check_forecast_updates(self) -> None:
        """
        Monitor NWS forecast changes for open weather positions.

        Runs every 30 minutes. Groups positions by (city, date), fetches
        fresh NWS forecast, logs warnings when forecast shifts significantly,
        and triggers portfolio reshaping when forecast moves to a different
        bracket with shift >= 4F.
        """
        try:
            weather_strategy = get_weather_strategy()
            if weather_strategy is None:
                return

            # Get open weather positions (extract data inside session)
            with next(get_db_session()) as session:
                positions = session.query(PositionDB).filter(
                    PositionDB.quantity > 0
                ).all()
                weather_positions = []
                for pos in positions:
                    ticker = str(pos.ticker)
                    upper = ticker.upper()
                    if any(upper.startswith(p) for p in ("KXHIGH", "KXLOWT", "KXLOW", "KXRAIN")) or "SNOWM" in upper:
                        weather_positions.append({
                            "ticker": ticker,
                            "side": str(pos.side or "unknown"),
                            "quantity": int(pos.quantity or 0),
                            "entry_price": int(pos.average_price or 0),
                        })

            if not weather_positions:
                return

            # Group by event (city + date)
            events: Dict[str, List[dict]] = {}
            for wp in weather_positions:
                parsed = weather_strategy._parse_weather_ticker(wp["ticker"])
                if parsed:
                    city = parsed.get("city", "")
                    date_str = parsed.get("date", "")
                    event_key = f"{city}:{date_str}"
                    events.setdefault(event_key, []).append(wp)

            # Check forecasts for each event
            shifts_detected = 0
            reshapes_executed = 0
            reshaper = get_forecast_reshaper()

            for event_key, positions_in_event in events.items():
                city, date_str = event_key.split(":", 1)
                try:
                    # Fetch fresh NWS forecast
                    from src.data_sources.nws_weather import fetch_nws_forecast, KALSHI_STATIONS
                    station_info = KALSHI_STATIONS.get(city, {})
                    station_id = station_info.get("station_id")
                    if not station_id:
                        continue

                    forecast = await asyncio.get_event_loop().run_in_executor(
                        None, fetch_nws_forecast, station_id
                    )
                    if not forecast:
                        continue

                    # Determine the primary forecast value for this event
                    # (use type from first position to determine high vs low)
                    first_parsed = weather_strategy._parse_weather_ticker(
                        positions_in_event[0]["ticker"]
                    )
                    if not first_parsed:
                        continue
                    mkt_type = first_parsed.get("type", "high_temp")
                    if "high" in mkt_type:
                        forecast_val = forecast.get("high_temp")
                    elif "low" in mkt_type:
                        forecast_val = forecast.get("low_temp")
                    else:
                        continue
                    if forecast_val is None:
                        continue

                    # Parse market_date from date_str
                    try:
                        market_date = datetime.strptime(date_str, "%y%b%d").date()
                    except (ValueError, TypeError):
                        try:
                            market_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                        except (ValueError, TypeError):
                            market_date = date.today()

                    # ── Per-position shift alerts (existing behavior) ──
                    for wp in positions_in_event:
                        parsed = weather_strategy._parse_weather_ticker(wp["ticker"])
                        if not parsed:
                            continue
                        threshold = parsed.get("threshold")
                        if threshold is None:
                            continue
                        distance = abs(threshold - forecast_val)
                        if distance <= 2.0:
                            shifts_detected += 1
                            logger.warning(
                                f"\u26a0\ufe0f FORECAST SHIFT: {wp['ticker']} | "
                                f"Threshold={threshold}F, NWS={forecast_val}F, "
                                f"Distance={distance:.1f}F \u2014 re-evaluation triggered"
                            )

                    # ── Forecast reshaper: check if portfolio needs restructuring ──
                    decision = reshaper.check_forecast_shift(
                        city=city,
                        new_forecast=forecast_val,
                        market_date=market_date,
                    )

                    if decision is None or not decision.should_reshape:
                        continue

                    # Build current_positions list for the reshaper
                    current_positions = [
                        {
                            "ticker": wp["ticker"],
                            "side": wp["side"],
                            "quantity": wp["quantity"],
                            "entry_price": wp["entry_price"],
                        }
                        for wp in positions_in_event
                    ]

                    # Generate reshape orders (sells first, then buys)
                    reshape_orders = reshaper.generate_reshape_orders(
                        city=city,
                        old_forecast=decision.old_forecast,
                        new_forecast=decision.new_forecast,
                        current_positions=current_positions,
                        available_brackets=[],  # No new bracket discovery during reshape
                    )

                    if not reshape_orders:
                        logger.info(
                            f"Reshape triggered for {city} but no orders generated"
                        )
                        continue

                    decision.orders = reshape_orders

                    # Cost-benefit gate: benefit must exceed cost x 1.5
                    if not reshaper.should_execute_reshape(decision):
                        logger.info(
                            f"Reshape for {city} blocked by cost-benefit gate "
                            f"(cost=${decision.estimated_total_cost:.2f})"
                        )
                        continue

                    # ── Execute reshape orders: sells first, then buys ──
                    logger.info(
                        f"RESHAPE EXECUTING: {city} | "
                        f"{decision.old_forecast:.0f}F -> {decision.new_forecast:.0f}F | "
                        f"{len(reshape_orders)} orders | "
                        f"est_cost=${decision.estimated_total_cost:.2f}"
                    )

                    for order in reshape_orders:
                        try:
                            # Map reshape action to buy/sell
                            if order.action in (
                                ReshapeAction.SELL_YES,
                                ReshapeAction.SELL_NO,
                                ReshapeAction.TRIM_NO,
                            ):
                                action = "sell"
                            elif order.action in (
                                ReshapeAction.BUY_YES,
                                ReshapeAction.BUY_NO,
                            ):
                                action = "buy"
                            else:
                                continue  # HOLD — no execution needed

                            # Use current market price (0 = market order in paper mode)
                            result = self.executor.execute(
                                ticker=order.ticker,
                                side=order.side,
                                action=action,
                                quantity=order.quantity,
                                price=0,  # Market order
                                skip_risk=False,
                                strategy="reshape",
                            )

                            if result.success:
                                reshapes_executed += 1
                                logger.info(
                                    f"  RESHAPE {action.upper()}: {order.ticker} "
                                    f"{order.side.upper()} x{order.quantity} | "
                                    f"{order.reason}"
                                )
                                self.alert_manager.alert_trade(
                                    ticker=order.ticker,
                                    action=action,
                                    side=order.side,
                                    quantity=order.quantity,
                                    price=0,
                                    trade_type="reshape",
                                )
                            else:
                                logger.warning(
                                    f"  Reshape order failed: {order.ticker} "
                                    f"{action} {order.side} x{order.quantity} | "
                                    f"reason: {getattr(result, 'error', 'unknown')}"
                                )

                        except Exception as order_err:
                            logger.error(
                                f"Reshape order execution error: {order.ticker} | {order_err}"
                            )

                except Exception as e:
                    logger.debug(f"Forecast check failed for {event_key}: {e}")

            if shifts_detected or reshapes_executed:
                logger.info(
                    f"\U0001f321\ufe0f Forecast monitoring: {shifts_detected} shift alerts, "
                    f"{reshapes_executed} reshape trades executed "
                    f"(out of {len(weather_positions)} weather positions)"
                )

        except Exception as e:
            logger.error(f"Forecast update check failed: {e}")

    # ── Observation-Settled Fast Scanner ─────────────────────────────

    async def _run_observation_settled_scan(self) -> int:
        """
        Fast observation-settled scanner: detect dead brackets & execute.

        This runs every 60 seconds (vs 10 min for the full weather scan).
        When a weather station's observed temp surpasses a bracket,
        the bracket is mathematically dead → buy NO for guaranteed profit.

        Uses the obs-settled capital pool (15% of bankroll) with geometric
        50% deployment per signal to prevent pool depletion.

        Returns:
            Number of trades executed
        """
        executed = 0
        try:
            # Get current bankroll for pool calculations
            bankroll = self.risk_manager.get_bankroll()
            if bankroll <= 0:
                bankroll = self._cash_balance_cents / 100.0 if self._cash_balance_cents > 0 else 50.0

            # Log obs pool state every scan
            obs_pool_total = self.risk_manager.get_obs_settled_pool(bankroll) + self.risk_manager._obs_pool_deployed
            obs_pool_available = self.risk_manager.get_obs_settled_pool(bankroll)
            obs_next_deploy = self.risk_manager.get_obs_settled_deploy_amount(bankroll)
            logger.info(
                f"[OBS_POOL] Total: ${obs_pool_total:.2f}, "
                f"Deployed: ${self.risk_manager._obs_pool_deployed:.2f}, "
                f"Available: ${obs_pool_available:.2f}, "
                f"Next deploy: ${obs_next_deploy:.2f}"
            )

            # Check if pool has deployable capital
            if obs_next_deploy <= 0:
                logger.debug("Obs-settled pool depleted, skipping scan")
                return 0

            # Pass bankroll to scanner so it uses pool-aware sizing
            opportunities = self.obs_scanner.scan(bankroll=bankroll)
            if not opportunities:
                return 0

            logger.info(
                f"🔭 Obs scanner found {len(opportunities)} guaranteed opportunities"
            )

            for opp in opportunities:
                # Re-check pool each iteration (geometric decay)
                deploy_amount = self.risk_manager.get_obs_settled_deploy_amount(bankroll)
                if deploy_amount <= 0:
                    logger.info("Obs pool depleted mid-scan, stopping")
                    break

                # Check if we already have a position in this ticker
                try:
                    with next(get_db_session()) as session:
                        existing = session.query(PositionDB).filter(
                            PositionDB.ticker == opp.ticker,
                            PositionDB.quantity > 0,
                        ).first()
                        if existing:
                            logger.debug(
                                f"  Obs scanner: already have position in {opp.ticker}"
                            )
                            continue
                except Exception:
                    pass

                # Calculate position size from pool deploy amount
                cost_per_contract = opp.no_price_cents / 100.0  # dollars
                qty = min(
                    opp.max_contracts,
                    max(1, int(deploy_amount / cost_per_contract)),
                )
                total_cost = qty * cost_per_contract

                if qty < 1:
                    continue

                # Defense-in-depth: fee-adjusted profitability check
                # NO wins when YES resolves to 0 → profit = NO_price - fee
                _obs_profit_cents = opp.no_price_cents - (opp.no_price_cents * 0.02)
                _obs_cost_cents = opp.no_price_cents
                if _obs_profit_cents <= 0 or (total_cost > 0 and opp.guaranteed_profit_cents * qty / total_cost / 100.0 < 0.02):
                    logger.warning(
                        f"  🚫 Obs scanner BLOCKED {opp.ticker} | Fee-destroyed or unviable risk/reward"
                    )
                    continue

                # Execute the trade
                try:
                    order_id = f"obs_{uuid.uuid4().hex[:12]}"
                    net_profit = opp.guaranteed_profit_cents * qty / 100.0

                    logger.info(
                        f"  🎯 EXECUTING obs-settled: {opp.ticker} | "
                        f"NO × {qty} @ {opp.no_price_cents}¢ = ${total_cost:.2f} | "
                        f"guaranteed profit: ${net_profit:.2f} | "
                        f"pool deploy: ${deploy_amount:.2f} | "
                        f"{opp.reason}"
                    )

                    trade_result = await self.executor.execute_trade(
                        ticker=opp.ticker,
                        side="no",
                        quantity=qty,
                        price=opp.no_price_cents,
                        order_id=order_id,
                        strategy="obs_settled",
                    )

                    if trade_result:
                        executed += 1
                        # Record deployment in obs pool (geometric decay)
                        self.risk_manager.record_obs_deploy(total_cost)
                        self.risk_manager.save_obs_pool_state()

                        self.obs_scanner.mark_traded(opp.ticker)
                        self._stats["trades_executed"] = (
                            self._stats.get("trades_executed", 0) + 1
                        )

                        # Log for session stats
                        self.weather_stats.record_trade(
                            city=opp.city,
                            side="no",
                            price_cents=opp.no_price_cents,
                            edge_pct=opp.guaranteed_profit_cents / opp.no_price_cents,
                        )

                        logger.info(
                            f"  ✅ OBS-SETTLED TRADE: {opp.ticker} NO × {qty} "
                            f"@ {opp.no_price_cents}¢ | "
                            f"guaranteed ${net_profit:.2f} profit | "
                            f"pool remaining: ${self.risk_manager.get_obs_settled_pool(bankroll):.2f}"
                        )
                    else:
                        logger.warning(
                            f"  ❌ Obs-settled execution failed: {opp.ticker}"
                        )

                except Exception as e:
                    logger.error(f"Obs-settled trade failed for {opp.ticker}: {e}")

        except Exception as e:
            logger.error(f"Observation scanner cycle failed: {e}")

        return executed

    async def _scan_and_execute_weather(self) -> int:
        """
        Scan weather markets, RANK opportunities, and execute the best trades.

        Ranking system:
        1. Score each opportunity by EV, confidence, timing, and liquidity
        2. Filter out existing positions and sub-threshold edges
        3. Sort by composite score (best first)
        4. Enforce per-cycle budget: spend at most 50% of available cash per cycle
        5. Enforce per-city diversification: max 2 NEW trades per city per cycle
        6. Enforce max trades per cycle: 5 for live, 10 for paper
        """
        executed = 0
        try:
            if self.weather_strategy is None:
                return 0

            opportunities = self.weather_strategy.get_tradeable_opportunities()

            if not opportunities:
                logger.debug("No weather trading opportunities found")
                self._latest_weather_opportunities = []
                return 0

            # Store for opportunity cost exit comparison
            self._latest_weather_opportunities = opportunities

            logger.info(f"Weather scan: {len(opportunities)} raw opportunities")

            # ── Configuration (phase-aware) ──
            phase_limits = self.risk_manager.get_phase_limits()
            MAX_TRADES_PER_CYCLE = int(os.environ.get("WEATHER_MAX_TRADES_PER_CYCLE", "24"))  # 6 events × ~4 brackets each
            MAX_TRADES_PER_CITY = int(os.environ.get("WEATHER_MAX_TRADES_PER_CITY", "7"))  # ~6-7 brackets per event (high or low)
            CYCLE_BUDGET_PCT = 0.30        # Spend at most 30% of cash per cycle (was 50%)
            MIN_EDGE = 0.05                # 5% minimum POSITIVE directional edge

            # Phase-aware position cap: don't exceed max_positions from bankroll phase
            # Only count positions on markets that haven't closed yet (exclude
            # near-settled positions that are just waiting for resolution)
            max_phase_positions = phase_limits.get("max_positions", 12)
            try:
                with next(get_db_session()) as session:
                    now = datetime.now(timezone.utc)
                    # Count positions where the market is still open
                    active_positions = (
                        session.query(PositionDB)
                        .join(MarketDB, PositionDB.ticker == MarketDB.ticker)
                        .filter(
                            PositionDB.quantity > 0,
                            MarketDB.close_time > now,
                        )
                        .count()
                    )
                    # Fallback: also count positions with no matching market record
                    orphan_positions = (
                        session.query(PositionDB)
                        .filter(
                            PositionDB.quantity > 0,
                            ~PositionDB.ticker.in_(
                                session.query(MarketDB.ticker)
                            ),
                        )
                        .count()
                    )
                    current_position_count = active_positions + orphan_positions
                if current_position_count >= max_phase_positions:
                    logger.info(
                        f"Phase position cap: {current_position_count}/{max_phase_positions} "
                        f"positions ({phase_limits['phase']} phase) — skip entry"
                    )
                    return 0
            except Exception:
                pass

            # ── Bracket Portfolio Role Lookup ──
            # Build CityBracketPortfolios to classify each ticker by its role
            # (far_no, medium_no, forecast_yes, near_yes_hedge) so we can enforce
            # proper bracket spread: 2-5 contracts per bracket across multiple brackets
            # instead of concentrating on one high-scored bracket.
            bracket_roles = {}  # ticker -> BracketRole
            try:
                from src.strategy.city_bracket_portfolio import BracketRole
                city_portfolios = self.weather_strategy.build_city_portfolios()
                for portfolio in city_portfolios:
                    for bv in portfolio.brackets:
                        bracket_roles[bv.ticker] = bv.role
                if bracket_roles:
                    logger.info(
                        f"📊 Bracket roles mapped: {len(bracket_roles)} tickers "
                        f"across {len(city_portfolios)} city portfolios"
                    )
            except Exception as _br_err:
                logger.debug(f"Bracket portfolio role lookup failed (using defaults): {_br_err}")

            # ── Phase 1: Score all opportunities ──
            scored = []
            skipped_edge = 0
            skipped_filter = 0

            for opp in opportunities:
                self.weather_stats.record_forecast(opp.city)

                # Log forecast snapshot for audit trail
                try:
                    _hours = 0.0
                    with next(get_db_session()) as _snap_sess:
                        _mkt = _snap_sess.query(MarketDB).filter(
                            MarketDB.ticker == opp.ticker
                        ).first()
                        if _mkt and _mkt.close_time:
                            _ct = _mkt.close_time
                            if _ct.tzinfo is None:
                                _ct = _ct.replace(tzinfo=timezone.utc)
                            _hours = max(0.0, (_ct - datetime.now(timezone.utc)).total_seconds() / 3600.0)
                    self.forecast_audit.log_snapshot(
                        city=opp.city,
                        market_date=opp.market_date,
                        forecast_temp=float(opp.nws_forecast_temp),
                        nws_confidence=opp.nws_confidence,
                        source="nws",
                        hours_to_close=_hours,
                    )
                except Exception:
                    pass

                # Edge threshold — must be POSITIVE directional edge
                # (edge should already be positive from weather_strategy,
                #  but this is a safety gate)
                if opp.edge < MIN_EDGE:
                    skipped_edge += 1
                    self.weather_stats.record_skip()
                    continue

                self.weather_stats.record_opportunity(opp.edge)

                # Score the opportunity
                result = self._score_opportunity(opp)
                if result is None:
                    skipped_filter += 1
                    continue

                # Deduplication — control re-entry on tickers we already hold.
                # Allow ADDING contracts on the SAME side (role limits cap qty later).
                # Block entry on the OPPOSITE side (must exit first).
                # Block re-entry if there's an unresolved same-cycle buy.
                with next(get_db_session()) as session:
                    # Determine what side this opportunity wants to trade
                    _opp_side = (
                        "yes" if opp.recommendation == "BUY_YES"
                        else "no" if opp.recommendation == "BUY_NO"
                        else "unknown"
                    )

                    # Primary: do we already HOLD this position?
                    existing_position = session.query(PositionDB).filter(
                        PositionDB.ticker == opp.ticker,
                        PositionDB.quantity > 0,
                    ).first()
                    if existing_position:
                        _held_side = str(existing_position.side or "").lower()
                        if _held_side != _opp_side:
                            # Opposite side — can't buy YES if holding NO (or vice versa)
                            skipped_filter += 1
                            continue
                        # Same side — allow adding (role limits will cap quantity)

                    # Secondary: is there an unresolved buy trade (catches same-cycle)?
                    # Exclude api_sync trades — these are bookkeeping entries from
                    # position sync, not real pending orders. The position dedup
                    # above already handles existing-position logic correctly.
                    if self.paper_trading:
                        existing_trade = session.query(TradeDB).filter(
                            TradeDB.ticker == opp.ticker,
                            TradeDB.action == "buy",
                            TradeDB.resolved == 0,
                            TradeDB.strategy != "api_sync",
                        ).count()
                    else:
                        existing_trade = session.query(TradeDB).filter(
                            TradeDB.ticker == opp.ticker,
                            TradeDB.action == "buy",
                            TradeDB.resolved == 0,
                            TradeDB.status.notin_(["simulated", "resolved_cleanup"]),
                            TradeDB.strategy != "api_sync",
                        ).count()
                    if existing_trade > 0:
                        skipped_filter += 1
                        continue

                scored.append(result)

            if not scored:
                logger.info(
                    f"No tradeable opportunities after filtering "
                    f"(edge_skip={skipped_edge}, filter_skip={skipped_filter})"
                )
                return 0

            # ── Phase 2: Rank by outside-in bracket role tiers ──
            # Plan V2 outside-in ordering: execute furthest NOs first (backbone
            # income), then medium NOs, forecast YES, and near-YES hedge last.
            # Within each tier, sort by composite score (highest first).
            # Opportunities without a bracket role fall back to trade_type:
            # no_exclusion → tier 0, everything else → tier 2.
            try:
                from src.strategy.city_bracket_portfolio import BracketRole
                _ROLE_TIER = {
                    BracketRole.VERY_FAR_NO: 0,
                    BracketRole.FAR_NO: 0,
                    BracketRole.MEDIUM_NO: 1,
                    BracketRole.FORECAST_YES: 2,
                    BracketRole.NEAR_YES_HEDGE: 3,
                }
            except ImportError:
                _ROLE_TIER = {}

            def _entry_tier(s: dict) -> int:
                role = bracket_roles.get(s["opp"].ticker)
                if role is not None and role in _ROLE_TIER:
                    return _ROLE_TIER[role]
                # Fallback: no_exclusion → tier 0, other → tier 2
                if getattr(s["opp"], "trade_type", "") == "no_exclusion":
                    return 0
                return 2

            scored.sort(key=lambda x: (_entry_tier(x), -x["score"]))

            # Count per tier for logging
            _tier_counts = {}
            for s in scored:
                t = _entry_tier(s)
                _tier_counts[t] = _tier_counts.get(t, 0) + 1

            # Log the ranked list
            _tier_names = {0: "far_no", 1: "medium_no", 2: "yes", 3: "hedge"}
            _tier_summary = ", ".join(
                f"{_tier_counts.get(t, 0)} {_tier_names.get(t, '?')}"
                for t in sorted(_tier_names)
            )
            logger.info(
                f"Ranked {len(scored)} opportunities "
                f"({_tier_summary}) "
                f"(skipped: {skipped_edge} edge, {skipped_filter} filter)"
            )
            for i, s in enumerate(scored[:10]):
                opp = s["opp"]
                _tt = getattr(opp, "trade_type", "unknown")
                logger.info(
                    f"  #{i+1} {opp.ticker} | "
                    f"Score: {s['score']:.4f} | "
                    f"Type: {_tt} | "
                    f"Edge: {opp.edge*100:.1f}% | "
                    f"Kelly: {s['half_kelly']:.1%} | "
                    f"Conf: {s['confidence_mult']:.0%} | "
                    f"Time: {'+' if s['days_out'] <= 0 else ''}{s['days_out']}d | "
                    f"Liq: {s['liquidity_bonus']:.0%} | "
                    f"{opp.city}"
                )

            # ── Phase 3: Execute via city-based budget allocation ──
            # Per-trade contract caps: min of env override and phase limit.
            # In SURVIVAL (5), a single trade can never exceed 5 contracts.
            _per_trade_phase_cap = phase_limits.get("max_contracts_per_ticker", 5)
            NO_MAX_CONTRACTS = min(
                int(os.environ.get("WEATHER_NO_MAX_CONTRACTS", "20")),
                _per_trade_phase_cap,
            )
            YES_MAX_CONTRACTS = min(
                int(os.environ.get("WEATHER_YES_MAX_CONTRACTS", "15")),
                _per_trade_phase_cap,
            )

            if self.paper_trading:
                weather_bankroll = 10000 * 0.25
                total_portfolio = weather_bankroll
            else:
                weather_bankroll = self.risk_manager.get_bankroll()
                total_portfolio = self._total_portfolio_dollars or weather_bankroll

            # Exclude obs-settled pool reserve (15%) from forecast-based capital
            available_cash = self.risk_manager.get_forecast_budget(weather_bankroll)

            # Group scored opportunities by city EVENT (high vs low get separate budgets).
            # "NYC_HIGH" and "NYC_LOW" each get their own $8 allocation so bracket
            # portfolios aren't starved by sharing one pool across both directions.
            city_opps: Dict[str, list] = {}
            city_best_edge: Dict[str, float] = {}
            for s in scored:
                _opp = s["opp"]
                _event_suffix = "HIGH" if getattr(_opp, "threshold_type", "") == "above" else "LOW"
                city_event = f"{_opp.city}_{_event_suffix}"
                if city_event not in city_opps:
                    city_opps[city_event] = []
                city_opps[city_event].append(s)
                edge = _opp.edge
                if edge > city_best_edge.get(city_event, 0.0):
                    city_best_edge[city_event] = edge

            # Allocate budget per city event — weighted by best edge
            city_allocations = self.city_budget_allocator.allocate_by_edge(
                city_edges=city_best_edge,
                total_portfolio=total_portfolio,
                available_cash=available_cash,
            )

            budget_spent = 0.0
            city_trades_this_cycle = {}  # city -> count of new trades
            city_no_spent = {}  # city -> dollars spent on NO trades (for YES budget reserve)

            # ── YES Budget Reserve ──
            # Reserve 30% of each city's budget for YES positions (forecast YES +
            # near-YES hedge). Without this, NO-first ranking consumes the entire
            # city budget on far NOs, leaving nothing for YES-side convergence.
            # This matches PLAN v2 allocation: 70% NOs (50% far + 20% medium) + 30% YES (15% forecast + 15% hedge).
            YES_RESERVE_PCT = float(os.environ.get("WEATHER_YES_RESERVE_PCT", "0.30"))

            for city_event, allocation in city_allocations.items():
                if city_event not in city_opps:
                    continue

                # Within each city event (e.g., NYC_HIGH, MIAMI_LOW), execute ranked opportunities
                city_scored = city_opps[city_event]
                # Already sorted globally; maintain that order within city event

                for rank, s in enumerate(city_scored):
                    # Global stop conditions
                    if executed >= MAX_TRADES_PER_CYCLE:
                        logger.info(
                            f"Hit max trades per cycle ({MAX_TRADES_PER_CYCLE}), "
                            f"saving remaining for next cycle"
                        )
                        break

                    opp = s["opp"]
                    side = s["side"]
                    entry_price = s["entry_price"]
                    price_cents = s["price_cents"]
                    half_kelly = s["half_kelly"]
                    trade_type = getattr(opp, "trade_type", "unknown")

                    # Anti-loop check: don't re-enter recently exited tickers
                    can_enter, skip_reason = self.anti_loop_detector.can_re_enter(opp.ticker)
                    if not can_enter:
                        logger.debug(
                            f"  SKIP {opp.ticker} | Anti-loop: {skip_reason}"
                        )
                        continue

                    # ── ROLE-SIDE AGREEMENT GATE ──
                    # Bracket role is the authority on which side to trade.
                    # If weather_strategy says YES but bracket portfolio says NO
                    # (or vice versa), skip — the edge was calculated for the
                    # wrong side and executing it would be a wrong-side trade.
                    _ticker_role = bracket_roles.get(opp.ticker)
                    if _ticker_role is not None:
                        try:
                            from src.strategy.city_bracket_portfolio import BracketRole
                            _NO_ROLES = (
                                BracketRole.FAR_NO,
                                BracketRole.VERY_FAR_NO,
                                BracketRole.MEDIUM_NO,
                            )
                            _YES_ROLES = (
                                BracketRole.FORECAST_YES,
                                BracketRole.NEAR_YES_HEDGE,
                            )
                            _role_expected_side = (
                                "no" if _ticker_role in _NO_ROLES
                                else "yes" if _ticker_role in _YES_ROLES
                                else None
                            )
                            if _role_expected_side and _role_expected_side != side:
                                logger.warning(
                                    f"  ⚠️ ROLE-SIDE MISMATCH: {opp.ticker} | "
                                    f"bracket_role={_ticker_role.value} expects "
                                    f"{_role_expected_side} but trade_type="
                                    f"{trade_type} recommends {side} — SKIPPING"
                                )
                                continue
                        except Exception:
                            pass  # If import fails, skip gate

                    # ── YES BUDGET RESERVE: Prevent NOs from consuming entire event budget ──
                    # When a NO trade would push past the NO budget limit (70% of event budget),
                    # skip it to reserve remaining budget for YES positions (forecast + hedge).
                    if side == "no" and YES_RESERVE_PCT > 0:
                        no_budget_limit = allocation.budget_dollars * (1.0 - YES_RESERVE_PCT)
                        no_spent_so_far = city_no_spent.get(city_event, 0.0)
                        if no_spent_so_far >= no_budget_limit:
                            logger.info(
                                f"  📊 YES RESERVE: {opp.ticker} | NO budget for {city_event} "
                                f"exhausted (${no_spent_so_far:.2f}/${no_budget_limit:.2f}), "
                                f"reserving ${allocation.budget_dollars * YES_RESERVE_PCT:.2f} for YES"
                            )
                            continue

                    # ── SAFETY GATE 1: Hard directional edge floor ──
                    # This is the FINAL check before execution — edge MUST be positive
                    HARD_MIN_EDGE = 0.05  # 5% minimum directional edge
                    if opp.edge < HARD_MIN_EDGE:
                        logger.warning(
                            f"  \U0001f6ab BLOCKED {opp.ticker} | Edge {opp.edge:.1%} below "
                            f"hard floor {HARD_MIN_EDGE:.0%} \u2014 REFUSING TO TRADE"
                        )
                        continue

                    # ── SAFETY GATE 2: Time-to-close gate ──
                    # Don't enter positions too close to market close
                    _hours_to_close = None  # initialized for use in Gate 3 tiered ceiling
                    try:
                        from src.data.models import MarketDB
                        with next(get_db_session()) as _gate_sess:
                            _mkt = _gate_sess.query(MarketDB).filter(
                                MarketDB.ticker == opp.ticker
                            ).first()
                            if _mkt and _mkt.close_time:
                                from datetime import datetime, timezone
                                _now = datetime.now(timezone.utc)
                                _close = _mkt.close_time
                                if _close.tzinfo is None:
                                    _close = _close.replace(tzinfo=timezone.utc)
                                _hours_to_close = (_close - _now).total_seconds() / 3600.0
                                if _hours_to_close < 2.0:
                                    logger.info(
                                        f"  \u23f0 SKIP {opp.ticker} | {_hours_to_close:.1f}h to close "
                                        f"\u2014 too close to resolution, refusing entry"
                                    )
                                    continue
                                elif _hours_to_close < 4.0 and opp.edge < 0.10:
                                    logger.info(
                                        f"  \u23f0 SKIP {opp.ticker} | {_hours_to_close:.1f}h to close "
                                        f"with only {opp.edge:.1%} edge \u2014 need >10% edge within 4h"
                                    )
                                    continue
                    except Exception as _gate_err:
                        logger.debug(f"Time gate check failed for {opp.ticker}: {_gate_err}")

                    # ── SAFETY GATE 3: Contract price floor & tiered YES ceiling ──
                    # NO:  buy at up to 90¢.  Above 90¢ the 2% winner fee eats
                    #      almost all profit (94¢ → 4¢ profit, one loss = 24 wins).
                    # YES: tiered ceiling based on edge strength + time-to-close:
                    #   Default  → 25¢  (low-conviction or unknown time horizon)
                    #   Edge≥15% AND <6h  → 45¢  (high-conviction, imminent resolution)
                    #   Edge≥30% AND <24h → 45¢  (very high edge, same-day)
                    #   Edge≥50% AND <48h → 50¢  (extreme conviction, within 2 days)
                    #   Hard cap: NEVER buy YES above 50¢ — risk/reward not worth it.
                    MIN_CONTRACT_PRICE = int(os.environ.get("WEATHER_MIN_PRICE", "5"))
                    NO_PRICE_CEILING = int(os.environ.get("WEATHER_NO_CEILING", "90"))
                    YES_DEFAULT_CEILING = int(os.environ.get("WEATHER_YES_CEILING", "25"))
                    YES_HARD_CAP = int(os.environ.get("WEATHER_YES_HARD_CAP", "50"))

                    if price_cents < MIN_CONTRACT_PRICE:
                        logger.info(
                            f"  \U0001f6ab SKIP {opp.ticker} | Price {price_cents}\u00a2 "
                            f"below floor {MIN_CONTRACT_PRICE}\u00a2 \u2014 too illiquid"
                        )
                        continue

                    if side == "yes":
                        _effective_yes_ceiling = YES_DEFAULT_CEILING
                        _tier_reason = "default"
                        if _hours_to_close is not None:
                            if opp.edge >= 0.50 and _hours_to_close < 48.0:
                                _effective_yes_ceiling = YES_HARD_CAP  # 50¢
                                _tier_reason = f"edge≥50%+<48h"
                            elif opp.edge >= 0.30 and _hours_to_close < 24.0:
                                _effective_yes_ceiling = 45
                                _tier_reason = f"edge≥30%+<24h"
                            elif opp.edge >= 0.15 and _hours_to_close < 6.0:
                                _effective_yes_ceiling = 45
                                _tier_reason = f"edge≥15%+<6h"
                        # Hard cap safety: never exceed YES_HARD_CAP
                        _effective_yes_ceiling = min(_effective_yes_ceiling, YES_HARD_CAP)

                        if price_cents > _effective_yes_ceiling:
                            _htc_str = f", {_hours_to_close:.1f}h to close" if _hours_to_close is not None else ""
                            logger.info(
                                f"  \U0001f6ab SKIP {opp.ticker} | YES at {price_cents}\u00a2 "
                                f"above ceiling {_effective_yes_ceiling}\u00a2 "
                                f"[tier={_tier_reason}, edge={opp.edge:.1%}{_htc_str}]"
                            )
                            continue
                        elif _effective_yes_ceiling > YES_DEFAULT_CEILING:
                            logger.info(
                                f"  \u2b06\ufe0f TIER UP {opp.ticker} | YES at {price_cents}\u00a2 "
                                f"allowed — ceiling raised to {_effective_yes_ceiling}\u00a2 "
                                f"[tier={_tier_reason}, edge={opp.edge:.1%}, "
                                f"{_hours_to_close:.1f}h to close]"
                            )

                    if side == "no" and price_cents > NO_PRICE_CEILING:
                        logger.info(
                            f"  \U0001f6ab SKIP {opp.ticker} | NO at {price_cents}\u00a2 "
                            f"above ceiling {NO_PRICE_CEILING}\u00a2 \u2014 fee-destroyed, 1 loss wipes ~24 wins"
                        )
                        continue

                    # ── SAFETY GATE 4a: Universal monthly market block ──
                    # Monthly markets tie up capital for weeks for tiny returns.
                    # Not part of our bracket strategy. Block regardless of market_type.
                    # Detects: KXDALSNOWM-26FEB-2.0, KXRAINNYCM-26FEB-3, etc.
                    # Snow monthly: "SNOWM" in ticker (KX{CITY}SNOWM-...)
                    # Rain monthly: "KXRAIN{CITY}M-" pattern (city between RAIN and M)
                    import re as _re_monthly
                    _ticker_upper_monthly = opp.ticker.upper()
                    _is_monthly_market = (
                        "SNOWM" in _ticker_upper_monthly
                        or bool(_re_monthly.search(r'KXRAIN\w{2,5}M-', _ticker_upper_monthly))
                    )
                    if _is_monthly_market:
                        logger.info(
                            f"  \U0001f6ab SKIP {opp.ticker} | Monthly market blocked \u2014 "
                            f"ties up capital for weeks with minimal return"
                        )
                        continue

                    # ── SAFETY GATE 4b: Rain/snow activation rules ──
                    # Rain and snow markets require higher edge (12%)
                    _mtype = getattr(opp, "market_type", "temperature")
                    if _mtype in ("rain", "snow"):
                        RAIN_SNOW_MIN_EDGE = 0.12  # 12% edge minimum
                        if opp.edge < RAIN_SNOW_MIN_EDGE:
                            logger.info(
                                f"  \U0001f6ab SKIP {opp.ticker} | {_mtype} market edge "
                                f"{opp.edge:.1%} < {RAIN_SNOW_MIN_EDGE:.0%} minimum"
                            )
                            continue

                    # ── SAFETY GATE 5: YES position cap (per market event) ──
                    # Cheap YES bets (≤15¢) get a higher cap because they're
                    # capital-efficient with asymmetric payoff.
                    # Pricier YES bets keep the tighter cap to limit exposure.
                    MAX_YES_PER_MARKET = int(os.environ.get("WEATHER_MAX_YES_PER_MARKET", "2"))
                    MAX_YES_CHEAP = int(os.environ.get("WEATHER_MAX_YES_CHEAP", "4"))
                    CHEAP_YES_THRESHOLD_CENTS = 15
                    _yes_cap = MAX_YES_CHEAP if price_cents <= CHEAP_YES_THRESHOLD_CENTS else MAX_YES_PER_MARKET
                    if side == "yes":
                        try:
                            import re as _re_cap
                            # Extract event prefix: everything before the last -B or -T segment
                            _event_match = _re_cap.match(r'^(.+?)-(B|T)[\d.]+$', opp.ticker.upper())
                            _event_prefix = _event_match.group(1) if _event_match else opp.ticker.upper()

                            with next(get_db_session()) as _cap_sess:
                                # Count YES positions for THIS market event only
                                active_yes_this_market = _cap_sess.query(PositionDB).filter(
                                    PositionDB.side == "yes",
                                    PositionDB.quantity > 0,
                                    PositionDB.ticker.like(f"{_event_prefix}%"),
                                ).count()
                                if active_yes_this_market >= _yes_cap:
                                    logger.info(
                                        f"  \U0001f6ab SKIP {opp.ticker} | YES cap for {_event_prefix} reached "
                                        f"({active_yes_this_market}/{_yes_cap}"
                                        f"{', cheap cap' if price_cents <= CHEAP_YES_THRESHOLD_CENTS else ''}"
                                        f") \u2014 NO-side only for this market"
                                    )
                                    continue
                        except Exception as _cap_err:
                            logger.debug(f"YES cap check failed: {_cap_err}")

                    # Per-type contract limits
                    if trade_type == "no_exclusion":
                        max_contracts = NO_MAX_CONTRACTS
                    else:
                        max_contracts = YES_MAX_CONTRACTS

                    # Position sizing: half-Kelly capped by city budget
                    # Successful traders risk <$1 per position. Hard cap at $1.00
                    # to match proven micro-bet strategy.
                    MAX_POSITION_DOLLARS = float(os.environ.get("WEATHER_MAX_POSITION_DOLLARS", "2.00"))
                    position_dollars = min(
                        MAX_POSITION_DOLLARS,                # Hard cap per position ($5)
                        available_cash * 0.15,               # Max 15% of cash per trade
                        weather_bankroll * half_kelly,        # Half-Kelly sizing
                        allocation.remaining_dollars,         # City budget remaining
                    )

                    # High-price NO risk reduction: at >80¢ per contract,
                    # risk/reward is very poor (risking 80¢+ to profit <20¢ minus fees).
                    # Cap total cost for high-price NOs at $2.00 regardless of other limits.
                    HIGH_PRICE_NO_THRESHOLD = 80  # cents
                    HIGH_PRICE_NO_MAX_DOLLARS = 2.00
                    if side == "no" and price_cents >= HIGH_PRICE_NO_THRESHOLD:
                        position_dollars = min(position_dollars, HIGH_PRICE_NO_MAX_DOLLARS)

                    if position_dollars < entry_price:
                        logger.debug(
                            f"  SKIP {opp.ticker} | City budget exhausted "
                            f"({city}: ${allocation.remaining_dollars:.2f} remaining)"
                        )
                        continue

                    contracts = max(1, int(position_dollars / entry_price))
                    contracts = min(contracts, max_contracts)

                    # ── SAFETY GATE 6: Per-ticker contract cap ──
                    # Defense-in-depth: cap total contracts on any single ticker
                    # to prevent concentration (e.g., 38 contracts on one 97¢ NO).
                    # Phase-aware: SURVIVAL=10, ACCELERATION=15, SCALING=25.
                    TICKER_CONTRACT_CAP = phase_limits.get("max_contracts_per_ticker", 10)
                    try:
                        with next(get_db_session()) as _tc_sess:
                            existing_qty = 0
                            _existing_pos = _tc_sess.query(PositionDB).filter(
                                PositionDB.ticker == opp.ticker,
                                PositionDB.quantity > 0,
                            ).first()
                            if _existing_pos:
                                existing_qty = int(_existing_pos.quantity or 0)
                            # Also count pending buy trades not yet synced to PositionDB
                            _pending_buys = _tc_sess.query(TradeDB).filter(
                                TradeDB.ticker == opp.ticker,
                                TradeDB.action == "buy",
                                TradeDB.resolved == 0,
                            ).all()
                            for _pb in _pending_buys:
                                existing_qty += int(_pb.quantity or 0)

                            room = max(0, TICKER_CONTRACT_CAP - existing_qty)
                            if room == 0:
                                logger.info(
                                    f"  \U0001f6d1 SKIP {opp.ticker} | Per-ticker cap: "
                                    f"{existing_qty}/{TICKER_CONTRACT_CAP} already held"
                                )
                                continue
                            if contracts > room:
                                logger.info(
                                    f"  \U0001f4c9 CAP {opp.ticker} | {contracts} \u2192 {room} contracts "
                                    f"(holding {existing_qty}, cap {TICKER_CONTRACT_CAP})"
                                )
                                contracts = room
                    except Exception as _tc_err:
                        logger.debug(f"Ticker cap check failed: {_tc_err}")

                    # ── SAFETY GATE 7: Role-based bracket contract limit ──
                    # Uses CityBracketPortfolio roles to enforce per-bracket spread:
                    # Far NOs: max 5, Medium NOs: max 3, Forecast YES: max 5, Near YES hedge: max 3.
                    # Accounts for existing position quantity so same-side adds
                    # don't exceed the role limit.
                    _ticker_role = bracket_roles.get(opp.ticker)
                    if _ticker_role is not None:
                        try:
                            from src.strategy.city_bracket_portfolio import BracketRole
                            _ROLE_CONTRACT_LIMITS = {
                                BracketRole.VERY_FAR_NO: 5,
                                BracketRole.FAR_NO: 5,
                                BracketRole.MEDIUM_NO: 5,
                                BracketRole.FORECAST_YES: 5,
                                BracketRole.NEAR_YES_HEDGE: 3,
                            }
                            _role_limit = _ROLE_CONTRACT_LIMITS.get(_ticker_role, 5)
                            _role_room = max(0, _role_limit - existing_qty)
                            if _role_room == 0:
                                logger.info(
                                    f"  📊 BRACKET SPREAD: {opp.ticker} | "
                                    f"role {_ticker_role.value} cap reached "
                                    f"({existing_qty}/{_role_limit} held)"
                                )
                                continue
                            if contracts > _role_room:
                                logger.info(
                                    f"  📊 BRACKET SPREAD: {opp.ticker} | "
                                    f"{contracts} → {_role_room} contracts "
                                    f"(role: {_ticker_role.value}, "
                                    f"holding {existing_qty}/{_role_limit})"
                                )
                                contracts = _role_room
                        except Exception:
                            pass  # If role import fails, fall through to phase cap

                    trade_cost = contracts * entry_price  # in dollars

                    # Log the trade we're about to execute
                    _role_str = _ticker_role.value if _ticker_role else "unclassified"
                    logger.info(
                        f"\U0001f321\ufe0f RANKED #{rank+1}: {opp.ticker}\n"
                        f"  Type: {getattr(opp, 'trade_type', 'unknown')} | "
                        f"Role: {_role_str} | "
                        f"Side: {opp.recommendation} | "
                        f"Score: {s['score']:.4f} | "
                        f"NWS: {opp.nws_forecast_temp}\u00b0F | "
                        f"Market: {price_cents}\u00a2 | "
                        f"Prob: {opp.our_probability*100:.0f}% | "
                        f"Edge: {opp.edge*100:.1f}% | "
                        f"Kelly: {half_kelly:.1%} | "
                        f"Qty: {contracts} | "
                        f"Cost: ${trade_cost:.2f}"
                    )

                    # ── SAFETY GATE 8: Fee-adjusted profitability check ──
                    # Defense-in-depth: even if all upstream filters fail,
                    # never execute a trade that can't profit after fees.
                    # At 99¢ YES: profit if win = 1¢, fee = 2% × 1¢ = 0.02¢ → loss.
                    # At 99¢ NO (1¢ YES): profit if win = 99¢, fee = 2% × 99¢ → fine.
                    KALSHI_FEE_RATE = 0.02
                    if side == "yes":
                        _profit_if_win = (100 - price_cents) / 100.0  # $/contract
                    else:
                        _profit_if_win = price_cents / 100.0  # NO wins = YES at 0
                    _fee_per_contract = KALSHI_FEE_RATE * _profit_if_win
                    _net_profit_per_contract = _profit_if_win - _fee_per_contract
                    _total_cost = contracts * entry_price
                    if _net_profit_per_contract <= 0:
                        logger.warning(
                            f"  🚫 BLOCKED {opp.ticker} | Fee-destroyed: "
                            f"profit {_profit_if_win*100:.1f}¢ - fee {_fee_per_contract*100:.2f}¢ "
                            f"= {_net_profit_per_contract*100:.2f}¢/contract"
                        )
                        continue
                    # Also reject if total cost far exceeds potential net profit
                    _max_net_profit = _net_profit_per_contract * contracts
                    if _total_cost > 0 and _max_net_profit / _total_cost < 0.02:
                        logger.warning(
                            f"  🚫 BLOCKED {opp.ticker} | Risk/reward unviable: "
                            f"cost ${_total_cost:.2f} for max net profit "
                            f"${_max_net_profit:.2f} ({_max_net_profit/_total_cost:.1%} return)"
                        )
                        continue

                    # ── PRE-EXECUTION EDGE VERIFICATION ──
                    # Check if edge still exists at current prices before committing.
                    # Prices can move between signal generation and execution.
                    edge_still_valid, current_edge = self.verify_edge_before_execution(
                        opp=opp,
                        side=side,
                        original_edge=opp.edge,
                    )
                    if not edge_still_valid:
                        logger.info(
                            f"PRE-EXEC CHECK: Edge closed for {opp.ticker} "
                            f"({opp.edge:.1%} -> {current_edge:.1%}), skipping"
                        )
                        continue

                    try:
                        order_id = f"WEATHER-{uuid.uuid4().hex[:8].upper()}"

                        # Map trade_type to strategy string for exit engine routing
                        if trade_type == "no_exclusion":
                            db_strategy = "weather_no_hold"
                        elif trade_type == "yes_convergence":
                            db_strategy = "weather_yes_convergence"
                        else:
                            db_strategy = "weather"

                        if self.paper_trading:
                            with next(get_db_session()) as session:
                                trade = TradeDB(
                                    order_id=order_id,
                                    fill_id=f"FILL-{order_id}",
                                    ticker=opp.ticker,
                                    side=side,
                                    action="buy",
                                    quantity=contracts,
                                    price=price_cents,
                                    fee=round(contracts * KALSHI_WINNER_FEE_RATE * (100 - price_cents) / 100, 2),
                                    status="simulated",
                                    timestamp=datetime.now(timezone.utc),
                                    strategy=db_strategy,
                                    edge=opp.edge,
                                )
                                session.add(trade)
                                session.commit()

                            # Update position tracking for exit reevaluator
                            try:
                                with next(get_db_session()) as session:
                                    position = session.query(PositionDB).filter_by(ticker=opp.ticker).first()
                                    if position:
                                        old_qty = int(position.quantity or 0)
                                        old_avg = int(position.average_price or 0)
                                        new_qty = old_qty + contracts
                                        new_avg = (old_qty * old_avg + contracts * price_cents) / new_qty if new_qty > 0 else 0
                                        position.quantity = new_qty
                                        position.average_price = int(new_avg)
                                        position.side = side
                                    else:
                                        position = PositionDB(
                                            ticker=opp.ticker,
                                            side=side,
                                            quantity=contracts,
                                            average_price=price_cents,
                                        )
                                        session.add(position)
                                    session.commit()
                            except Exception as e:
                                logger.error(f"Failed to update position for weather trade {opp.ticker}: {e}")

                            executed += 1
                            budget_spent += trade_cost
                            allocation.spend(trade_cost)
                            city_trades_this_cycle[city_event] = city_trades_this_cycle.get(city_event, 0) + 1
                            self.anti_loop_detector.record_entry(opp.ticker, side)
                            self._record_trade("weather", price_cents)
                            self.weather_stats.record_trade(price_cents, opp.edge, opp.city)
                            # Track NO spending for YES budget reserve
                            if side == "no":
                                city_no_spent[city_event] = city_no_spent.get(city_event, 0.0) + trade_cost

                            logger.info(
                                f"\u2705 WEATHER TRADE #{executed}: BUY {side.upper()} @ {price_cents}\u00a2 | "
                                f"{opp.ticker} | {trade_type} | {_role_str} | Qty: {contracts} | "
                                f"Edge: {opp.edge:.1%} | "
                                f"Event: {city_event} ${allocation.total_spent:.2f}/${allocation.budget_dollars:.2f} | "
                                f"Total: ${budget_spent:.2f}",
                            )

                            # Discord alert → #trades
                            self.alert_manager.alert_trade(
                                ticker=opp.ticker,
                                action="buy",
                                side=side,
                                quantity=contracts,
                                price=price_cents,
                                edge=opp.edge,
                                trade_type=trade_type,
                                bracket_role=_role_str,
                                city_event=city_event,
                                budget_spent=allocation.total_spent,
                                budget_total=allocation.budget_dollars,
                            )

                            # Place resting limit sell at model EV (passive profit-taking)
                            try:
                                if side == "no":
                                    target_exit = int((1.0 - opp.our_probability) * 100) - 2
                                else:
                                    target_exit = int(opp.our_probability * 100) - 2
                                if target_exit > price_cents:
                                    import asyncio
                                    asyncio.get_event_loop().create_task(
                                        self.resting_order_manager.place_profit_target(
                                            ticker=opp.ticker,
                                            side=side,
                                            quantity=contracts,
                                            target_price=target_exit,
                                            entry_price=price_cents,
                                        )
                                    )
                            except Exception as _pt_err:
                                logger.debug(f"Profit target placement failed: {_pt_err}")

                        else:
                            result = self.executor.execute(
                                ticker=opp.ticker,
                                side=side,
                                action="buy",
                                quantity=contracts,
                                price=price_cents,
                                skip_risk=False,  # FIXED: let risk manager do its job
                                strategy=db_strategy,
                                edge=opp.edge,
                            )
                            if result.success:
                                executed += 1
                                budget_spent += trade_cost
                                allocation.spend(trade_cost)
                                city_trades_this_cycle[city_event] = city_trades_this_cycle.get(city_event, 0) + 1
                                self.anti_loop_detector.record_entry(opp.ticker, side)
                                self._stats["trades_executed"] += 1
                                # Track NO spending for YES budget reserve
                                if side == "no":
                                    city_no_spent[city_event] = city_no_spent.get(city_event, 0.0) + trade_cost

                                logger.info(
                                    f"\u2705 WEATHER TRADE #{executed}: BUY {side.upper()} @ {price_cents}\u00a2 | "
                                    f"{opp.ticker} | {trade_type} | {_role_str} | Qty: {contracts} | "
                                    f"Edge: {opp.edge:.1%} | "
                                    f"Event: {city_event} ${allocation.total_spent:.2f}/${allocation.budget_dollars:.2f} | "
                                    f"Total: ${budget_spent:.2f}",
                                )

                                # Discord alert → #trades
                                self.alert_manager.alert_trade(
                                    ticker=opp.ticker,
                                    action="buy",
                                    side=side,
                                    quantity=contracts,
                                    price=price_cents,
                                    edge=opp.edge,
                                    trade_type=trade_type,
                                    bracket_role=_role_str,
                                    city_event=city_event,
                                    budget_spent=allocation.total_spent,
                                    budget_total=allocation.budget_dollars,
                                )

                                # Place resting limit sell at model EV
                                try:
                                    if side == "no":
                                        target_exit = int((1.0 - opp.our_probability) * 100) - 2
                                    else:
                                        target_exit = int(opp.our_probability * 100) - 2
                                    if target_exit > price_cents:
                                        import asyncio
                                        asyncio.get_event_loop().create_task(
                                            self.resting_order_manager.place_profit_target(
                                                ticker=opp.ticker,
                                                side=side,
                                                quantity=contracts,
                                                target_price=target_exit,
                                                entry_price=price_cents,
                                            )
                                        )
                                except Exception as _pt_err:
                                    logger.debug(f"Profit target placement failed: {_pt_err}")

                            else:
                                # Circuit breaker: if we get insufficient balance,
                                # stop trying more orders this cycle — they'll all
                                # fail too and waste API calls.
                                err_msg = (result.message or "").lower()
                                if "insufficient" in err_msg or "balance" in err_msg:
                                    logger.warning(
                                        f"INSUFFICIENT BALANCE \u2014 stopping weather cycle "
                                        f"({executed} trades placed, ${budget_spent:.2f} deployed)"
                                    )
                                    break

                    except Exception as e:
                        err_str = str(e).lower()
                        if "insufficient" in err_str or "balance" in err_str:
                            logger.warning(
                                f"INSUFFICIENT BALANCE \u2014 stopping weather cycle "
                                f"({executed} trades placed, ${budget_spent:.2f} deployed)"
                            )
                            break
                        logger.error(f"Weather trade failed: {opp.ticker}", error=str(e))

                    if executed >= MAX_TRADES_PER_CYCLE:
                        break  # Stop iterating cities too

            # Log city budget utilization
            summary = self.city_budget_allocator.get_cycle_summary(city_allocations)
            if summary["total_budget"] > 0:
                logger.info(
                    f"City budget summary: ${summary['total_spent']:.2f}/"
                    f"${summary['total_budget']:.2f} ({summary['utilization_pct']:.0f}% used) | "
                    f"trades={summary['total_trades']} | "
                    f"cities: {dict(city_trades_this_cycle)}"
                )

            if executed > 0:
                logger.info(
                    f"Cycle complete: {executed} trades executed, "
                    f"${budget_spent:.2f} deployed, "
                    f"cities: {dict(city_trades_this_cycle)}"
                )

            return executed

        except Exception as e:
            logger.error("Weather scan failed", error=str(e))
            return 0

    # ── Position exit engine (single authority: PositionReEvaluator) ─────

    async def _re_evaluate_and_exit_positions(self) -> Dict[str, int]:
        """
        Run deterministic position re-evaluator and execute exits.

        Returns:
            Dict with exit statistics.
        """
        stats = {
            "evaluated": 0,
            "exited": 0,
            "edge_eroded": 0,
            "edge_flipped": 0,
            "edge_decay_momentum": 0,
            "momentum_reversal": 0,
            "stop_loss": 0,
            "time_decay": 0,
        }

        try:
            # Pass latest opportunities for opportunity cost comparison
            self.position_reevaluator.set_available_opportunities(
                self._latest_weather_opportunities
            )

            # Rule-based evaluation (runs every cycle)
            exit_decisions = self.position_reevaluator.evaluate_all_positions()
            stats["evaluated"] = len(exit_decisions)

            for decision in exit_decisions:
                if not decision.should_exit:
                    continue

                success = self.position_reevaluator.execute_exit(decision)
                if success:
                    stats["exited"] += 1
                    self._stats["trades_executed"] += 1

                    # Track by category
                    if decision.reason == ReEvalExitReason.EDGE_ERODED:
                        stats["edge_eroded"] += 1
                    elif decision.reason == ReEvalExitReason.EDGE_FLIPPED:
                        stats["edge_flipped"] += 1
                    elif decision.reason == ReEvalExitReason.EDGE_DECAY_MOMENTUM:
                        stats["edge_decay_momentum"] += 1
                    elif decision.reason == ReEvalExitReason.MOMENTUM_REVERSAL:
                        stats["momentum_reversal"] += 1
                    elif decision.reason == ReEvalExitReason.STOP_LOSS:
                        stats["stop_loss"] += 1
                    elif decision.reason in (
                        ReEvalExitReason.TIME_DECAY_24H,
                        ReEvalExitReason.TIME_DECAY_48H,
                    ):
                        stats["time_decay"] += 1

                    self.alert_manager.alert_trade(
                        ticker=decision.ticker,
                        action="sell",
                        side=decision.side,
                        quantity=decision.quantity,
                        price=decision.current_price_cents or 0,
                    )

        except Exception as e:
            logger.error(f"Position re-evaluation failed: {e}")

        # ── Check resting sell orders (posted asks waiting for buyers) ──
        try:
            if self.resting_order_manager and self.resting_order_manager.active_count > 0:
                resting_result = await self.resting_order_manager.check_and_reprice()
                if resting_result.get("filled", 0) > 0 or resting_result.get("repriced", 0) > 0:
                    stats["resting_filled"] = resting_result.get("filled", 0)
                    stats["resting_repriced"] = resting_result.get("repriced", 0)
        except Exception as e:
            logger.error(f"Resting order check failed: {e}")

        # ── Update profit target orders (re-price at latest model EV) ──
        try:
            if self.resting_order_manager:
                pt_result = await self.resting_order_manager.update_profit_targets()
                if pt_result.get("updated", 0) > 0:
                    stats["profit_targets_updated"] = pt_result["updated"]
        except Exception as e:
            logger.error(f"Profit target update failed: {e}")

        return stats

    # ── Portfolio & cycle management ─────────────────────────────────────

    async def _update_portfolio(self) -> None:
        """Update portfolio snapshot.

        In live mode, snapshots come from _sync_bankroll_from_api() which
        uses Kalshi API values (the only source of truth).  Local DB position
        valuations are unreliable, so we skip the local save_snapshot() to
        avoid writing stale equity values that poison the drawdown calculation.
        """
        if not self.paper_trading:
            # Live: snapshots are written by _sync_bankroll_from_api()
            self._last_snapshot = datetime.now(timezone.utc)
            return

        try:
            self.portfolio.save_snapshot()
            self._last_snapshot = datetime.now(timezone.utc)
        except Exception as e:
            logger.error("Portfolio snapshot failed", error=str(e))

    async def _run_cycle(self) -> None:
        """Run one complete trading cycle (weather-only)."""
        cycle_start = datetime.now(timezone.utc)
        self._cycle_count += 1

        logger.debug("Starting trading cycle", cycle=self._cycle_count)

        try:
            # 0. Periodic bankroll + position sync (every 5 minutes)
            # MUST run BEFORE pause check so manual sells on Kalshi are picked up.
            # Previously this was after the pause return, so the bot never saw
            # manual position changes while paused — causing infinite pause loops.
            if not self.paper_trading:
                try:
                    now_sync = datetime.now(timezone.utc)
                    should_bankroll_sync = (
                        self._last_bankroll_sync is None
                        or (now_sync - self._last_bankroll_sync).total_seconds() >= self.BANKROLL_SYNC_INTERVAL
                    )
                    if should_bankroll_sync:
                        await self._sync_positions_from_api()
                        await self._sync_bankroll_from_api()
                        self._last_bankroll_sync = now_sync
                except Exception as e:
                    logger.debug(f"Periodic bankroll sync failed: {e}")
                    self._last_bankroll_sync = datetime.now(timezone.utc)

            # 1. Check risk limits and auto-pause if needed
            trading_blocked = False
            try:
                pause_reason = self.risk_manager.auto_check_and_pause(strategy="weather")
                if pause_reason:
                    logger.warning(f"Trading auto-paused: {pause_reason}")
                    trading_blocked = True
                elif self.risk_manager.is_trading_paused():
                    logger.info("Trading is paused, skipping new trades")
                    trading_blocked = True
                elif not self.risk_manager.check_daily_trade_limit():
                    logger.info("Daily trade limit reached, skipping new trades")
                    trading_blocked = True
            except Exception as e:
                logger.error(f"Risk check failed (corrupt DB data?): {e}")
                trading_blocked = True  # Block trading if risk check fails

            # ALWAYS run exit engine even when paused — selling reduces exposure
            if trading_blocked:
                try:
                    now = datetime.now(timezone.utc)
                    should_reeval = (
                        self._last_reeval_check is None
                        or (now - self._last_reeval_check).total_seconds() >= 30
                    )
                    if should_reeval:
                        reeval_stats = await self._re_evaluate_and_exit_positions()
                        self._last_reeval_check = now
                        if reeval_stats.get("exited", 0) > 0:
                            logger.info(
                                "Position exits while paused (reducing exposure)",
                                exited=reeval_stats["exited"],
                            )
                            # After selling, un-pause and re-check next cycle
                            self.risk_manager.resume_trading()
                except Exception as e:
                    logger.error(f"Exit engine failed while paused: {e}")
                return

            # 1.5. Check model health (every 10 cycles)
            # DISABLED for initial live trading - need more resolved trades for meaningful stats
            # Re-enable after 50+ resolved trades with outcomes
            if False and self._cycle_count % 10 == 0:
                try:
                    health_report = self.health_monitor.generate_health_report()
                    self._position_multiplier = self.health_monitor.get_position_size_multiplier()

                    if self.health_monitor.should_pause_trading():
                        logger.warning(
                            "Model health critical \u2014 pausing trading",
                            brier_score=health_report.brier_score,
                            win_rate=health_report.win_rate,
                        )
                        self.risk_manager.pause_trading("Model health critical")
                        return

                    if self._position_multiplier < 1.0:
                        logger.info(
                            "Model health: reducing position sizes",
                            multiplier=self._position_multiplier,
                            status=health_report.status.value,
                        )
                except Exception as e:
                    logger.warning(f"Health check failed: {e}")
                    self._position_multiplier = 1.0

            # 1.6. Edge realization tracking (every 10 cycles ~30 min)
            if self._cycle_count % 10 == 0:
                try:
                    record_edge_realizations()
                    summary = get_edge_realization_summary(days=14)
                    if summary and summary.trade_count > 0:
                        logger.info(format_edge_realization_log(summary, days=14))
                except Exception as e:
                    logger.debug(f"Edge realization tracking failed: {e}")

            # 1.7. Strategy drift detection (every 10 cycles ~30 min)
            if self._cycle_count % 10 == 0:
                try:
                    drift_alerts = self.health_monitor.check_strategy_drift()
                    for alert_msg in drift_alerts:
                        logger.warning(f"DRIFT: {alert_msg}")

                    # Send Discord alert for critical drift signals
                    critical_alerts = [a for a in drift_alerts if a.startswith("CRITICAL:")]
                    if critical_alerts and self.alert_manager:
                        self.alert_manager.alert_risk(
                            violation_type="Strategy Drift",
                            message="\n".join(critical_alerts),
                            is_critical=True,
                            data={"drift_alert_count": len(drift_alerts)},
                        )
                except Exception as e:
                    logger.debug(f"Strategy drift check failed: {e}")

            now = datetime.now(timezone.utc)

            # NOTE: Bankroll sync moved to top of _run_cycle (step 0) so it
            # runs even when trading is paused. This prevents infinite pause
            # loops when positions are sold manually on Kalshi.

            # 6.9. ── OBSERVATION-SETTLED FAST SCANNER (every 60 seconds) ──────
            # Speed-critical: detects dead brackets from live NWS station data
            # and buys NO for guaranteed profit before the market adjusts.
            try:
                should_obs_scan = (
                    self._last_obs_scan is None
                    or (now - self._last_obs_scan).total_seconds() >= self.OBS_SCAN_INTERVAL
                )
                if should_obs_scan:
                    obs_trades = await self._run_observation_settled_scan()
                    self._last_obs_scan = now
                    if obs_trades:
                        logger.info(
                            f"🔭 Observation-settled trades executed: {obs_trades}"
                        )
            except Exception as e:
                logger.error(f"Observation scanner failed: {e}")
                self._last_obs_scan = now

            # 7. Weather strategy — scan and execute trades (every 10 minutes)
            try:
                should_weather_scan = (
                    self._last_weather_scan is None
                    or (now - self._last_weather_scan).total_seconds() >= 600
                )
                if should_weather_scan:
                    weather_trades = await self._scan_and_execute_weather()
                    self._last_weather_scan = now
                    if weather_trades:
                        logger.info("Weather trades executed", count=weather_trades)
            except Exception as e:
                logger.error(f"Weather strategy failed: {e}")
                self._last_weather_scan = now

            # 7.1. Forecast update monitoring (every 15 min — aligned with NWS fetch cycle)
            try:
                should_forecast_check = (
                    self._last_forecast_check is None
                    or (now - self._last_forecast_check).total_seconds() >= 900
                )
                if should_forecast_check:
                    await self._check_forecast_updates()
                    self._last_forecast_check = now
            except Exception as e:
                logger.error(f"Forecast monitoring failed: {e}")
                self._last_forecast_check = now

            # 7.9. Position re-evaluator — single exit authority (every ~30 seconds)
            # PositionReEvaluator is the ONLY exit decision-maker.
            try:
                should_reeval = (
                    self._last_reeval_check is None
                    or (now - self._last_reeval_check).total_seconds() >= 30
                )
                if should_reeval:
                    reeval_stats = await self._re_evaluate_and_exit_positions()
                    self._last_reeval_check = now
                    if reeval_stats.get("exited", 0) > 0:
                        logger.info(
                            "Position re-evaluation exits",
                            exited=reeval_stats["exited"],
                            edge_eroded=reeval_stats.get("edge_eroded", 0),
                            edge_flipped=reeval_stats.get("edge_flipped", 0),
                            edge_decay_momentum=reeval_stats.get("edge_decay_momentum", 0),
                            momentum_reversal=reeval_stats.get("momentum_reversal", 0),
                            stop_loss=reeval_stats.get("stop_loss", 0),
                            time_decay=reeval_stats.get("time_decay", 0),
                        )
            except Exception as e:
                logger.error(f"Position re-evaluation failed: {e}")
                self._last_reeval_check = now

            # 8. Update portfolio snapshot periodically
            should_snapshot = (
                self._last_snapshot is None
                or (now - self._last_snapshot).total_seconds() >= self.DEFAULT_PORTFOLIO_SNAPSHOT_INTERVAL
            )
            if should_snapshot:
                await self._update_portfolio()

            # 8b. Calibration: backfill resolutions + periodic calibration check
            try:
                if self._cycle_count % 10 == 0:  # Every 10 cycles (~20 min)
                    from src.analysis.calibration import backfill_resolutions
                    updated = backfill_resolutions()
                    if updated:
                        logger.info("Calibration: backfilled forecast resolutions", count=updated)
            except Exception as e:
                logger.debug(f"Calibration backfill failed: {e}")

            # 8b2. Settlement manager — detect resolved markets, reconcile PnL (every 5 min)
            try:
                should_settlement_mgr = (
                    self._last_settlement_manager_check is None
                    or (now - self._last_settlement_manager_check).total_seconds()
                    >= SettlementManager.SETTLEMENT_CHECK_INTERVAL
                )
                if should_settlement_mgr:
                    settlements = self.settlement_manager.check_settlements()
                    self._last_settlement_manager_check = now
                    if settlements:
                        total_pnl = sum(s["pnl"] for s in settlements)
                        logger.info(
                            f"Settlement manager: {len(settlements)} positions resolved, "
                            f"P&L: ${total_pnl:+.2f}"
                        )
            except Exception as e:
                logger.error(f"Settlement manager failed: {e}")
                self._last_settlement_manager_check = now

            # 8c. Forecast audit: check for settlement observations (hourly)
            try:
                should_settlement_check = (
                    self._last_settlement_check is None
                    or (now - self._last_settlement_check).total_seconds() >= 3600
                )
                if should_settlement_check:
                    await self._check_settlements()
                    self._last_settlement_check = now
            except Exception as e:
                logger.debug(f"Settlement check failed: {e}")
                self._last_settlement_check = now

            # 8d. Check for expired orders
            expired = self.executor.check_expired_orders()
            if expired:
                logger.info("Orders expired", count=len(expired))

            # Update stats
            self._stats["cycles_completed"] = self._cycle_count
            self._stats["last_cycle_time"] = (datetime.now(timezone.utc) - cycle_start).total_seconds()

            # 9. Run system monitoring (every 5 cycles)
            if self._cycle_count % 5 == 0:
                try:
                    health = self.system_monitor.run_monitoring_cycle()
                    if health.overall_status.value == "unhealthy":
                        logger.warning(
                            "System health UNHEALTHY",
                            indicators=[
                                f"{i.name}: {i.message}"
                                for i in health.indicators
                                if i.status.value == "unhealthy"
                            ],
                        )
                except Exception as e:
                    logger.warning(f"System monitoring failed: {e}")

            # 30-minute weather summary logging
            now = datetime.now(timezone.utc)
            should_summary = (
                self._last_summary is None
                or (now - self._last_summary).total_seconds() >= self._summary_interval
            )
            if should_summary:
                self._last_summary = now
                w = self.weather_stats.get_and_reset_window()
                cities_str = ", ".join(w["cities_active"]) or "none"
                avg_price = w["avg_entry_price_cents"]
                avg_edge = w["avg_edge_pct"]
                logger.info(
                    f"\U0001f4ca WEATHER 30-MIN SUMMARY\n"
                    f"  \u251c\u2500\u2500 Forecasts generated: {w['forecasts_generated']}\n"
                    f"  \u251c\u2500\u2500 Opportunities found: {w['opportunities_found']}\n"
                    f"  \u251c\u2500\u2500 Trades executed: {w['trades_executed']}\n"
                    f"  \u251c\u2500\u2500 Avg entry price: {avg_price:.1f}\u00a2\n"
                    f"  \u251c\u2500\u2500 Avg edge: {avg_edge:.1f}%\n"
                    f"  \u2514\u2500\u2500 Cities active: {cities_str}",
                )

            # Heartbeat logging (every hour)
            should_heartbeat = (
                self._last_heartbeat is None
                or (now - self._last_heartbeat).total_seconds() >= self._heartbeat_interval
            )
            if should_heartbeat:
                runtime_hours = (now - self._stats["start_time"]).total_seconds() / 3600 if self._stats["start_time"] else 0
                logger.info(
                    "=== HEARTBEAT ===",
                    status="RUNNING",
                    runtime_hours=f"{runtime_hours:.1f}",
                    cycles=self._cycle_count,
                    trades_executed=self._stats["trades_executed"],
                    paper_mode=self.paper_trading,
                )
                self._last_heartbeat = now

        except Exception as e:
            logger.error("Trading cycle failed", error=str(e), cycle=self._cycle_count)
            self.system_monitor.record_error("trading_cycle", str(e))
            self.alert_manager.alert_system(
                title="Cycle Error",
                message=f"Trading cycle {self._cycle_count} failed: {e}",
                is_error=True,
            )

    # ── Startup & sync methods ───────────────────────────────────────────

    async def _validate_markets_on_startup(self) -> None:
        """Validate a sample of markets on startup, pruning stale entries."""
        import random as _random
        try:
            with next(get_db_session()) as session:
                total_markets = session.query(MarketDB).filter(
                    MarketDB.status.in_(["active", "open"])
                ).count()
                sample_tickers = [
                    str(m.ticker)
                    for m in session.query(MarketDB).filter(
                        MarketDB.status.in_(["active", "open"])
                    ).all()
                ]

            if not sample_tickers:
                logger.info("No active markets in database to validate")
                return

            sample_size = min(10, len(sample_tickers))
            sample = _random.sample(sample_tickers, sample_size)
            pruned = 0

            from src.api.kalshi_client import KalshiClient as _KC
            import httpx as _httpx
            async with _KC() as client:
                for ticker in sample:
                    try:
                        await client.get_market(ticker, use_cache=False)
                    except _httpx.HTTPStatusError as e:
                        if e.response.status_code == 404:
                            with next(get_db_session()) as session:
                                market = session.query(MarketDB).filter_by(ticker=ticker).first()
                                if market:
                                    market.status = "inactive_404"  # type: ignore[assignment]
                                    market.updated_at = datetime.now(timezone.utc)  # type: ignore[assignment]
                            pruned += 1
                    except Exception:
                        continue
                    await asyncio.sleep(0.2)

            logger.info(
                f"Validated {sample_size} markets, pruned {pruned} stale entries",
                total_markets=total_markets,
                sample_size=sample_size,
                pruned=pruned,
            )
        except Exception as e:
            logger.warning(f"Startup market validation failed: {e}")

    def _repair_orphaned_positions(self) -> int:
        """Create synthetic TradeDB entries for positions with no buy trade.

        Called once on startup. Finds PositionDB entries that have no
        matching unresolved buy trade in TradeDB and creates one.

        Returns number of orphans repaired.
        """
        repaired = 0
        try:
            with next(get_db_session()) as session:
                positions = session.query(PositionDB).filter(
                    PositionDB.quantity > 0
                ).all()

                for pos in positions:
                    buy_trade = session.query(TradeDB).filter(
                        TradeDB.ticker == pos.ticker,
                        TradeDB.resolved == 0,
                        TradeDB.action == "buy",
                    ).first()

                    if buy_trade is None:
                        synthetic = TradeDB(
                            order_id=f"REPAIR-{pos.ticker}",
                            fill_id=f"REPAIR-FILL-{pos.ticker}",
                            ticker=pos.ticker,
                            side=pos.side,
                            action="buy",
                            quantity=pos.quantity,
                            price=pos.average_price,
                            fee=0,
                            status="filled",
                            timestamp=pos.created_at or datetime.now(timezone.utc),
                            strategy="api_sync",
                            resolved=0,
                        )
                        session.add(synthetic)
                        repaired += 1
                        logger.warning(
                            f"Repaired orphan: {pos.ticker} | "
                            f"{pos.side} x{pos.quantity} @ {pos.average_price}c"
                        )

                session.commit()

        except Exception as e:
            logger.error(f"Orphan repair failed: {e}")

        if repaired:
            logger.info(f"Repaired {repaired} orphaned positions with synthetic trades")
        return repaired

    async def _sync_positions_from_api(self) -> None:
        """Sync open positions from the Kalshi API into the local DB.

        Performs bidirectional reconciliation:
        1. Adds Kalshi positions missing from DB
        2. Removes DB positions that don't exist on Kalshi (phantom cleanup)

        Only runs phantom cleanup in live mode to avoid wiping paper positions.

        Kalshi v2 API returns market_positions with fields:
          ticker, position (signed int), market_exposure, total_traded, etc.
        We derive side/quantity/average_price via the Position model properties.
        """
        if self.paper_trading:
            logger.info("Position sync: skipping in paper mode")
            return

        try:
            from src.api.kalshi_client import KalshiClient as _KC

            async with _KC() as client:
                api_positions = await client.get_positions()

            # Build set of tickers with real positions on Kalshi
            api_tickers = set()
            for pos in api_positions:
                if pos.quantity > 0:
                    api_tickers.add(pos.ticker)
                logger.debug(
                    f"API position: {pos.ticker} | "
                    f"position={pos.position}, side={pos.side}, "
                    f"qty={pos.quantity}, avg_price={pos.average_price}c, "
                    f"exposure={pos.market_exposure}c"
                )

            with next(get_db_session()) as session:
                # Query ALL positions in DB (including qty=0) so we don't
                # try to INSERT a row that already exists with qty=0
                all_db_positions = session.query(PositionDB).all()
                all_db_tickers = {str(p.ticker) for p in all_db_positions}
                active_db_positions = [p for p in all_db_positions if (p.quantity or 0) > 0]

                # 1. Remove phantom DB positions not on Kalshi (only active ones)
                phantoms_removed = 0
                for db_pos in active_db_positions:
                    if str(db_pos.ticker) not in api_tickers:
                        logger.warning(
                            f"Phantom position removed: {db_pos.ticker} | "
                            f"{db_pos.side} x{db_pos.quantity} @ {db_pos.average_price}c "
                            f"(not found on Kalshi)"
                        )
                        session.delete(db_pos)
                        phantoms_removed += 1

                # 2. Clean up stale qty=0 positions not on Kalshi
                stale_removed = 0
                for db_pos in all_db_positions:
                    if (db_pos.quantity or 0) == 0 and str(db_pos.ticker) not in api_tickers:
                        session.delete(db_pos)
                        stale_removed += 1

                # 3. Add missing AND update existing positions from Kalshi
                synced = 0
                updated = 0
                for pos in api_positions:
                    if pos.quantity <= 0:
                        continue

                    if pos.ticker in all_db_tickers:
                        # Position row exists (possibly qty=0) — UPDATE it
                        db_pos = session.query(PositionDB).filter_by(
                            ticker=pos.ticker
                        ).first()
                        if db_pos:
                            changed = False
                            if db_pos.quantity != pos.quantity:
                                db_pos.quantity = pos.quantity
                                changed = True
                            if db_pos.average_price != pos.average_price:
                                db_pos.average_price = pos.average_price
                                changed = True
                            if db_pos.side != pos.side:
                                db_pos.side = pos.side
                                changed = True
                            if changed:
                                db_pos.updated_at = datetime.now(timezone.utc)
                                updated += 1
                        continue

                    # Truly new position — INSERT
                    db_pos = PositionDB(
                        ticker=pos.ticker,
                        side=pos.side,
                        quantity=pos.quantity,
                        average_price=pos.average_price,
                        market_price=None,
                        unrealized_pnl=None,
                        created_at=datetime.now(timezone.utc),
                        updated_at=datetime.now(timezone.utc),
                    )
                    session.add(db_pos)

                    # Create synthetic trade record for audit trail
                    synthetic_trade = TradeDB(
                        order_id=f"SYNC-{pos.ticker}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}",
                        fill_id=f"SYNC-FILL-{pos.ticker}",
                        ticker=pos.ticker,
                        side=pos.side,
                        action="buy",
                        quantity=pos.quantity,
                        price=pos.average_price,
                        fee=0,
                        status="filled",
                        timestamp=datetime.now(timezone.utc),
                        strategy="api_sync",
                        resolved=0,
                    )
                    session.add(synthetic_trade)

                    synced += 1
                    logger.info(
                        f"Position synced: {pos.ticker} | "
                        f"{pos.side} x{pos.quantity} @ {pos.average_price}c | "
                        f"strategy=api_sync"
                    )

                session.commit()

                logger.info(
                    f"Position sync complete: {len(api_tickers)} API positions, "
                    f"{synced} newly synced, {updated} updated, "
                    f"{phantoms_removed} phantoms removed, "
                    f"{stale_removed} stale qty=0 cleaned up"
                )

        except Exception as e:
            logger.warning(f"Position sync failed (non-fatal): {e}")
            import traceback
            logger.debug(f"Position sync traceback: {traceback.format_exc()}")

    async def _sync_bankroll_from_api(self) -> None:
        """Fetch total portfolio value from Kalshi and update the risk manager.

        Portfolio value = cash balance + position market exposure.
        The balance endpoint only returns cash, so we also sum position exposure
        to get the full portfolio value that Kalshi shows on the dashboard.
        """
        try:
            from src.api.kalshi_client import KalshiClient as _KC

            async with _KC() as client:
                balance = await client.get_balance()
                api_positions = await client.get_positions()

            # Cash balance in dollars
            cash_dollars = float(balance.balance) / 100.0
            self._cash_balance_cents = int(balance.balance)

            # Use Kalshi's portfolio_value when available.  The API returns
            # position market value (NOT cash + positions), so we add cash
            # to get total portfolio.  Falls back to cost-basis sum of
            # market_exposure only if the API doesn't return portfolio_value.
            if balance.portfolio_value and balance.portfolio_value > 0:
                position_value_dollars = float(balance.portfolio_value) / 100.0
                total_portfolio = cash_dollars + position_value_dollars
            else:
                # Fallback: sum cost-basis exposure (less accurate)
                position_value_cents = sum(
                    abs(pos.market_exposure)
                    for pos in api_positions
                    if pos.quantity > 0
                )
                position_value_dollars = position_value_cents / 100.0
                total_portfolio = cash_dollars + position_value_dollars

            self._total_portfolio_dollars = total_portfolio

            # CRITICAL: Use CASH balance for bankroll, not total portfolio.
            # Position sizing must be based on what we can actually spend.
            old_bankroll = self.risk_manager.initial_bankroll
            self.risk_manager.initial_bankroll = cash_dollars

            # Log per-strategy exposure from trade DB
            try:
                with next(get_db_session()) as session:
                    weather_exp = sum(
                        float(t.price or 0) * float(t.quantity or 0) / 100.0
                        for t in session.query(TradeDB).filter(
                            TradeDB.strategy == "weather", TradeDB.resolved == 0, TradeDB.action == "buy"
                        ).all()
                    )
            except Exception:
                weather_exp = 0.0

            # Sync bankroll phase and adjust risk limits accordingly
            phase_msg = self.risk_manager.sync_phase_limits()
            phase = self.risk_manager.get_phase()

            pv_source = "api" if (balance.portfolio_value and balance.portfolio_value > 0) else "cost-basis"
            logger.info(
                f"Bankroll synced from Kalshi API: "
                f"${old_bankroll:.2f} -> ${cash_dollars:.2f} (cash-based) | "
                f"total_portfolio=${total_portfolio:.2f} "
                f"(cash=${cash_dollars:.2f} + positions=${position_value_dollars:.2f} [{pv_source}]) | "
                f"weather_exp=${weather_exp:.2f} | phase={phase.value}"
            )

            # Discord: portfolio snapshot → #portfolio (every 30 min, controlled by rate limiter)
            num_pos = len([p for p in api_positions if p.quantity > 0])
            obs_pool = getattr(self, '_obs_pool_available', 0.0)
            self.alert_manager.alert_portfolio_snapshot(
                cash=cash_dollars,
                positions=position_value_dollars,
                total=total_portfolio,
                phase=phase.value,
                weather_exposure=weather_exp,
                num_positions=num_pos,
                obs_pool=obs_pool,
            )

            # Discord: phase change → #portfolio (if phase changed)
            old_phase_str = getattr(self, '_last_discord_phase', None)
            if old_phase_str and old_phase_str != phase.value:
                self.alert_manager.alert_phase_change(
                    old_phase=old_phase_str,
                    new_phase=phase.value,
                    bankroll=cash_dollars,
                )
            self._last_discord_phase = phase.value

            # Save a portfolio snapshot from the API values.  In live mode this
            # is the ONLY snapshot source — portfolio.save_snapshot() is skipped
            # because local position valuations are unreliable.
            #
            # market_exposure is cost basis (not current market value), so
            # total_portfolio won't exactly match the Kalshi dashboard, but it
            # IS consistent across syncs, which is all drawdown tracking needs.
            try:
                from decimal import Decimal as D
                from src.data.models import PortfolioSnapshotDB
                with next(get_db_session()) as session:
                    snapshot = PortfolioSnapshotDB(
                        timestamp=datetime.now(timezone.utc),
                        balance=D(str(round(cash_dollars, 2))),
                        total_position_value=D(str(round(position_value_dollars, 2))),
                        unrealized_pnl=D("0"),
                        realized_pnl=D("0"),
                        total_equity=D(str(round(total_portfolio, 2))),
                        daily_return=0.0,
                    )
                    session.add(snapshot)
                    session.commit()
                    logger.debug(f"Portfolio snapshot from API: equity=${total_portfolio:.2f}")
            except Exception as snap_err:
                logger.debug(f"Snapshot save failed (non-fatal): {snap_err}")

        except Exception as e:
            logger.warning(f"Bankroll sync failed (non-fatal, keeping ${self.risk_manager.initial_bankroll:.2f}): {e}")

    # ── Main run loop ────────────────────────────────────────────────────

    async def run(self) -> None:
        """Run the main trading loop."""
        self._setup_signal_handlers()
        self._running = True
        self._stats["start_time"] = datetime.now(timezone.utc)

        mode = "PAPER" if self.paper_trading else "LIVE"
        logger.info(f"Starting trading system in {mode} mode")

        logger.info(
            "\U0001f321\ufe0f WEATHER-ONLY MODE\n"
            f"  Weather: ENABLED (scan every 10 min, main 60s loop)\n"
            "  Other strategies: DISABLED (weather-only mode)",
        )

        self.alert_manager.alert_system(
            title="System Started",
            message=f"Trading system started in {mode} mode",
            is_error=False,
        )

        # Verify probability utils availability (replaced scipy dependency)
        try:
            from src.utils.probability_utils import norm_cdf  # noqa: F401
            logger.info("Math probability functions verified")
        except ImportError as e:
            logger.error(f"Math probability import failed: {e}")
            raise SystemExit(1)

        # Validate markets before starting
        await self._validate_markets_on_startup()

        # Sync positions from Kalshi API to ensure exit engine has accurate data
        await self._sync_positions_from_api()

        # Sync bankroll from Kalshi API so risk manager uses real balance
        await self._sync_bankroll_from_api()

        # Repair orphaned positions AFTER API sync so synced positions get
        # synthetic TradeDB entries.  Without this, the dedup check in
        # _scan_and_execute_weather sees no unresolved buy trade and keeps
        # re-buying into existing positions.
        repaired = self._repair_orphaned_positions()
        if repaired:
            logger.info(f"Startup: repaired {repaired} orphaned positions")

        # Start data collection
        logger.info("Starting data collector...")
        await self.data_collector.start()

        try:
            # Wait for initial data collection
            logger.info("Waiting for initial data collection (30 seconds)...")
            await asyncio.sleep(30)

            while self._running and not self._shutdown_requested:
                await self._run_cycle()

                # Wait for next cycle
                await asyncio.sleep(self.price_update_interval)

        except Exception as e:
            logger.error("Trading loop crashed", error=str(e))
            self.alert_manager.alert_system(
                title="System Crash",
                message=f"Trading loop crashed: {e}",
                is_error=True,
            )
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        """Graceful shutdown of the trading system."""
        logger.info("Shutting down trading system")

        self._running = False

        # Close shared API client
        if self._api_client and self._api_client.client:
            await self._api_client.client.aclose()

        # Stop data collector
        if self.data_collector:
            await self.data_collector.stop()

        # Cancel pending orders
        pending = self.executor.get_pending_orders()
        for order in pending:
            self.executor.cancel_order(order.order_id)
            logger.info("Cancelled pending order", order_id=order.order_id)

        # Final portfolio snapshot
        await self._update_portfolio()

        # Log final stats
        runtime = (
            (datetime.now(timezone.utc) - self._stats["start_time"]).total_seconds()
            if self._stats["start_time"]
            else 0
        )

        logger.info(
            "Trading system shutdown complete",
            runtime_seconds=runtime,
            cycles_completed=self._stats["cycles_completed"],
            trades_executed=self._stats["trades_executed"],
            opportunities_found=self._stats["opportunities_found"],
        )

        self.alert_manager.alert_system(
            title="System Stopped",
            message=f"Trading system stopped after {self._stats['cycles_completed']} cycles",
            is_error=False,
        )

    def stop(self) -> None:
        """Request graceful stop."""
        self._shutdown_requested = True

    def get_stats(self) -> Dict[str, Any]:
        """Get current system statistics."""
        stats = self._stats.copy()
        stats["running"] = self._running
        stats["paper_trading"] = self.paper_trading
        stats["trading_paused"] = self.risk_manager.is_trading_paused()

        # Add portfolio summary
        try:
            summary = self.portfolio.get_summary()
            stats["equity"] = summary.total_equity
            stats["positions"] = summary.position_count
            stats["daily_return"] = summary.daily_return
        except Exception:
            pass

        # Add session stats
        stats["session"] = self.session_stats.get_session_stats()

        return stats


async def run_trading_system(
    paper_trading: bool = True,
    market_scan_interval: int = 120,
    min_edge: float = 0.03,
    auto_restart: bool = True,
    max_restarts: int = 5,
    reset_drawdown: bool = False,
) -> None:
    """
    Convenience function to run the trading system with auto-restart.

    Args:
        paper_trading: If True, simulate trades.
        market_scan_interval: Seconds between market scans.
        min_edge: Minimum edge threshold.
        auto_restart: If True, automatically restart on crash.
        max_restarts: Maximum number of restarts before giving up.
        reset_drawdown: If True, reset peak equity to current before starting.
    """
    restart_count = 0
    restart_delay = 30  # seconds between restarts

    while restart_count <= max_restarts:
        try:
            system = TradingSystem(
                paper_trading=paper_trading,
                market_scan_interval=market_scan_interval,
                min_edge_threshold=min_edge,
            )

            # Reset drawdown before first run if requested
            if reset_drawdown and restart_count == 0:
                new_peak = system.risk_manager.reset_peak_equity()
                logger.info(f"Drawdown reset: new peak equity = ${new_peak:.2f}")

            await system.run()
            break  # Clean exit, don't restart

        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received, shutting down")
            break

        except Exception as e:
            restart_count += 1
            logger.error(
                "Trading system crashed",
                error=str(e),
                restart_count=restart_count,
                max_restarts=max_restarts,
            )

            if not auto_restart or restart_count > max_restarts:
                logger.error("Auto-restart disabled or max restarts exceeded, exiting")
                raise

            logger.info(
                f"Auto-restarting in {restart_delay} seconds...",
                restart_count=restart_count,
            )
            await asyncio.sleep(restart_delay)
            restart_delay = min(restart_delay * 2, 300)  # Exponential backoff, max 5 min


def main() -> None:
    """Entry point for the trading system."""
    import argparse

    parser = argparse.ArgumentParser(description="Kalshi Trading System")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Run in live trading mode (default: paper trading)",
    )
    parser.add_argument(
        "--scan-interval",
        type=int,
        default=120,
        help="Market scan interval in seconds (default: 120)",
    )
    parser.add_argument(
        "--min-edge",
        type=float,
        default=0.03,
        help="Minimum edge threshold (default: 0.03)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    parser.add_argument(
        "--reset-drawdown",
        action="store_true",
        help="Reset peak equity to current value (drawdown → 0%%) before starting",
    )

    args = parser.parse_args()

    # Configure logging level
    if args.verbose:
        import logging
        logging.getLogger().setLevel(logging.DEBUG)

    paper_trading = not args.live

    if not paper_trading:
        print("WARNING: Running in LIVE trading mode!")
        response = input("Type 'CONFIRM' to continue: ")
        if response != "CONFIRM":
            print("Aborted.")
            sys.exit(1)

    asyncio.run(
        run_trading_system(
            paper_trading=paper_trading,
            market_scan_interval=args.scan_interval,
            min_edge=args.min_edge,
            reset_drawdown=args.reset_drawdown,
        )
    )


if __name__ == "__main__":
    main()
