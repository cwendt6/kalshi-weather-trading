"""
Order execution engine for Kalshi trading.

Handles order placement with risk checks, validation,
partial fills, timeouts, and cancellation.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
import uuid

from src.data.database import get_db_session
from src.data.models import MarketDB, PositionDB, TradeDB
from src.execution.risk_manager import RiskCheckResult, get_risk_manager
from src.utils.fees import KALSHI_WINNER_FEE_RATE
from src.utils.logging import logger


class OrderStatus(Enum):
    """Order status states."""

    PENDING = "pending"
    SUBMITTED = "submitted"
    PARTIAL = "partial"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"
    FAILED = "failed"


class OrderSide(Enum):
    """Order side."""

    YES = "yes"
    NO = "no"


class OrderAction(Enum):
    """Order action."""

    BUY = "buy"
    SELL = "sell"


@dataclass
class Order:
    """An order to be executed."""

    order_id: str
    ticker: str
    side: OrderSide
    action: OrderAction
    quantity: int
    price: int  # Limit price in cents

    status: OrderStatus = OrderStatus.PENDING
    filled_quantity: int = 0
    average_fill_price: int = 0
    fees: float = 0.0

    # Risk check
    risk_approved: bool = False
    risk_check: Optional[RiskCheckResult] = None

    # Timing
    created_at: datetime = datetime.now(timezone.utc)
    submitted_at: Optional[datetime] = None
    filled_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None

    # Kalshi order ID (after submission)
    exchange_order_id: Optional[str] = None

    # Error tracking
    error_message: Optional[str] = None

    @property
    def is_complete(self) -> bool:
        """Check if order is in a terminal state."""
        return self.status in (
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
            OrderStatus.FAILED,
        )

    @property
    def remaining_quantity(self) -> int:
        """Quantity remaining to be filled."""
        return self.quantity - self.filled_quantity

    @property
    def notional_value(self) -> float:
        """Total notional value of order in dollars."""
        return self.quantity * self.price / 100.0


@dataclass
class ExecutionResult:
    """Result of an execution attempt."""

    success: bool
    order: Order
    message: str
    trade_id: Optional[str] = None


class OrderExecutor:
    """
    Executes orders on Kalshi with safety checks.

    Features:
    - Risk limit validation before every order
    - Parameter validation
    - Limit orders only (no market orders)
    - Partial fill handling
    - Order timeout and cancellation
    - Full activity logging
    """

    # Default timeout for orders
    DEFAULT_TIMEOUT_SECONDS = 300  # 5 minutes

    # Minimum/maximum order constraints
    MIN_QUANTITY = 1
    MAX_QUANTITY = 10000
    MIN_PRICE = 1
    MAX_PRICE = 99

    def __init__(
        self,
        paper_trading: bool = True,
        default_timeout: int = 300,
        kalshi_client: Optional[Any] = None,
    ) -> None:
        """
        Initialize order executor.

        Args:
            paper_trading: If True, simulate orders without real execution.
            default_timeout: Default order timeout in seconds.
            kalshi_client: Optional Kalshi API client for live trading.
        """
        self.paper_trading = paper_trading
        self.default_timeout = default_timeout
        self.kalshi_client = kalshi_client

        # Order tracking
        self._orders: Dict[str, Order] = {}
        self._pending_orders: List[str] = []

        logger.info(
            "Order executor initialized",
            paper_trading=paper_trading,
            default_timeout=default_timeout,
        )

    def _generate_order_id(self) -> str:
        """Generate unique order ID."""
        return f"ORD-{uuid.uuid4().hex[:12].upper()}"

    def _validate_order(self, order: Order) -> tuple[bool, str]:
        """
        Validate order parameters.

        Returns:
            Tuple of (is_valid, error_message).
        """
        # Check quantity
        if order.quantity < self.MIN_QUANTITY:
            return False, f"Quantity {order.quantity} below minimum {self.MIN_QUANTITY}"
        if order.quantity > self.MAX_QUANTITY:
            return False, f"Quantity {order.quantity} above maximum {self.MAX_QUANTITY}"

        # Check price
        if order.price < self.MIN_PRICE:
            return False, f"Price {order.price} below minimum {self.MIN_PRICE}"
        if order.price > self.MAX_PRICE:
            return False, f"Price {order.price} above maximum {self.MAX_PRICE}"

        # Check ticker exists
        with next(get_db_session()) as session:
            market = session.query(MarketDB).filter_by(ticker=order.ticker).first()
            if not market:
                return False, f"Market {order.ticker} not found"
            if market.status != "active":
                return False, f"Market {order.ticker} is not active (status: {market.status})"

        return True, ""

    def _check_risk(self, order: Order) -> RiskCheckResult:
        """Check order against risk limits."""
        risk_manager = get_risk_manager()
        return risk_manager.check_trade(
            ticker=order.ticker,
            side=order.side.value,
            quantity=order.quantity,
            price=order.price,
        )

    def create_order(
        self,
        ticker: str,
        side: str,
        action: str,
        quantity: int,
        price: int,
        timeout_seconds: Optional[int] = None,
    ) -> Order:
        """
        Create a new order (does not submit).

        Args:
            ticker: Market ticker.
            side: "yes" or "no".
            action: "buy" or "sell".
            quantity: Number of contracts.
            price: Limit price in cents.
            timeout_seconds: Order timeout (uses default if None).

        Returns:
            Created Order object.
        """
        order_id = self._generate_order_id()
        timeout = timeout_seconds or self.default_timeout

        order = Order(
            order_id=order_id,
            ticker=ticker,
            side=OrderSide(side),
            action=OrderAction(action),
            quantity=quantity,
            price=price,
            created_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=timeout),
        )

        self._orders[order_id] = order

        logger.info(
            "Order created",
            order_id=order_id,
            ticker=ticker,
            side=side,
            action=action,
            quantity=quantity,
            price=price,
        )

        return order

    def submit_order(
        self, order: Order, skip_risk: bool = False, strategy: Optional[str] = None,
        edge: Optional[float] = None,
    ) -> ExecutionResult:
        """
        Submit an order for execution.

        Validates parameters and risk limits before submission.

        Args:
            order: Order to submit.
            skip_risk: If True, bypass all risk checks entirely. Used for
                       time decay entries (high-conviction, short-duration)
                       and forced exit escalation.
            strategy: Trading strategy name for trade records.

        Returns:
            ExecutionResult with success/failure info.
        """
        # Validate order parameters
        is_valid, error_msg = self._validate_order(order)
        if not is_valid:
            order.status = OrderStatus.REJECTED
            order.error_message = error_msg
            logger.warning("Order rejected - validation failed", order_id=order.order_id, error=error_msg)
            return ExecutionResult(
                success=False,
                order=order,
                message=f"Validation failed: {error_msg}",
            )

        # Risk checks — bypass for sells, skip_risk flag, or testing mode
        if order.action == OrderAction.SELL or skip_risk:
            order.risk_approved = True
            if skip_risk and order.action != OrderAction.SELL:
                logger.debug(f"Risk check BYPASSED for {order.order_id} (skip_risk=True)")
            elif order.action == OrderAction.SELL:
                logger.debug(f"Risk check skipped for SELL order {order.order_id} (exits always allowed)")
        else:
            risk_result = self._check_risk(order)
            order.risk_check = risk_result
            order.risk_approved = risk_result.approved

            if not risk_result.approved:
                order.status = OrderStatus.REJECTED
                order.error_message = "; ".join(risk_result.messages)
                logger.warning(
                    "Order rejected - risk check failed",
                    order_id=order.order_id,
                    violations=[v.value for v in risk_result.violations],
                )
                return ExecutionResult(
                    success=False,
                    order=order,
                    message=f"Risk check failed: {order.error_message}",
                )

        # Submit order
        order.status = OrderStatus.SUBMITTED
        order.submitted_at = datetime.now(timezone.utc)
        self._pending_orders.append(order.order_id)

        if self.paper_trading:
            # Paper trading - simulate immediate fill
            return self._simulate_fill(order, strategy=strategy, edge=edge)
        else:
            # Live trading - submit to Kalshi
            return self._submit_to_kalshi(order, strategy=strategy, edge=edge)

    def _simulate_fill(
        self, order: Order, strategy: Optional[str] = None,
        edge: Optional[float] = None,
    ) -> ExecutionResult:
        """Simulate order fill for paper trading."""
        # Simulate immediate full fill at limit price
        order.status = OrderStatus.FILLED
        order.filled_quantity = order.quantity
        order.average_fill_price = order.price
        order.filled_at = datetime.now(timezone.utc)

        # Calculate fees (2% on winning side, but we don't know outcome yet)
        # For paper trading, estimate 1% average fee
        order.fees = order.notional_value * 0.01

        # Store trade record
        self._store_trade(order, strategy=strategy, edge=edge)

        # Update position
        self._update_position(order)

        # Remove from pending
        if order.order_id in self._pending_orders:
            self._pending_orders.remove(order.order_id)

        logger.info(
            "Order filled (paper)",
            order_id=order.order_id,
            ticker=order.ticker,
            quantity=order.filled_quantity,
            price=order.average_fill_price,
        )

        return ExecutionResult(
            success=True,
            order=order,
            message="Order filled (paper trading)",
            trade_id=order.order_id,
        )

    def _submit_to_kalshi(self, order: Order, strategy: Optional[str] = None, edge: Optional[float] = None) -> ExecutionResult:
        """Submit order to Kalshi API."""
        from src.api.kalshi_client import KalshiClient

        async def _check_balance_and_place_order():
            """Check cash balance, then place order within proper async context.

            IMPORTANT: Always creates a fresh KalshiClient inside the async
            context.  When called via asyncio.run() in a ThreadPoolExecutor,
            the pre-initialised self.kalshi_client's httpx connection pool is
            bound to the *main* event loop, which is unreachable from the
            worker thread.  A fresh client gets its own connections on the
            thread's event loop and avoids 'Event loop is closed' errors.
            """
            async with KalshiClient() as client:
                return await _do_order(client)

        async def _do_order(client):
            """Execute the order against Kalshi API."""
            # Pre-trade balance check: only for BUY orders.
            # SELL orders don't require cash — you're selling contracts
            # you already own back to the market.
            if order.action == OrderAction.BUY:
                try:
                    balance = await client.get_balance()
                    cash_cents = balance.balance  # balance is in cents
                    order_cost_cents = order.quantity * order.price
                    if cash_cents < order_cost_cents:
                        cash_dollars = cash_cents / 100.0
                        cost_dollars = order_cost_cents / 100.0
                        raise ValueError(
                            f"Insufficient cash: ${cash_dollars:.2f} available, "
                            f"${cost_dollars:.2f} needed for "
                            f"{order.quantity}x @{order.price}c"
                        )
                except ValueError:
                    raise  # Re-raise our balance check error
                except Exception as e:
                    # If balance check fails, log but still try the order
                    logger.warning(
                        f"Balance pre-check failed (proceeding anyway): {e}"
                    )

            return await client.place_order(
                ticker=order.ticker,
                side=order.side.value,
                action=order.action.value,
                quantity=order.quantity,
                order_type="limit",
                price=order.price,
            )

        try:
            import asyncio
            import concurrent.futures

            # Run the async place_order call from sync context
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop and loop.is_running():
                # Already in an async context — use a thread to avoid blocking
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future = pool.submit(
                        asyncio.run,
                        _check_balance_and_place_order(),
                    )
                    api_order = future.result(timeout=30)
            else:
                api_order = asyncio.run(_check_balance_and_place_order())

            # Update our order with exchange response
            if api_order and hasattr(api_order, "order_id"):
                order.exchange_order_id = str(api_order.order_id)
                order.status = OrderStatus.SUBMITTED
                order.submitted_at = datetime.now(timezone.utc)

                logger.info(
                    "Order submitted to Kalshi",
                    order_id=order.order_id,
                    exchange_id=order.exchange_order_id,
                    ticker=order.ticker,
                    side=order.side.value,
                    quantity=order.quantity,
                    price=order.price,
                )

                # Store trade and update position
                self._store_trade(order, strategy=strategy, edge=edge)
                self._update_position(order)

                # Remove from pending
                if order.order_id in self._pending_orders:
                    self._pending_orders.remove(order.order_id)

                return ExecutionResult(
                    success=True,
                    order=order,
                    message=f"Order submitted: {order.exchange_order_id}",
                    trade_id=order.exchange_order_id,
                )
            else:
                order.status = OrderStatus.FAILED
                order.error_message = "No order response from Kalshi"
                return ExecutionResult(
                    success=False,
                    order=order,
                    message="Kalshi returned no order response",
                )

        except Exception as e:
            order.status = OrderStatus.FAILED
            order.error_message = str(e)
            logger.error(
                f"Order submission failed | {order.ticker} "
                f"{order.side.value} x{order.quantity} @{order.price}c | "
                f"{type(e).__name__}: {e}"
            )
            return ExecutionResult(
                success=False,
                order=order,
                message=f"Submission failed: {e}",
            )

    async def check_order_status(self, exchange_order_id: str) -> Optional[str]:
        """Check fill status of a submitted order on Kalshi."""
        if self.kalshi_client is None:
            return None
        try:
            orders = await self.kalshi_client.get_orders(status=None)
            for o in orders:
                if hasattr(o, "order_id") and str(o.order_id) == exchange_order_id:
                    return o.status if hasattr(o, "status") else None
            return None
        except Exception as e:
            logger.debug(f"Order status check failed: {e}")
            return None

    def _store_trade(self, order: Order, strategy: Optional[str] = None, edge: Optional[float] = None) -> None:
        """Store trade record in database. Calculates PnL for sell orders.

        For live orders that are SUBMITTED but not yet filled, we store the
        order's intended quantity and price (not filled_quantity/average_fill_price
        which are still 0).
        """
        with next(get_db_session()) as session:
            pnl_val = None
            pnl_pct = None
            outcome_val = None
            resolved_val = 0
            resolved_at_val = None

            # Use filled values if available, otherwise use order values
            # (live orders are SUBMITTED before fill, so filled_quantity=0)
            stored_qty = order.filled_quantity if order.filled_quantity > 0 else order.quantity
            stored_price = order.average_fill_price if order.average_fill_price > 0 else order.price

            if order.action == OrderAction.SELL and stored_qty > 0:
                # Look up entry price from PositionDB for PnL calculation
                position = session.query(PositionDB).filter_by(
                    ticker=order.ticker
                ).first()
                entry_price = int(position.average_price) if position else 0

                if entry_price > 0:
                    exit_price = stored_price
                    if order.side.value == "yes":
                        gross_pnl = stored_qty * (exit_price - entry_price) / 100.0
                    else:
                        gross_pnl = stored_qty * (entry_price - exit_price) / 100.0
                    fees = gross_pnl * KALSHI_WINNER_FEE_RATE if gross_pnl > 0 else 0.0
                    net_pnl = gross_pnl - fees
                    cost_basis = stored_qty * entry_price / 100.0

                    pnl_val = round(net_pnl, 2)
                    pnl_pct = round((net_pnl / cost_basis) * 100 if cost_basis > 0 else 0, 2)
                    outcome_val = "win" if net_pnl > 0 else "loss"
                    resolved_val = 1
                    resolved_at_val = datetime.now(timezone.utc)

            trade = TradeDB(
                order_id=order.order_id,
                fill_id=f"FILL-{order.order_id}",
                ticker=order.ticker,
                side=order.side.value,
                action=order.action.value,
                quantity=stored_qty,
                price=stored_price,
                fee=order.fees,
                status=order.status.value,
                timestamp=order.filled_at or datetime.now(timezone.utc),
                strategy=strategy,
                pnl=pnl_val,
                pnl_percent=pnl_pct,
                outcome=outcome_val,
                resolved=resolved_val,
                resolved_at=resolved_at_val,
                edge=edge,
            )
            session.add(trade)
            session.commit()

    def _update_position(self, order: Order) -> None:
        """Update position after fill."""
        with next(get_db_session()) as session:
            position = session.query(PositionDB).filter_by(ticker=order.ticker).first()

            if order.action == OrderAction.BUY:
                if position:
                    # Update existing position
                    old_qty = int(position.quantity or 0)
                    old_avg = int(position.average_price or 0)
                    new_qty = old_qty + order.filled_quantity
                    # Weighted average price
                    new_avg = (
                        (old_qty * old_avg + order.filled_quantity * order.average_fill_price)
                        / new_qty
                        if new_qty > 0
                        else 0
                    )
                    # Use setattr to avoid SQLAlchemy type issues
                    setattr(position, "quantity", new_qty)
                    setattr(position, "average_price", int(new_avg))
                    setattr(position, "side", order.side.value)
                else:
                    # Create new position
                    position = PositionDB(
                        ticker=order.ticker,
                        side=order.side.value,
                        quantity=order.filled_quantity,
                        average_price=order.average_fill_price,
                    )
                    session.add(position)
            else:
                # SELL - reduce position
                if position:
                    current_qty = int(position.quantity or 0)
                    new_qty = max(0, current_qty - order.filled_quantity)
                    setattr(position, "quantity", new_qty)
                    if new_qty == 0:
                        session.delete(position)

            session.commit()

    def cancel_order(self, order_id: str) -> ExecutionResult:
        """
        Cancel a pending order.

        Args:
            order_id: ID of order to cancel.

        Returns:
            ExecutionResult with cancellation status.
        """
        order = self._orders.get(order_id)
        if not order:
            return ExecutionResult(
                success=False,
                order=Order(
                    order_id=order_id,
                    ticker="",
                    side=OrderSide.YES,
                    action=OrderAction.BUY,
                    quantity=0,
                    price=0,
                    status=OrderStatus.FAILED,
                    error_message="Order not found",
                ),
                message="Order not found",
            )

        if order.is_complete:
            return ExecutionResult(
                success=False,
                order=order,
                message=f"Cannot cancel order in {order.status.value} state",
            )

        order.status = OrderStatus.CANCELLED

        if order_id in self._pending_orders:
            self._pending_orders.remove(order_id)

        logger.info("Order cancelled", order_id=order_id)

        return ExecutionResult(
            success=True,
            order=order,
            message="Order cancelled",
        )

    def check_expired_orders(self) -> List[Order]:
        """Check for and expire timed-out orders."""
        now = datetime.now(timezone.utc)
        expired = []

        for order_id in list(self._pending_orders):
            order = self._orders.get(order_id)
            if order and order.expires_at and now > order.expires_at:
                order.status = OrderStatus.EXPIRED
                self._pending_orders.remove(order_id)
                expired.append(order)
                logger.info("Order expired", order_id=order_id)

        return expired

    def get_order(self, order_id: str) -> Optional[Order]:
        """Get order by ID."""
        return self._orders.get(order_id)

    def get_pending_orders(self) -> List[Order]:
        """Get all pending orders."""
        return [
            self._orders[oid]
            for oid in self._pending_orders
            if oid in self._orders
        ]

    def check_and_reconcile_fills(self) -> List[dict]:
        """Check submitted live orders for partial/full fills and reconcile.

        Live orders may remain in SUBMITTED state after placement.
        This method checks their fill status on Kalshi and updates
        local records accordingly.

        Returns list of reconciled order summaries.
        """
        if self.paper_trading:
            return []

        reconciled = []
        for order_id in list(self._pending_orders):
            order = self._orders.get(order_id)
            if not order or not order.exchange_order_id:
                continue

            try:
                import asyncio

                async def _check(eid: str):
                    from src.api.kalshi_client import KalshiClient
                    async with KalshiClient() as client:
                        orders = await client.get_orders(status=None)
                        for o in orders:
                            if hasattr(o, "order_id") and str(o.order_id) == eid:
                                return o
                    return None

                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None

                if loop and loop.is_running():
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor() as pool:
                        future = pool.submit(asyncio.run, _check(order.exchange_order_id))
                        api_order = future.result(timeout=15)
                else:
                    api_order = asyncio.run(_check(order.exchange_order_id))

                if not api_order:
                    continue

                status = getattr(api_order, "status", None)
                filled_qty = getattr(api_order, "filled_quantity", 0) or 0
                avg_price = getattr(api_order, "average_fill_price", 0) or 0

                if filled_qty > order.filled_quantity:
                    order.filled_quantity = filled_qty
                    order.average_fill_price = avg_price

                    if status == "filled" or filled_qty >= order.quantity:
                        order.status = OrderStatus.FILLED
                        order.filled_at = datetime.now(timezone.utc)
                        if order_id in self._pending_orders:
                            self._pending_orders.remove(order_id)
                    elif filled_qty > 0:
                        order.status = OrderStatus.PARTIAL

                    reconciled.append({
                        "order_id": order_id,
                        "exchange_id": order.exchange_order_id,
                        "ticker": order.ticker,
                        "filled": filled_qty,
                        "total": order.quantity,
                        "status": order.status.value,
                    })

                    logger.info(
                        f"Reconciled fill: {order.ticker} "
                        f"{filled_qty}/{order.quantity} @ {avg_price}c "
                        f"({order.status.value})"
                    )

            except Exception as e:
                logger.debug(f"Fill reconciliation failed for {order_id}: {e}")

        return reconciled

    def get_order_history(self, limit: int = 100) -> List[Order]:
        """Get recent order history."""
        orders = sorted(
            self._orders.values(),
            key=lambda o: o.created_at,
            reverse=True,
        )
        return orders[:limit]

    def execute(
        self,
        ticker: str,
        side: str,
        action: str,
        quantity: int,
        price: int,
        skip_risk: bool = False,
        strategy: Optional[str] = None,
        edge: Optional[float] = None,
    ) -> ExecutionResult:
        """
        Convenience method to create and submit order in one call.

        Args:
            ticker: Market ticker.
            side: "yes" or "no".
            action: "buy" or "sell".
            quantity: Number of contracts.
            price: Limit price in cents.
            skip_risk: If True, bypass all risk checks (used for time decay
                       entries and other high-conviction paths).
            strategy: Trading strategy name (e.g., "weather_no_hold").
            edge: Predicted edge at entry time (decimal, e.g. 0.08 = 8%).

        Returns:
            ExecutionResult.
        """
        order = self.create_order(
            ticker=ticker,
            side=side,
            action=action,
            quantity=quantity,
            price=price,
        )
        return self.submit_order(order, skip_risk=skip_risk, strategy=strategy, edge=edge)


# Global instance
_executor: Optional[OrderExecutor] = None


def get_executor(paper_trading: bool = True, kalshi_client: Optional[Any] = None) -> OrderExecutor:
    """Get or create the global order executor instance."""
    global _executor
    if _executor is None:
        _executor = OrderExecutor(paper_trading=paper_trading, kalshi_client=kalshi_client)
    elif kalshi_client is not None and _executor.kalshi_client is None:
        # Update existing executor with client for live trading
        _executor.kalshi_client = kalshi_client
    return _executor


def execute_order(
    ticker: str,
    side: str,
    action: str,
    quantity: int,
    price: int,
) -> ExecutionResult:
    """Convenience function to execute an order."""
    return get_executor().execute(ticker, side, action, quantity, price)
