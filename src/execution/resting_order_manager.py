"""
Resting order manager: posts limit sell orders that sit on the book waiting for buyers.

Problem: The position re-evaluator wants to exit KXDALSNOWM-26FEB-2.0, but there
are no bids. The old code would just log "no bid price available" and retry next
cycle, forever stuck. This is the #1 cause of the cash deadlock.

Solution: Instead of requiring an existing bid, POST an ask order at a reasonable
price and let it sit on the order book. Re-evaluate periodically and lower the
price if it hasn't filled (escalating aggressiveness).

Flow:
1. Position re-evaluator recommends exit but no bid available
2. RestingOrderManager posts a limit sell at estimated mid-price (or entry price)
3. Every check_interval: check if order filled via Kalshi API
4. If not filled after reprice_interval: cancel old order, re-post lower by escalate_cents
5. After max_wait: accept the loss, cancel order (position resolves naturally)
"""

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from src.utils.logging import logger


@dataclass
class RestingOrder:
    """State for a single resting sell order."""

    ticker: str
    side: str  # "yes" or "no"
    quantity: int
    order_id: Optional[str] = None  # Kalshi order ID once placed
    initial_price: int = 0  # First ask price in cents
    current_price: int = 0  # Current ask price (may have been lowered)
    entry_price: int = 0  # Original buy price (for PnL tracking)
    status: str = "pending"  # pending, posted, filled, canceled, expired
    reason: str = ""  # Why we're exiting (edge_eroded, etc.)
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_posted_at: Optional[datetime] = None
    last_repriced_at: Optional[datetime] = None
    reprice_count: int = 0  # How many times we've lowered the price
    filled_at: Optional[datetime] = None


class RestingOrderManager:
    """
    Manages long-lived ask orders for positions that need to exit illiquid markets.

    Usage:
        manager = RestingOrderManager(kalshi_client_factory=get_client)

        # When exit fails due to no bid:
        await manager.post_resting_ask(
            ticker="KXDALSNOWM-26FEB-2.0",
            side="yes",
            quantity=5,
            initial_price=45,  # cents
            entry_price=50,    # what we paid
            reason="edge_eroded",
        )

        # Every cycle in main loop:
        filled_count = await manager.check_and_reprice()
    """

    # How long to back off (minutes) after an insufficient_balance error
    BALANCE_BACKOFF_MINUTES = 30

    def __init__(
        self,
        kalshi_client_factory: Optional[Callable] = None,
        escalate_cents: Optional[int] = None,
        reprice_interval_min: Optional[int] = None,
        max_wait_min: Optional[int] = None,
        paper_trading: bool = False,
    ):
        """
        Args:
            kalshi_client_factory: Async context manager that yields a KalshiClient.
                                   e.g., the _KC() function from main.py
            escalate_cents: How much to lower ask price each reprice (default: 2)
            reprice_interval_min: Minutes between repricings (default: 10)
            max_wait_min: Cancel order after this many minutes (default: 30)
            paper_trading: If True, simulate order placement
        """
        self._client_factory = kalshi_client_factory
        self.paper_trading = paper_trading

        self.escalate_cents = escalate_cents or int(
            os.environ.get("RESTING_ORDER_ESCALATE_CENTS", "2")
        )
        self.reprice_interval_min = reprice_interval_min or int(
            os.environ.get("RESTING_ORDER_REPRICE_MIN", "10")
        )
        self.max_wait_min = max_wait_min or int(
            os.environ.get("RESTING_ORDER_MAX_WAIT_MIN", "30")
        )

        # ticker -> RestingOrder
        self._resting_orders: Dict[str, RestingOrder] = {}

        # Profit target orders: ticker -> RestingOrder
        # Distinct from exit-escalation orders — these are placed at model EV on entry
        self._profit_targets: Dict[str, RestingOrder] = {}

        # Profit target update interval
        self.profit_target_update_min = int(
            os.environ.get("RESTING_PROFIT_TARGET_UPDATE_MIN", "10")
        )
        self._last_profit_target_update: Optional[datetime] = None

        # Balance backoff: ticker -> datetime when we can retry after insufficient_balance
        self._balance_backoff: Dict[str, datetime] = {}

        logger.info(
            f"RestingOrderManager initialized: "
            f"escalate={self.escalate_cents}c, "
            f"reprice_every={self.reprice_interval_min}min, "
            f"max_wait={self.max_wait_min}min, "
            f"profit_target_update={self.profit_target_update_min}min, "
            f"paper={self.paper_trading}"
        )

    @property
    def active_count(self) -> int:
        """Number of currently active resting orders."""
        return sum(
            1
            for o in self._resting_orders.values()
            if o.status in ("pending", "posted")
        )

    def has_resting_order(self, ticker: str) -> bool:
        """Check if we already have a resting order for this ticker."""
        order = self._resting_orders.get(ticker)
        return order is not None and order.status in ("pending", "posted")

    async def post_resting_ask(
        self,
        ticker: str,
        side: str,
        quantity: int,
        initial_price: int,
        entry_price: int = 0,
        reason: str = "edge_eroded",
    ) -> bool:
        """
        Post a limit sell order that sits on the book.

        Args:
            ticker: Market ticker
            side: "yes" or "no"
            quantity: Number of contracts to sell
            initial_price: Ask price in cents (should be reasonable — mid-price or entry price)
            entry_price: What we originally paid (for PnL tracking)
            reason: Why we're exiting

        Returns:
            True if order was posted (or already exists), False on failure
        """
        # Don't duplicate
        if self.has_resting_order(ticker):
            existing = self._resting_orders[ticker]
            logger.debug(
                f"[RESTING] Already have order for {ticker} at {existing.current_price}c "
                f"(posted {existing.last_posted_at})"
            )
            return True

        # Check balance backoff — don't retry if we recently got insufficient_balance
        backoff_until = self._balance_backoff.get(ticker)
        if backoff_until and datetime.now(timezone.utc) < backoff_until:
            remaining = (backoff_until - datetime.now(timezone.utc)).total_seconds() / 60
            logger.debug(
                f"[RESTING] {ticker} on balance backoff for {remaining:.0f}min more"
            )
            return False

        # Ensure price is at least 1 cent
        initial_price = max(1, initial_price)

        order = RestingOrder(
            ticker=ticker,
            side=side,
            quantity=quantity,
            initial_price=initial_price,
            current_price=initial_price,
            entry_price=entry_price,
            reason=reason,
        )

        if self.paper_trading:
            # Simulate order placement
            order.order_id = f"RESTING-PAPER-{ticker}"
            order.status = "posted"
            order.last_posted_at = datetime.now(timezone.utc)
            self._resting_orders[ticker] = order
            logger.info(
                f"[RESTING] PAPER posted ask: {ticker} {side} "
                f"{quantity}x @ {initial_price}c (reason: {reason})"
            )
            return True

        # Live: place actual order via Kalshi API
        try:
            if self._client_factory is None:
                logger.error("[RESTING] No Kalshi client factory configured")
                return False

            async with self._client_factory() as client:
                result = await client.place_order(
                    ticker=ticker,
                    side=side,
                    action="sell",
                    quantity=quantity,
                    order_type="limit",
                    price=initial_price,
                )

            order.order_id = result.order_id
            order.status = "posted"
            order.last_posted_at = datetime.now(timezone.utc)
            self._resting_orders[ticker] = order

            logger.info(
                f"[RESTING] Posted ask: {ticker} {side} "
                f"{quantity}x @ {initial_price}c "
                f"(order_id={result.order_id}, reason={reason})"
            )
            return True

        except Exception as e:
            err_str = str(e).lower()
            if "409" in err_str and "market_closed" in err_str:
                logger.info(
                    f"[RESTING] Market closed for {ticker} — "
                    f"no resting order needed (will settle automatically)"
                )
                return True  # Don't retry
            if "insufficient_balance" in err_str or "insufficient" in err_str:
                backoff_until = datetime.now(timezone.utc) + timedelta(
                    minutes=self.BALANCE_BACKOFF_MINUTES
                )
                self._balance_backoff[ticker] = backoff_until
                logger.warning(
                    f"[RESTING] Insufficient balance for {ticker} — "
                    f"backing off {self.BALANCE_BACKOFF_MINUTES}min "
                    f"(retry at {backoff_until.strftime('%H:%M')} UTC)"
                )
                return False
            logger.error(
                f"[RESTING] Failed to post ask for {ticker}: {e}"
            )
            return False

    async def check_and_reprice(self) -> Dict[str, int]:
        """
        Check all resting orders and reprice if needed.

        Returns:
            {"filled": N, "repriced": N, "expired": N, "active": N}
        """
        if not self._resting_orders:
            return {"filled": 0, "repriced": 0, "expired": 0, "active": 0}

        now = datetime.now(timezone.utc)
        filled = 0
        repriced = 0
        expired = 0
        to_remove: List[str] = []

        # For live trading, fetch current order statuses
        live_order_statuses: Dict[str, str] = {}
        if not self.paper_trading and self._client_factory:
            try:
                async with self._client_factory() as client:
                    orders = await client.get_orders(status="resting")
                    for o in orders:
                        live_order_statuses[o.order_id] = o.status

                    # Also check filled orders
                    filled_orders = await client.get_orders(status="executed")
                    for o in filled_orders:
                        live_order_statuses[o.order_id] = "executed"
            except Exception as e:
                logger.warning(f"[RESTING] Failed to fetch order statuses: {e}")

        for ticker, order in list(self._resting_orders.items()):
            if order.status not in ("pending", "posted"):
                to_remove.append(ticker)
                continue

            age_minutes = (now - order.created_at).total_seconds() / 60.0

            # 1. Check if filled (live mode)
            if not self.paper_trading and order.order_id:
                status = live_order_statuses.get(order.order_id)
                if status == "executed":
                    order.status = "filled"
                    order.filled_at = now
                    pnl = (order.current_price - order.entry_price) * order.quantity / 100.0
                    logger.info(
                        f"[RESTING] FILLED: {ticker} {order.side} "
                        f"{order.quantity}x @ {order.current_price}c "
                        f"(entry={order.entry_price}c, PnL=${pnl:+.2f}, "
                        f"waited {age_minutes:.0f}min)"
                    )
                    filled += 1
                    to_remove.append(ticker)
                    continue

                # Check if order was canceled externally
                if status == "cancelled":
                    order.status = "canceled"
                    logger.info(
                        f"[RESTING] Externally canceled: {ticker} "
                        f"(order_id={order.order_id})"
                    )
                    to_remove.append(ticker)
                    continue

            # 2. Check expiry (give up after max_wait_min)
            if age_minutes >= self.max_wait_min:
                logger.info(
                    f"[RESTING] EXPIRED: {ticker} after {age_minutes:.0f}min "
                    f"(max={self.max_wait_min}min), canceling order"
                )
                await self._cancel_order(order)
                order.status = "expired"
                expired += 1
                to_remove.append(ticker)
                continue

            # 3. Skip reprice if on balance backoff
            backoff_until = self._balance_backoff.get(ticker)
            if backoff_until and now < backoff_until:
                continue  # Don't attempt API calls until backoff expires
            elif backoff_until and now >= backoff_until:
                self._balance_backoff.pop(ticker, None)  # Backoff expired

            # 4. Check if we should reprice (escalate down)
            last_reprice = order.last_repriced_at or order.last_posted_at or order.created_at
            minutes_since_reprice = (now - last_reprice).total_seconds() / 60.0

            if minutes_since_reprice >= self.reprice_interval_min:
                new_price = max(1, order.current_price - self.escalate_cents)

                if new_price < order.current_price:
                    logger.info(
                        f"[RESTING] Repricing {ticker}: "
                        f"{order.current_price}c -> {new_price}c "
                        f"(attempt #{order.reprice_count + 1}, "
                        f"age={age_minutes:.0f}min)"
                    )

                    # Cancel old order and post new one at lower price
                    await self._cancel_order(order)
                    success = await self._repost_order(order, new_price)

                    if success:
                        order.current_price = new_price
                        order.last_repriced_at = now
                        order.reprice_count += 1
                        repriced += 1
                    else:
                        # Repost failed — mark as expired
                        order.status = "expired"
                        expired += 1
                        to_remove.append(ticker)

        # Clean up completed orders
        for ticker in to_remove:
            if ticker in self._resting_orders:
                completed = self._resting_orders[ticker]
                if completed.status in ("filled", "canceled", "expired"):
                    del self._resting_orders[ticker]

        active = self.active_count

        if filled or repriced or expired:
            logger.info(
                f"[RESTING] Cycle summary: "
                f"filled={filled}, repriced={repriced}, expired={expired}, "
                f"active={active}"
            )

        return {
            "filled": filled,
            "repriced": repriced,
            "expired": expired,
            "active": active,
        }

    async def _cancel_order(self, order: RestingOrder) -> bool:
        """Cancel a resting order on Kalshi."""
        if self.paper_trading:
            logger.debug(f"[RESTING] PAPER cancel: {order.ticker}")
            return True

        if not order.order_id or not self._client_factory:
            return False

        try:
            async with self._client_factory() as client:
                await client.cancel_order(order.order_id)
            logger.debug(
                f"[RESTING] Canceled order {order.order_id} for {order.ticker}"
            )
            return True
        except Exception as e:
            err_str = str(e).lower()
            if "409" in err_str and "market_closed" in err_str:
                logger.info(
                    f"[RESTING] Market closed for {order.ticker} — "
                    f"cancel not needed (order will settle automatically)"
                )
                return True  # Treat as successful — market settlement handles it
            logger.warning(
                f"[RESTING] Failed to cancel order {order.order_id}: {e}"
            )
            return False

    async def _repost_order(self, order: RestingOrder, new_price: int) -> bool:
        """Cancel old order and post new one at a different price."""
        if self.paper_trading:
            order.order_id = f"RESTING-PAPER-{order.ticker}-{order.reprice_count + 1}"
            order.last_posted_at = datetime.now(timezone.utc)
            order.status = "posted"
            return True

        if not self._client_factory:
            return False

        try:
            async with self._client_factory() as client:
                result = await client.place_order(
                    ticker=order.ticker,
                    side=order.side,
                    action="sell",
                    quantity=order.quantity,
                    order_type="limit",
                    price=new_price,
                )
            order.order_id = result.order_id
            order.last_posted_at = datetime.now(timezone.utc)
            order.status = "posted"
            return True
        except Exception as e:
            err_str = str(e).lower()
            if "409" in err_str and "market_closed" in err_str:
                logger.info(
                    f"[RESTING] Market closed for {order.ticker} — "
                    f"removing resting order (will settle automatically)"
                )
                order.status = "expired"
                return False  # Caller marks as expired and removes
            if "insufficient_balance" in err_str or "insufficient" in err_str:
                backoff_until = datetime.now(timezone.utc) + timedelta(
                    minutes=self.BALANCE_BACKOFF_MINUTES
                )
                self._balance_backoff[order.ticker] = backoff_until
                logger.warning(
                    f"[RESTING] Insufficient balance to repost {order.ticker} — "
                    f"backing off {self.BALANCE_BACKOFF_MINUTES}min"
                )
                # Keep order as-is (don't mark expired) — will retry after backoff
                return False
            logger.error(
                f"[RESTING] Failed to repost order for {order.ticker} at {new_price}c: {e}"
            )
            return False

    def get_summary(self) -> Dict[str, Any]:
        """Get summary of all resting orders."""
        orders = []
        for ticker, order in self._resting_orders.items():
            age = (datetime.now(timezone.utc) - order.created_at).total_seconds() / 60.0
            orders.append({
                "ticker": ticker,
                "side": order.side,
                "qty": order.quantity,
                "price": order.current_price,
                "initial_price": order.initial_price,
                "entry_price": order.entry_price,
                "status": order.status,
                "age_min": round(age, 1),
                "reprice_count": order.reprice_count,
                "reason": order.reason,
            })
        return {
            "active_count": self.active_count,
            "total_tracked": len(self._resting_orders),
            "orders": orders,
        }

    async def cancel_all(self) -> int:
        """Cancel all resting orders (e.g., at shutdown)."""
        canceled = 0
        for ticker, order in list(self._resting_orders.items()):
            if order.status in ("pending", "posted"):
                success = await self._cancel_order(order)
                if success:
                    order.status = "canceled"
                    canceled += 1
        self._resting_orders.clear()

        # Also cancel profit targets
        for ticker, order in list(self._profit_targets.items()):
            if order.status in ("pending", "posted"):
                success = await self._cancel_order(order)
                if success:
                    order.status = "canceled"
                    canceled += 1
        self._profit_targets.clear()

        logger.info(f"[RESTING] Canceled all {canceled} resting orders (incl profit targets)")
        return canceled

    # ─── Profit target orders (placed at model EV on trade entry) ─────────

    async def place_profit_target(
        self,
        ticker: str,
        side: str,
        quantity: int,
        target_price: int,
        entry_price: int = 0,
    ) -> bool:
        """
        Place a resting limit sell at model fair value (minus fee buffer).

        Called immediately after a weather trade is executed. The target_price
        should be the model's estimated probability * 100 - 2 (fee buffer).

        Unlike exit-escalation orders, profit targets don't reprice down —
        they update based on new forecasts via update_profit_targets().

        Args:
            ticker: Market ticker
            side: "yes" or "no"
            quantity: Contracts to sell
            target_price: Ask price in cents (model EV minus fee buffer)
            entry_price: What we paid (for PnL tracking)

        Returns:
            True if order placed successfully
        """
        # Don't duplicate
        if ticker in self._profit_targets:
            existing = self._profit_targets[ticker]
            if existing.status in ("pending", "posted"):
                logger.debug(
                    f"[PROFIT-TARGET] Already have target for {ticker} at "
                    f"{existing.current_price}c"
                )
                return True

        # Target must be above entry price to be profitable after fees
        if target_price <= entry_price:
            logger.debug(
                f"[PROFIT-TARGET] Skip {ticker}: target {target_price}c <= "
                f"entry {entry_price}c — no profit possible"
            )
            return False

        # Clamp to valid range
        target_price = max(1, min(99, target_price))

        order = RestingOrder(
            ticker=ticker,
            side=side,
            quantity=quantity,
            initial_price=target_price,
            current_price=target_price,
            entry_price=entry_price,
            reason="profit_target",
        )

        if self.paper_trading:
            order.order_id = f"TARGET-PAPER-{ticker}"
            order.status = "posted"
            order.last_posted_at = datetime.now(timezone.utc)
            self._profit_targets[ticker] = order
            logger.info(
                f"[PROFIT-TARGET] PAPER placed: {ticker} {side} "
                f"{quantity}x @ {target_price}c (entry={entry_price}c, "
                f"profit={target_price - entry_price}c/contract)"
            )
            return True

        # Live: place actual limit sell
        try:
            if self._client_factory is None:
                logger.error("[PROFIT-TARGET] No Kalshi client factory")
                return False

            async with self._client_factory() as client:
                result = await client.place_order(
                    ticker=ticker,
                    side=side,
                    action="sell",
                    quantity=quantity,
                    order_type="limit",
                    price=target_price,
                )

            order.order_id = result.order_id
            order.status = "posted"
            order.last_posted_at = datetime.now(timezone.utc)
            self._profit_targets[ticker] = order

            logger.info(
                f"[PROFIT-TARGET] Placed: {ticker} {side} "
                f"{quantity}x @ {target_price}c "
                f"(order_id={result.order_id}, entry={entry_price}c)"
            )
            return True

        except Exception as e:
            err_str = str(e).lower()
            if "409" in err_str and "market_closed" in err_str:
                logger.info(
                    f"[PROFIT-TARGET] Market closed for {ticker} — "
                    f"no profit target needed (will settle automatically)"
                )
                return True
            logger.error(f"[PROFIT-TARGET] Failed for {ticker}: {e}")
            return False

    async def update_profit_targets(
        self,
        get_model_price_fn: Optional[Callable] = None,
    ) -> Dict[str, int]:
        """
        Update all profit target prices based on latest model estimates.

        Runs every profit_target_update_min minutes. Re-fetches weather forecast,
        recalculates model probability, and moves the limit sell to the new EV.

        Args:
            get_model_price_fn: Optional callable(ticker, side) -> int (price cents)
                that returns the current model fair-value price for a ticker.
                If None, uses the weather strategy directly.

        Returns:
            {"updated": N, "filled": N, "canceled": N, "active": N}
        """
        now = datetime.now(timezone.utc)

        # Rate limit updates
        if self._last_profit_target_update:
            elapsed = (now - self._last_profit_target_update).total_seconds() / 60
            if elapsed < self.profit_target_update_min:
                return {"updated": 0, "filled": 0, "canceled": 0,
                        "active": self._profit_target_active_count}

        self._last_profit_target_update = now

        if not self._profit_targets:
            return {"updated": 0, "filled": 0, "canceled": 0, "active": 0}

        updated = 0
        filled = 0
        canceled = 0
        to_remove: List[str] = []

        for ticker, order in list(self._profit_targets.items()):
            if order.status not in ("pending", "posted"):
                to_remove.append(ticker)
                continue

            # Get updated model price
            new_target = None
            if get_model_price_fn:
                try:
                    new_target = get_model_price_fn(ticker, order.side)
                except Exception as e:
                    logger.debug(f"[PROFIT-TARGET] Model price failed for {ticker}: {e}")

            if new_target is None:
                # Try using weather strategy directly
                new_target = self._get_weather_model_price(ticker, order.side)

            if new_target is None:
                continue

            # Apply 2c fee buffer
            new_target = max(1, new_target - 2)

            # Don't update if target hasn't changed meaningfully (>= 2c)
            if abs(new_target - order.current_price) < 2:
                continue

            # Must still be above entry price
            if new_target <= order.entry_price:
                logger.info(
                    f"[PROFIT-TARGET] Canceling {ticker}: model {new_target}c "
                    f"<= entry {order.entry_price}c — no longer profitable"
                )
                await self._cancel_order(order)
                order.status = "canceled"
                canceled += 1
                to_remove.append(ticker)
                continue

            old_price = order.current_price
            logger.info(
                f"[PROFIT-TARGET] Updated {ticker}: {old_price}c -> {new_target}c "
                f"(forecast shifted)"
            )

            # Cancel and repost at new price
            await self._cancel_order(order)
            success = await self._repost_order(order, new_target)
            if success:
                order.current_price = new_target
                updated += 1
            else:
                order.status = "canceled"
                canceled += 1
                to_remove.append(ticker)

        # Clean up
        for ticker in to_remove:
            if ticker in self._profit_targets:
                del self._profit_targets[ticker]

        active = self._profit_target_active_count

        if updated or filled or canceled:
            logger.info(
                f"[PROFIT-TARGET] Update cycle: updated={updated}, "
                f"filled={filled}, canceled={canceled}, active={active}"
            )

        return {"updated": updated, "filled": filled, "canceled": canceled, "active": active}

    @property
    def _profit_target_active_count(self) -> int:
        """Count active profit target orders."""
        return sum(
            1 for o in self._profit_targets.values()
            if o.status in ("pending", "posted")
        )

    def _get_weather_model_price(self, ticker: str, side: str) -> Optional[int]:
        """Get model fair-value price for a weather market ticker."""
        try:
            from src.strategy.weather_strategy import get_weather_strategy
            strategy = get_weather_strategy()
            parsed = strategy.parse_ticker_with_direction_fix(ticker)
            if not parsed:
                return None

            forecast = strategy.nws_client.get_forecast(parsed["city"], parsed["date"])
            if not forecast:
                return None

            market_type = parsed["market_type"]
            if market_type == "temperature":
                prob, _ = strategy._calculate_probability(
                    forecast, parsed["threshold"], parsed["type"],
                    is_bracket=parsed.get("is_bracket", False),
                    temp_series=parsed.get("temp_series"),
                )
            elif market_type == "snow":
                prob, _ = strategy._calculate_snow_probability(
                    forecast, parsed["threshold"], parsed.get("is_monthly", False)
                )
            elif market_type == "rain":
                prob, _ = strategy._calculate_rain_probability(
                    forecast, parsed["threshold"], parsed.get("is_monthly", False)
                )
            else:
                return None

            # Convert probability to price in cents
            if side == "yes":
                return int(prob * 100)
            else:
                return int((1.0 - prob) * 100)
        except Exception as e:
            logger.debug(f"[PROFIT-TARGET] Weather model price failed for {ticker}: {e}")
            return None

    def get_profit_target_summary(self) -> Dict[str, Any]:
        """Get summary of all profit target orders."""
        orders = []
        for ticker, order in self._profit_targets.items():
            age = (datetime.now(timezone.utc) - order.created_at).total_seconds() / 60.0
            orders.append({
                "ticker": ticker,
                "side": order.side,
                "qty": order.quantity,
                "target_price": order.current_price,
                "entry_price": order.entry_price,
                "potential_profit_cents": order.current_price - order.entry_price,
                "status": order.status,
                "age_min": round(age, 1),
            })
        return {
            "active_count": self._profit_target_active_count,
            "total_tracked": len(self._profit_targets),
            "orders": orders,
        }
