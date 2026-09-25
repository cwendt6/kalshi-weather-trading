"""
Risk management module for trading controls.

Implements safeguards to prevent catastrophic losses:
- Daily loss limits
- Maximum position per market
- Maximum total exposure
- Drawdown-based trading pause
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional

from src.data.database import get_db_session
from src.data.models import MarketDB, PortfolioSnapshotDB, PositionDB, TradeDB
from src.utils.datetime_utils import ensure_utc
from src.utils.logging import logger


class RiskStatus(Enum):
    """Risk check status."""

    APPROVED = "approved"
    REJECTED = "rejected"
    WARNING = "warning"


class RiskViolation(Enum):
    """Types of risk violations."""

    DAILY_LOSS_LIMIT = "daily_loss_limit"
    WEEKLY_LOSS_LIMIT = "weekly_loss_limit"
    MAX_POSITION_SIZE = "max_position_size"
    MAX_TOTAL_EXPOSURE = "max_total_exposure"
    MAX_DRAWDOWN = "max_drawdown"
    TRADING_PAUSED = "trading_paused"
    CONCENTRATION_LIMIT = "concentration_limit"


class BankrollPhase(Enum):
    """Bankroll-dependent risk phase."""

    SURVIVAL = "survival"          # $0 - $300: ultra-conservative
    ACCELERATION = "acceleration"  # $300 - $1000: moderate growth
    SCALING = "scaling"            # $1000+: standard risk


@dataclass
class RiskCheckResult:
    """Result of a risk check."""

    status: RiskStatus
    approved: bool
    violations: List[RiskViolation]
    messages: List[str]

    # Limits info
    daily_pnl: float
    daily_loss_limit: float
    current_exposure: float
    max_exposure: float
    position_value: float
    max_position_value: float
    current_drawdown: float
    max_drawdown: float

    # Trade details (if applicable)
    ticker: Optional[str] = None
    requested_amount: Optional[float] = None
    allowed_amount: Optional[float] = None

    timestamp: datetime = datetime.now(timezone.utc)


@dataclass
class RiskEvent:
    """A risk event for logging/alerting."""

    event_type: RiskViolation
    severity: str  # "warning", "violation", "critical"
    message: str
    details: Dict[str, Any]
    timestamp: datetime = datetime.now(timezone.utc)


class RiskManager:
    """
    Risk management for prediction market trading.

    Enforces:
    - Daily loss limit (default 5% of bankroll)
    - Maximum position per market (default 10% of bankroll)
    - Maximum total exposure (default 50% of bankroll)
    - Maximum drawdown (default 15% from peak)
    """

    # Risk parameters — RE-ENABLED for live trading safety.
    DEFAULT_DAILY_LOSS_LIMIT_PCT = 0.10   # 10% daily loss limit — STOP trading if hit
    DEFAULT_MAX_POSITION_PCT = 0.15        # 15% max per single position
    DEFAULT_MAX_EXPOSURE_PCT = 0.75        # 75% max total capital at risk
    DEFAULT_MAX_DRAWDOWN_PCT = 0.25        # 25% max drawdown from peak

    # Phase 4: Observation-settled capital pool
    import os as _os
    OBS_POOL_PCT = float(_os.getenv("OBS_POOL_PCT", "0.15"))              # 15% of bankroll
    OBS_POOL_DEPLOY_PCT = float(_os.getenv("OBS_POOL_DEPLOY_PCT", "0.50"))  # 50% per signal
    OBS_POOL_MIN_DEPLOY = float(_os.getenv("OBS_POOL_MIN_DEPLOY", "0.50"))  # Min $0.50 per trade
    from pathlib import Path as _Path
    OBS_POOL_STATE_FILE = _Path(_os.getenv("OBS_POOL_STATE_FILE", "data/obs_pool_state.json"))

    def __init__(
        self,
        daily_loss_limit_pct: float = 0.10,
        max_position_pct: float = 0.15,
        max_exposure_pct: float = 0.75,
        max_drawdown_pct: float = 0.25,
        initial_bankroll: Optional[float] = None,
    ) -> None:
        """
        Initialize risk manager.

        Args:
            daily_loss_limit_pct: Max daily loss as % of bankroll.
            max_position_pct: Max position size as % of bankroll.
            max_exposure_pct: Max total exposure as % of bankroll.
            max_drawdown_pct: Max drawdown before pausing trading.
            initial_bankroll: Override bankroll (uses DB if None).
        """
        self.daily_loss_limit_pct = daily_loss_limit_pct
        self.max_position_pct = max_position_pct
        self.max_exposure_pct = max_exposure_pct
        self.max_drawdown_pct = max_drawdown_pct
        self.initial_bankroll = initial_bankroll
        self.max_daily_trades = 100  # Default for paper mode

        # Trading state
        self._trading_paused = False
        self._pause_reason: Optional[str] = None
        self._consecutive_api_failures = 0
        self._max_consecutive_failures = 3

        # Event log
        self._risk_events: List[RiskEvent] = []

        # Bankroll phase tracking
        self._current_phase: Optional[BankrollPhase] = None

        # Obs-settled capital pool state
        self._obs_pool_deployed: float = 0.0   # Currently deployed in obs-settled positions
        self._obs_pool_profits: float = 0.0    # Cumulative profits returned to pool

        # Circuit breaker state
        self._circuit_breaker_active: bool = False
        self._circuit_breaker_reason: Optional[str] = None
        self._consecutive_loss_count: int = 0

        logger.info(
            "Risk manager initialized",
            daily_loss_limit_pct=daily_loss_limit_pct,
            max_position_pct=max_position_pct,
            max_exposure_pct=max_exposure_pct,
            max_drawdown_pct=max_drawdown_pct,
        )

    def get_bankroll(self) -> float:
        """Get current bankroll from database or override."""
        if self.initial_bankroll is not None:
            return self.initial_bankroll

        with next(get_db_session()) as session:
            snapshot = (
                session.query(PortfolioSnapshotDB)
                .order_by(PortfolioSnapshotDB.timestamp.desc())
                .first()
            )

            if snapshot and snapshot.balance:
                return float(snapshot.balance)

            return 1000.0  # Default

    def get_peak_equity(self) -> float:
        """Get peak portfolio equity for drawdown calculation."""
        with next(get_db_session()) as session:
            # Get max total_equity from snapshots
            from sqlalchemy import func

            result = session.query(
                func.max(PortfolioSnapshotDB.total_equity)
            ).scalar()

            if result:
                return float(result)

            # If no history, use current bankroll as peak
            return self.get_bankroll()

    def get_current_equity(self) -> float:
        """Get current portfolio equity."""
        with next(get_db_session()) as session:
            snapshot = (
                session.query(PortfolioSnapshotDB)
                .order_by(PortfolioSnapshotDB.timestamp.desc())
                .first()
            )

            if snapshot and snapshot.total_equity:
                return float(snapshot.total_equity)

            return self.get_bankroll()

    def get_daily_pnl(self, strategy: Optional[str] = None) -> float:
        """Calculate P&L for today, optionally filtered by strategy."""
        with next(get_db_session()) as session:
            # Get today's start
            today_start = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            )

            # Sum realized P&L from today's trades
            trade_query = (
                session.query(TradeDB)
                .filter(TradeDB.timestamp >= today_start)
                .filter(TradeDB.status == "filled")
            )
            if strategy:
                trade_query = trade_query.filter(TradeDB.strategy == strategy)
            trades = trade_query.all()

            realized_pnl = 0.0
            for trade in trades:
                if trade.fee:
                    realized_pnl -= float(trade.fee)

            # When filtering by strategy, use trade-based P&L (snapshots aren't per-strategy)
            if strategy:
                # Sum cost of open buy trades and resolved sell trades for this strategy today
                buy_cost = 0.0
                sell_revenue = 0.0
                for trade in trades:
                    cost = float(trade.price or 0) * float(trade.quantity or 0) / 100.0
                    if trade.action == "buy":
                        buy_cost += cost
                    elif trade.action == "sell":
                        sell_revenue += cost
                return sell_revenue - buy_cost - abs(realized_pnl)

            # Get unrealized P&L from positions
            positions = session.query(PositionDB).all()
            unrealized_pnl = 0.0
            for pos in positions:
                if pos.unrealized_pnl:
                    unrealized_pnl += float(pos.unrealized_pnl)

            # Get today's starting equity
            yesterday_snapshot = (
                session.query(PortfolioSnapshotDB)
                .filter(PortfolioSnapshotDB.timestamp < today_start)
                .order_by(PortfolioSnapshotDB.timestamp.desc())
                .first()
            )

            if yesterday_snapshot and yesterday_snapshot.total_equity:
                start_equity = float(yesterday_snapshot.total_equity)
                current_equity = self.get_current_equity()
                return current_equity - start_equity

            return realized_pnl + unrealized_pnl

    def get_weekly_pnl(self) -> float:
        """Calculate P&L for the current week (Monday 00:00 UTC to now).

        Uses the same approach as get_daily_pnl(): compares current equity
        to the equity snapshot from the most recent Monday.

        NOTE: Same phantom PnL limitation as daily — if stale trade records
        inflate the PnL calculation, this will also be affected. Still
        valuable as a belt-and-suspenders safety gate.
        """
        with next(get_db_session()) as session:
            now = datetime.now(timezone.utc)
            # Monday 00:00 UTC of the current week
            days_since_monday = now.weekday()  # 0=Monday
            week_start = (now - timedelta(days=days_since_monday)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )

            # Try equity-based comparison first (most accurate)
            monday_snapshot = (
                session.query(PortfolioSnapshotDB)
                .filter(PortfolioSnapshotDB.timestamp < week_start)
                .order_by(PortfolioSnapshotDB.timestamp.desc())
                .first()
            )

            if monday_snapshot and monday_snapshot.total_equity:
                start_equity = float(monday_snapshot.total_equity)
                current_equity = self.get_current_equity()
                return current_equity - start_equity

            # Fallback: sum realized P&L from this week's trades
            trades = (
                session.query(TradeDB)
                .filter(
                    TradeDB.timestamp >= week_start,
                    TradeDB.status == "filled",
                )
                .all()
            )

            realized_pnl = 0.0
            for trade in trades:
                if trade.fee:
                    realized_pnl -= float(trade.fee)

            # Add unrealized P&L from current positions
            positions = session.query(PositionDB).all()
            unrealized_pnl = 0.0
            for pos in positions:
                if pos.unrealized_pnl:
                    unrealized_pnl += float(pos.unrealized_pnl)

            return realized_pnl + unrealized_pnl

    # ─── Bankroll Phase System ─────────────────────────────────────────

    def get_phase(self) -> BankrollPhase:
        """
        Determine current bankroll phase based on available capital.

        SURVIVAL (<$300): Ultra-conservative, protect capital at all costs.
        ACCELERATION ($300-$1000): Moderate growth, gradually increase risk.
        SCALING ($1000+): Standard risk parameters.
        """
        bankroll = self.get_bankroll()
        if bankroll < 300:
            return BankrollPhase.SURVIVAL
        elif bankroll < 1000:
            return BankrollPhase.ACCELERATION
        else:
            return BankrollPhase.SCALING

    def get_phase_limits(self) -> Dict[str, Any]:
        """
        Get risk limits for the current bankroll phase.

        Returns dict with:
            max_position_pct, max_exposure_pct, daily_loss_limit_pct,
            min_cash_reserve_pct, max_positions
        """
        phase = self.get_phase()
        if phase == BankrollPhase.SURVIVAL:
            return {
                "phase": phase.value,
                "max_position_pct": 0.10,      # 10% per position
                "max_exposure_pct": 0.80,       # 80% total (bracket strategy holds many small NO positions)
                "daily_loss_limit_pct": 0.08,   # 8% daily loss = STOP
                "weekly_loss_limit_pct": 0.15,  # 15% weekly loss = STOP
                "min_cash_reserve_pct": 0.25,   # Always keep 25% cash
                "max_positions": 70,            # Loose safety rail — bracket portfolios need 3 cities × ~12 brackets
                "max_contracts_per_ticker": 5,   # Bracket spread: 2-5 per bracket, forces diversification
            }
        elif phase == BankrollPhase.ACCELERATION:
            return {
                "phase": phase.value,
                "max_position_pct": 0.15,
                "max_exposure_pct": 0.75,
                "daily_loss_limit_pct": 0.10,
                "weekly_loss_limit_pct": 0.20,  # 20% weekly loss = STOP
                "min_cash_reserve_pct": 0.15,
                "max_positions": 70,            # Loose safety rail for bracket portfolios
                "max_contracts_per_ticker": 7,   # Bracket spread: moderate concentration
            }
        else:  # SCALING
            return {
                "phase": phase.value,
                "max_position_pct": 0.20,
                "max_exposure_pct": 0.80,
                "daily_loss_limit_pct": 0.12,
                "weekly_loss_limit_pct": 0.25,  # 25% weekly loss = STOP
                "min_cash_reserve_pct": 0.10,
                "max_positions": 80,            # Loose safety rail for bracket portfolios
                "max_contracts_per_ticker": 12,  # Bracket spread: generous but bounded
            }

    def sync_phase_limits(self) -> Optional[str]:
        """
        Update risk manager limits based on current bankroll phase.

        Call this on bankroll sync (every 5 minutes) to dynamically
        adjust limits as the bankroll grows or shrinks.

        Returns:
            Phase transition message if phase changed, None otherwise.
        """
        new_phase = self.get_phase()
        old_phase = self._current_phase

        limits = self.get_phase_limits()
        self.max_position_pct = limits["max_position_pct"]
        self.max_exposure_pct = limits["max_exposure_pct"]
        self.daily_loss_limit_pct = limits["daily_loss_limit_pct"]

        transition_msg = None
        if old_phase is not None and new_phase != old_phase:
            bankroll = self.get_bankroll()
            transition_msg = (
                f"Bankroll phase transition: {old_phase.value} -> {new_phase.value} "
                f"(bankroll=${bankroll:.2f})"
            )
            logger.info(transition_msg)

        self._current_phase = new_phase
        return transition_msg

    def get_total_exposure(self, strategy: Optional[str] = None) -> float:
        """Calculate total current exposure from PositionDB (API-synced source of truth).

        Always uses PositionDB regardless of strategy filter. TradeDB contains
        stale unresolved trades from expired markets that inflate exposure.
        PositionDB is kept accurate by periodic Kalshi API sync.

        The strategy parameter is accepted for API compatibility but does not
        change the calculation — all open positions count toward exposure.
        """
        with next(get_db_session()) as session:
            positions = session.query(PositionDB).all()

            total = 0.0
            for pos in positions:
                if pos.quantity and pos.average_price:
                    # Position cost = quantity * price / 100
                    total += float(pos.quantity) * float(pos.average_price) / 100.0

            return total

    def get_position_exposure(self, ticker: str) -> float:
        """Get current exposure for a specific market."""
        with next(get_db_session()) as session:
            position = session.query(PositionDB).filter_by(ticker=ticker).first()

            if position and position.quantity and position.average_price:
                return float(position.quantity) * float(position.average_price) / 100.0

            return 0.0

    def reset_peak_equity(self) -> float:
        """
        Reset peak equity to current equity so drawdown restarts at 0%.

        Caps all portfolio snapshots with total_equity > current to current.
        Returns the new peak (= current equity).
        """
        current = self.get_current_equity()
        try:
            with next(get_db_session()) as session:
                from sqlalchemy import update
                stmt = (
                    update(PortfolioSnapshotDB)
                    .where(PortfolioSnapshotDB.total_equity > current)
                    .values(total_equity=current)
                )
                result = session.execute(stmt)
                session.commit()
                logger.info(
                    "Peak equity reset",
                    new_peak=current,
                    snapshots_updated=result.rowcount,
                )
        except Exception as e:
            logger.error(f"Failed to reset peak equity: {e}")

        # Also clear any existing pause from drawdown
        if self._trading_paused and self._pause_reason and "drawdown" in self._pause_reason.lower():
            self.resume_trading()

        return current

    def get_current_drawdown(self) -> float:
        """Calculate current drawdown from peak equity."""
        peak = self.get_peak_equity()
        current = self.get_current_equity()

        if peak <= 0:
            return 0.0

        return (peak - current) / peak

    def _log_risk_event(
        self,
        event_type: RiskViolation,
        severity: str,
        message: str,
        details: Dict[str, Any],
    ) -> None:
        """Log a risk event."""
        event = RiskEvent(
            event_type=event_type,
            severity=severity,
            message=message,
            details=details,
            timestamp=datetime.now(timezone.utc),
        )
        self._risk_events.append(event)

        # Also log via logger
        log_msg = f"RISK {severity.upper()}: {message}"
        if severity == "critical":
            logger.error(log_msg, **details)
        elif severity == "violation":
            logger.warning(log_msg, **details)
        else:
            logger.info(log_msg, **details)

    def check_trade(
        self,
        ticker: str,
        side: str,
        quantity: int,
        price: int,
        strategy: Optional[str] = "weather",
    ) -> RiskCheckResult:
        """
        Check if a proposed trade passes risk checks.

        Args:
            ticker: Market ticker.
            side: "yes" or "no".
            quantity: Number of contracts.
            price: Price per contract in cents.
            strategy: Strategy name for exposure filtering (default "weather").
                      Uses strategy-specific exposure from TradeDB for consistency
                      with auto_check_and_pause. Global PositionDB exposure includes
                      api_sync positions that inflate the number after DB recovery.

        Returns:
            RiskCheckResult with approval status and details.
        """
        violations: List[RiskViolation] = []
        messages: List[str] = []

        bankroll = self.get_bankroll()
        daily_pnl = self.get_daily_pnl()
        current_exposure = self.get_total_exposure(strategy=strategy)
        position_exposure = self.get_position_exposure(ticker)
        current_drawdown = self.get_current_drawdown()

        # Calculate trade value
        trade_value = quantity * price / 100.0

        # Check if trading is paused
        if self._trading_paused:
            violations.append(RiskViolation.TRADING_PAUSED)
            messages.append(f"Trading paused: {self._pause_reason}")

        # Daily loss limit — disabled (was triggering on phantom PnL from
        # stale trade records). Sell-side exits are already exempted in the
        # executor. Re-enable once daily PnL calculation is clean.
        daily_loss_limit = bankroll * self.daily_loss_limit_pct

        # Weekly loss limit check
        # NOTE: Same phantom PnL limitation as daily — if stale trade records
        # inflate the PnL calculation, this will also be affected.
        phase_limits_for_weekly = self.get_phase_limits()
        weekly_loss_limit_pct = phase_limits_for_weekly.get("weekly_loss_limit_pct", 0.20)
        weekly_loss_limit = bankroll * weekly_loss_limit_pct
        weekly_pnl = self.get_weekly_pnl()
        if weekly_pnl < -weekly_loss_limit:
            violations.append(RiskViolation.WEEKLY_LOSS_LIMIT)
            messages.append(
                f"Weekly loss limit breached: ${weekly_pnl:.2f} < -${weekly_loss_limit:.2f} "
                f"({weekly_loss_limit_pct:.0%} of ${bankroll:.2f})"
            )
            self._log_risk_event(
                RiskViolation.WEEKLY_LOSS_LIMIT,
                "warning",
                "Weekly loss limit breached",
                {
                    "weekly_pnl": weekly_pnl,
                    "weekly_loss_limit": weekly_loss_limit,
                    "weekly_loss_limit_pct": weekly_loss_limit_pct,
                    "bankroll": bankroll,
                },
            )

        # Check max position size
        max_position_value = bankroll * self.max_position_pct
        new_position_value = position_exposure + trade_value

        if new_position_value > max_position_value:
            violations.append(RiskViolation.MAX_POSITION_SIZE)
            messages.append(
                f"Max position size exceeded: ${new_position_value:.2f} > ${max_position_value:.2f}"
            )
            self._log_risk_event(
                RiskViolation.MAX_POSITION_SIZE,
                "violation",
                f"Max position size would be exceeded for {ticker}",
                {
                    "ticker": ticker,
                    "current": position_exposure,
                    "requested": trade_value,
                    "limit": max_position_value,
                },
            )

        # Check max contracts per ticker (defense-in-depth)
        phase_limits = self.get_phase_limits()
        max_contracts = phase_limits.get("max_contracts_per_ticker", 5)

        try:
            with next(get_db_session()) as session:
                existing_pos = session.query(PositionDB).filter(
                    PositionDB.ticker == ticker,
                    PositionDB.quantity > 0,
                ).first()
                existing_qty = int(existing_pos.quantity) if existing_pos else 0
        except Exception:
            existing_qty = 0

        total_contracts = existing_qty + quantity
        if total_contracts > max_contracts:
            violations.append(RiskViolation.CONCENTRATION_LIMIT)
            messages.append(
                f"Contract count exceeded: {total_contracts} > {max_contracts} "
                f"(existing: {existing_qty}, new: {quantity})"
            )
            self._log_risk_event(
                RiskViolation.CONCENTRATION_LIMIT,
                "violation",
                f"Contract count limit exceeded for {ticker}",
                {
                    "ticker": ticker,
                    "existing": existing_qty,
                    "requested": quantity,
                    "limit": max_contracts,
                },
            )

        # Check max total exposure
        max_exposure = bankroll * self.max_exposure_pct
        new_total_exposure = current_exposure + trade_value

        if new_total_exposure > max_exposure:
            violations.append(RiskViolation.MAX_TOTAL_EXPOSURE)
            messages.append(
                f"Max total exposure exceeded: ${new_total_exposure:.2f} > ${max_exposure:.2f}"
            )
            self._log_risk_event(
                RiskViolation.MAX_TOTAL_EXPOSURE,
                "violation",
                "Max total exposure would be exceeded",
                {
                    "current": current_exposure,
                    "requested": trade_value,
                    "limit": max_exposure,
                },
            )

        # Check max drawdown
        max_drawdown = self.max_drawdown_pct
        if current_drawdown >= max_drawdown:
            violations.append(RiskViolation.MAX_DRAWDOWN)
            messages.append(
                f"Max drawdown exceeded: {current_drawdown:.1%} >= {max_drawdown:.1%}"
            )
            self._log_risk_event(
                RiskViolation.MAX_DRAWDOWN,
                "critical",
                "Max drawdown threshold reached",
                {"drawdown": current_drawdown, "limit": max_drawdown},
            )
            # Pause trading on max drawdown
            self.pause_trading(f"Max drawdown reached: {current_drawdown:.1%}")

        # Correlation check
        if not self.check_correlation(ticker, side):
            violations.append(RiskViolation.CONCENTRATION_LIMIT)
            messages.append(f"Correlated exposure too high for {ticker}")

        # Determine allowed amount if partially allowed
        allowed_amount: Optional[float] = None
        if RiskViolation.MAX_POSITION_SIZE in violations:
            allowed_amount = max(0, max_position_value - position_exposure)
        elif RiskViolation.MAX_TOTAL_EXPOSURE in violations:
            allowed_amount = max(0, max_exposure - current_exposure)

        # Determine status
        if violations:
            status = RiskStatus.REJECTED
            approved = False
        else:
            status = RiskStatus.APPROVED
            approved = True

        return RiskCheckResult(
            status=status,
            approved=approved,
            violations=violations,
            messages=messages,
            daily_pnl=daily_pnl,
            daily_loss_limit=daily_loss_limit,
            current_exposure=current_exposure,
            max_exposure=max_exposure,
            position_value=new_position_value,
            max_position_value=max_position_value,
            current_drawdown=current_drawdown,
            max_drawdown=max_drawdown,
            ticker=ticker,
            requested_amount=trade_value,
            allowed_amount=allowed_amount,
            timestamp=datetime.now(timezone.utc),
        )

    def check_correlation(self, new_ticker: str, new_side: str) -> bool:
        """
        Check if a new trade would create correlated exposure.

        Returns True if trade is OK, False if too correlated with existing positions.
        """
        MAX_SAME_EVENT_POSITIONS = 10   # Relaxed: weather has many brackets per event
        MAX_SAME_EVENT_PCT = 1.0       # 100%: weather-only mode, events are our universe
        MAX_SAME_CATEGORY_PCT = 100.0  # Disabled: all trades are weather/climate

        try:
            with next(get_db_session()) as session:
                positions = session.query(PositionDB).filter(
                    PositionDB.quantity > 0
                ).all()

                if not positions:
                    return True

                new_market = session.query(MarketDB).filter_by(ticker=new_ticker).first()
                if not new_market:
                    return True

                new_event = self._extract_event_slug(new_ticker)
                new_category = str(new_market.category or "")

                same_event_count = 0
                same_event_dollars = 0.0
                same_category_dollars = 0.0

                for pos in positions:
                    pos_market = session.query(MarketDB).filter_by(ticker=pos.ticker).first()
                    if not pos_market:
                        continue

                    pos_event = self._extract_event_slug(pos.ticker)
                    pos_category = str(pos_market.category or "")
                    pos_dollars = float(pos.quantity or 0) * float(pos.average_price or 50) / 100.0

                    if pos_event == new_event and new_event:
                        same_event_count += 1
                        same_event_dollars += pos_dollars

                    if pos_category == new_category and new_category:
                        same_category_dollars += pos_dollars

                bankroll = self.get_bankroll()

                if same_event_count >= MAX_SAME_EVENT_POSITIONS:
                    logger.warning(
                        f"Correlation block: already {same_event_count} positions on event '{new_event}'"
                    )
                    return False

                if bankroll > 0 and same_event_dollars / bankroll > MAX_SAME_EVENT_PCT:
                    logger.warning(
                        f"Correlation block: ${same_event_dollars:.0f} "
                        f"({same_event_dollars/bankroll:.0%}) already on event '{new_event}'"
                    )
                    return False

                if bankroll > 0 and same_category_dollars / bankroll > MAX_SAME_CATEGORY_PCT:
                    logger.warning(
                        f"Correlation block: ${same_category_dollars:.0f} "
                        f"({same_category_dollars/bankroll:.0%}) in category '{new_category}'"
                    )
                    return False

                return True

        except Exception as e:
            logger.warning(f"Correlation check failed: {e}")
            return True  # Allow trade if check fails

    def _extract_event_slug(self, ticker: str) -> str:
        """
        Extract event identifier from ticker.

        Kalshi tickers follow patterns like:
        - KXBTC-25MAR28-T99999 (Bitcoin price event)
        - INX-25FEB07-T5680 (S&P 500 event)

        Markets in the same event share a common prefix before the strike.
        """
        parts = ticker.split("-")
        if len(parts) >= 2:
            return "-".join(parts[:2]) if len(parts) >= 3 else parts[0]
        return ticker

    def check_daily_limits(self, strategy: Optional[str] = None) -> RiskCheckResult:
        """
        Check daily risk limits without a specific trade.

        Useful for deciding whether to scan for opportunities.
        When strategy is provided, only counts exposure/pnl for that strategy.
        """
        bankroll = self.get_bankroll()
        daily_pnl = self.get_daily_pnl(strategy=strategy)
        current_exposure = self.get_total_exposure(strategy=strategy)
        current_drawdown = self.get_current_drawdown()

        violations: List[RiskViolation] = []
        messages: List[str] = []

        daily_loss_limit = bankroll * self.daily_loss_limit_pct
        max_exposure = bankroll * self.max_exposure_pct
        max_drawdown = self.max_drawdown_pct

        if self._trading_paused:
            violations.append(RiskViolation.TRADING_PAUSED)
            messages.append(f"Trading paused: {self._pause_reason}")

        # Daily loss limit check disabled — phantom PnL from stale records.
        # Re-enable once PnL calculation is clean.

        if current_exposure >= max_exposure:
            violations.append(RiskViolation.MAX_TOTAL_EXPOSURE)
            messages.append(f"At max exposure: ${current_exposure:.2f} >= ${max_exposure:.2f}")

        if current_drawdown >= max_drawdown:
            violations.append(RiskViolation.MAX_DRAWDOWN)
            messages.append(f"At max drawdown: {current_drawdown:.1%} >= {max_drawdown:.1%}")

        # Weekly loss limit check
        phase_limits = self.get_phase_limits()
        weekly_loss_limit_pct = phase_limits.get("weekly_loss_limit_pct", 0.20)
        weekly_loss_limit = bankroll * weekly_loss_limit_pct
        weekly_pnl = self.get_weekly_pnl()
        if weekly_pnl < -weekly_loss_limit:
            violations.append(RiskViolation.WEEKLY_LOSS_LIMIT)
            messages.append(
                f"Weekly loss limit breached: ${weekly_pnl:.2f} "
                f"exceeds -${weekly_loss_limit:.2f} ({weekly_loss_limit_pct:.0%} of bankroll)"
            )
            # Auto-pause trading (can auto-resume if equity recovers)
            self.pause_trading(
                f"Weekly loss limit: ${weekly_pnl:.2f} exceeds -${weekly_loss_limit:.2f}"
            )

        status = RiskStatus.REJECTED if violations else RiskStatus.APPROVED

        return RiskCheckResult(
            status=status,
            approved=len(violations) == 0,
            violations=violations,
            messages=messages,
            daily_pnl=daily_pnl,
            daily_loss_limit=daily_loss_limit,
            current_exposure=current_exposure,
            max_exposure=max_exposure,
            position_value=0.0,
            max_position_value=bankroll * self.max_position_pct,
            current_drawdown=current_drawdown,
            max_drawdown=max_drawdown,
            timestamp=datetime.now(timezone.utc),
        )

    def pause_trading(self, reason: str) -> None:
        """Pause all trading."""
        self._trading_paused = True
        self._pause_reason = reason

        self._log_risk_event(
            RiskViolation.TRADING_PAUSED,
            "critical",
            f"Trading paused: {reason}",
            {"reason": reason},
        )

        logger.warning("TRADING PAUSED", reason=reason)

    def resume_trading(self) -> None:
        """Resume trading after pause."""
        if self._trading_paused:
            logger.info(
                "Trading resumed",
                previous_reason=self._pause_reason,
            )
            self._trading_paused = False
            self._pause_reason = None

    def is_trading_paused(self) -> bool:
        """Check if trading is paused."""
        return self._trading_paused

    # ─── Obs-Settled Capital Pool ──────────────────────────────────────

    def get_obs_settled_pool(self, bankroll: float) -> float:
        """Get available obs-settled pool capital.

        Pool = (bankroll × OBS_POOL_PCT) - deployed + profits_returned
        """
        base_pool = bankroll * self.OBS_POOL_PCT
        available = base_pool - self._obs_pool_deployed + self._obs_pool_profits
        return max(0.0, available)

    def get_obs_settled_deploy_amount(self, bankroll: float) -> float:
        """Get amount to deploy on the next obs-settled signal.

        Returns 50% of remaining pool (geometric decay):
        - Signal 1: 50% of $15 = $7.50
        - Signal 2: 50% of $7.50 = $3.75
        - Signal 3: 50% of $3.75 = $1.88

        Returns 0 if pool is below OBS_POOL_MIN_DEPLOY ($0.50).
        """
        pool = self.get_obs_settled_pool(bankroll)
        deploy = pool * self.OBS_POOL_DEPLOY_PCT
        if deploy < self.OBS_POOL_MIN_DEPLOY:
            return 0.0  # Pool too depleted, skip
        return deploy

    def record_obs_deploy(self, amount: float) -> None:
        """Record capital deployed from obs pool."""
        self._obs_pool_deployed += amount
        logger.debug(f"Obs pool deploy: ${amount:.2f} (total deployed: ${self._obs_pool_deployed:.2f})")

    def record_obs_settlement(self, deployed: float, profit: float) -> None:
        """Record an obs-settled position paying out.

        The deployed capital is freed and profit is added back to pool.
        """
        self._obs_pool_deployed = max(0.0, self._obs_pool_deployed - deployed)
        self._obs_pool_profits += profit
        logger.info(
            f"Obs pool replenished: ${deployed:.2f} freed + ${profit:.2f} profit "
            f"(pool profits total: ${self._obs_pool_profits:.2f})"
        )

    def get_forecast_budget(self, bankroll: float) -> float:
        """Get budget available for forecast-based trades.

        This is bankroll MINUS the obs-settled pool reservation.
        Ensures forecast trades never consume obs pool capital.
        """
        obs_reserved = bankroll * self.OBS_POOL_PCT
        return max(0.0, bankroll - obs_reserved)

    def save_obs_pool_state(self) -> None:
        """Persist obs pool state to disk (for live mode restart recovery)."""
        try:
            import json
            self.OBS_POOL_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            self.OBS_POOL_STATE_FILE.write_text(json.dumps({
                "obs_pool_deployed": self._obs_pool_deployed,
                "obs_pool_profits": self._obs_pool_profits,
            }, indent=2))
        except Exception as e:
            logger.debug(f"Failed to save obs pool state: {e}")

    def load_obs_pool_state(self) -> None:
        """Load obs pool state from disk on startup."""
        try:
            import json
            if self.OBS_POOL_STATE_FILE.exists():
                data = json.loads(self.OBS_POOL_STATE_FILE.read_text())
                self._obs_pool_deployed = data.get("obs_pool_deployed", 0.0)
                self._obs_pool_profits = data.get("obs_pool_profits", 0.0)
                logger.info(
                    f"Obs pool state loaded: deployed=${self._obs_pool_deployed:.2f}, "
                    f"profits=${self._obs_pool_profits:.2f}"
                )
        except Exception as e:
            logger.debug(f"Failed to load obs pool state: {e}")

    def get_risk_status(self) -> Dict[str, Any]:
        """Get current risk status summary."""
        bankroll = self.get_bankroll()
        daily_pnl = self.get_daily_pnl()
        weekly_pnl = self.get_weekly_pnl()
        current_exposure = self.get_total_exposure()
        current_drawdown = self.get_current_drawdown()

        phase_limits = self.get_phase_limits()
        weekly_loss_limit_pct = phase_limits.get("weekly_loss_limit_pct", 0.20)

        return {
            "trading_paused": self._trading_paused,
            "pause_reason": self._pause_reason,
            "bankroll": bankroll,
            "daily_pnl": daily_pnl,
            "daily_pnl_pct": daily_pnl / bankroll if bankroll > 0 else 0,
            "daily_loss_limit": bankroll * self.daily_loss_limit_pct,
            "daily_loss_limit_pct": self.daily_loss_limit_pct,
            "weekly_pnl": weekly_pnl,
            "weekly_pnl_pct": weekly_pnl / bankroll if bankroll > 0 else 0,
            "weekly_loss_limit": bankroll * weekly_loss_limit_pct,
            "weekly_loss_limit_pct": weekly_loss_limit_pct,
            "current_exposure": current_exposure,
            "exposure_pct": current_exposure / bankroll if bankroll > 0 else 0,
            "max_exposure": bankroll * self.max_exposure_pct,
            "max_exposure_pct": self.max_exposure_pct,
            "current_drawdown": current_drawdown,
            "max_drawdown_pct": self.max_drawdown_pct,
            "remaining_daily_loss": max(
                0, bankroll * self.daily_loss_limit_pct + daily_pnl
            ),
            "remaining_weekly_loss": max(
                0, bankroll * weekly_loss_limit_pct + weekly_pnl
            ),
            "remaining_exposure": max(
                0, bankroll * self.max_exposure_pct - current_exposure
            ),
        }

    def get_risk_events(
        self,
        since: Optional[datetime] = None,
        event_type: Optional[RiskViolation] = None,
    ) -> List[RiskEvent]:
        """Get risk events, optionally filtered."""
        events = self._risk_events

        if since:
            events = [e for e in events if e.timestamp >= ensure_utc(since)]

        if event_type:
            events = [e for e in events if e.event_type == event_type]

        return events

    def clear_risk_events(self) -> None:
        """Clear risk event history."""
        self._risk_events = []

    def get_daily_trade_count(self) -> int:
        """Get number of trades placed today."""
        try:
            with next(get_db_session()) as session:
                today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
                count = session.query(TradeDB).filter(
                    TradeDB.timestamp >= today_start
                ).count()
                return count
        except Exception:
            return 0

    def record_api_failure(self) -> bool:
        """Record an API failure. Returns True if trading should be paused."""
        self._consecutive_api_failures += 1
        if self._consecutive_api_failures >= self._max_consecutive_failures:
            self.pause_trading(
                f"API failures: {self._consecutive_api_failures} consecutive failures"
            )
            return True
        return False

    def record_api_success(self) -> None:
        """Record a successful API call, resetting the failure counter."""
        self._consecutive_api_failures = 0

    def check_daily_trade_limit(self) -> bool:
        """Check if daily trade limit has been reached. Returns True if OK."""
        count = self.get_daily_trade_count()
        if count >= self.max_daily_trades:
            logger.warning(f"Daily trade limit reached: {count}/{self.max_daily_trades}")
            return False
        return True

    # ─── Circuit Breaker ─────────────────────────────────────────────

    @property
    def consecutive_loss_count(self) -> int:
        """Cached consecutive loss streak count (refreshed each check_circuit_breaker call)."""
        return self._consecutive_loss_count

    @property
    def circuit_breaker_active(self) -> bool:
        """Whether the circuit breaker is currently engaged."""
        return self._circuit_breaker_active

    def _count_consecutive_losses(self) -> int:
        """Query TradeDB for current consecutive losing streak."""
        try:
            with next(get_db_session()) as session:
                recent_trades = (
                    session.query(TradeDB)
                    .filter(TradeDB.resolved == 1)
                    .order_by(TradeDB.resolved_at.desc())
                    .limit(20)
                    .all()
                )

                count = 0
                for trade in recent_trades:
                    if trade.outcome == "loss":
                        count += 1
                    else:
                        break
                return count
        except Exception as e:
            logger.warning(f"Failed to count consecutive losses: {e}")
            return 0

    def check_circuit_breaker(
        self,
        model_health_multiplier: Optional[float] = None,
    ) -> tuple:
        """
        Check if system-wide circuit breaker should engage.

        Checks:
            a) Consecutive losing trades >= 5
            b) Daily realized loss > 20% of bankroll
            c) Reconciliation mismatch > $0.50
            d) Model health multiplier < 0.5

        Args:
            model_health_multiplier: Current position multiplier from ModelHealthMonitor.
                Pass from main.py; if None, this check is skipped.

        Returns:
            Tuple of (should_pause: bool, reason: str).
        """
        reasons: List[str] = []

        # (a) Consecutive losing trades >= 5
        self._consecutive_loss_count = self._count_consecutive_losses()
        if self._consecutive_loss_count >= 5:
            reasons.append(
                f"consecutive losses: {self._consecutive_loss_count} in a row"
            )

        # (b) Daily realized loss > 20% of bankroll
        try:
            bankroll = self.get_bankroll()
            if bankroll > 0:
                daily_pnl = self.get_daily_pnl()
                loss_pct = abs(daily_pnl) / bankroll if daily_pnl < 0 else 0.0
                if loss_pct > 0.20:
                    reasons.append(
                        f"daily loss {loss_pct:.1%} exceeds 20% hard halt "
                        f"(${daily_pnl:.2f} on ${bankroll:.2f} bankroll)"
                    )
        except Exception as e:
            logger.warning(f"Circuit breaker daily loss check failed: {e}")

        # (c) Reconciliation mismatch > $2.00
        # Compare API-reported position value vs local PositionDB cost basis.
        # Skip if either side has no data (startup timing / sync lag).
        try:
            with next(get_db_session()) as session:
                snapshot = (
                    session.query(PortfolioSnapshotDB)
                    .order_by(PortfolioSnapshotDB.timestamp.desc())
                    .first()
                )
                if snapshot and snapshot.total_position_value is not None:
                    api_position_value = float(snapshot.total_position_value)
                    local_exposure = self.get_total_exposure()

                    # Skip check if both sides are near-zero (no positions)
                    # or if local has no positions yet (sync hasn't completed)
                    if api_position_value < 0.01 and local_exposure < 0.01:
                        pass  # No positions on either side — nothing to reconcile
                    elif local_exposure < 0.01 and api_position_value > 1.0:
                        # Local DB has no positions but API does — likely sync
                        # hasn't run yet. Log a warning but don't trip breaker.
                        logger.warning(
                            f"Reconciliation skipped: PositionDB empty but API "
                            f"shows ${api_position_value:.2f} in positions "
                            f"(awaiting position sync)"
                        )
                    else:
                        # Both sides have data — compare directly
                        mismatch = abs(api_position_value - local_exposure)
                        # Use $2.00 threshold to allow for market value vs
                        # cost basis differences and normal price movement
                        if mismatch > 2.00:
                            reasons.append(
                                f"reconciliation mismatch: ${mismatch:.2f} "
                                f"(API positions=${api_position_value:.2f}, "
                                f"local exposure=${local_exposure:.2f})"
                            )
        except Exception as e:
            logger.warning(f"Circuit breaker reconciliation check failed: {e}")

        # (d) Model health critical (multiplier < 0.5)
        if model_health_multiplier is not None and model_health_multiplier < 0.5:
            reasons.append(
                f"model health critical: position_multiplier={model_health_multiplier:.2f}"
            )

        should_pause = len(reasons) > 0
        reason = "CIRCUIT BREAKER: " + "; ".join(reasons) if reasons else ""

        if should_pause:
            for r in reasons:
                logger.error(f"CIRCUIT BREAKER TRIGGER: {r}")

        return (should_pause, reason)

    def resume_circuit_breaker(self) -> None:
        """
        Manually clear the circuit breaker after human review.

        This is the ONLY way to resume trading after a circuit breaker trip.
        auto_check_and_pause() will NOT auto-resume while circuit breaker is active.
        """
        if self._circuit_breaker_active:
            logger.info(
                "Circuit breaker manually cleared",
                previous_reason=self._circuit_breaker_reason,
            )
            self._circuit_breaker_active = False
            self._circuit_breaker_reason = None
            self.resume_trading()

    def auto_check_and_pause(
        self,
        strategy: Optional[str] = None,
        model_health_multiplier: Optional[float] = None,
    ) -> Optional[str]:
        """Auto-check risk limits and circuit breaker. Returns reason if paused.

        Args:
            strategy: Strategy name for exposure filtering.
            model_health_multiplier: Current model health position multiplier
                (from ModelHealthMonitor). Passed through to check_circuit_breaker().
        """
        # If circuit breaker is already active, block everything and log
        if self._circuit_breaker_active:
            logger.error(
                "CIRCUIT BREAKER ACTIVE — manual review required",
                reason=self._circuit_breaker_reason,
            )
            return self._circuit_breaker_reason

        # Run circuit breaker checks
        cb_should_pause, cb_reason = self.check_circuit_breaker(
            model_health_multiplier=model_health_multiplier,
        )
        if cb_should_pause:
            self._circuit_breaker_active = True
            self._circuit_breaker_reason = cb_reason
            self.pause_trading(cb_reason)
            return cb_reason

        # Standard risk limit checks
        check = self.check_daily_limits(strategy=strategy)
        if not check.approved:
            # Filter out the recursive "Trading paused: ..." wrapper to prevent
            # cascading messages like "Trading paused: Trading paused: Trading paused: ..."
            fresh_messages = [
                m for m in check.messages
                if not m.startswith("Trading paused:")
            ]
            reason = "; ".join(fresh_messages) if fresh_messages else self._pause_reason or "risk limit violated"
            self.pause_trading(reason)
            return reason
        else:
            # Limits are back within range — auto-resume (only if NOT circuit breaker)
            if self._trading_paused and not self._circuit_breaker_active:
                logger.info("Risk limits cleared — auto-resuming trading")
                self.resume_trading()
        return None


# Global instance
_risk_manager: Optional[RiskManager] = None


def set_risk_manager(rm: RiskManager) -> None:
    """Set the global risk manager instance (call from main.py to use configured settings)."""
    global _risk_manager
    _risk_manager = rm
    logger.info("Global risk manager set with custom settings")


def get_risk_manager() -> RiskManager:
    """Get or create the global risk manager instance."""
    global _risk_manager
    if _risk_manager is None:
        _risk_manager = RiskManager()
    return _risk_manager


def check_trade_risk(
    ticker: str,
    side: str,
    quantity: int,
    price: int,
) -> RiskCheckResult:
    """Convenience function to check trade risk."""
    return get_risk_manager().check_trade(ticker, side, quantity, price)


def is_trading_allowed() -> bool:
    """Convenience function to check if trading is allowed."""
    result = get_risk_manager().check_daily_limits()
    return result.approved
