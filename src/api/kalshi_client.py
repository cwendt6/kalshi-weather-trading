"""
Kalshi API client with RSA-PSS authentication and rate limiting.

Uses the official kalshi-python SDK with custom rate limiting.
"""
import asyncio
import base64
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, cast

import httpx
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from config.settings import settings
from src.api.models import Balance, Fill, Market, Order, Orderbook, OrderbookEntry, Position
from src.utils.logging import logger


class RateLimiter:
    """Token bucket rate limiter for API calls."""

    def __init__(self, rate: float, capacity: int) -> None:
        """
        Initialize rate limiter.

        Args:
            rate: Tokens per second to add to bucket.
            capacity: Maximum tokens in bucket.
        """
        self.rate = rate
        self.capacity = capacity
        self.tokens = float(capacity)
        self.last_update = time.monotonic()

    async def acquire(self) -> None:
        """Wait until a token is available, then consume it."""
        while True:
            now = time.monotonic()
            elapsed = now - self.last_update
            self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
            self.last_update = now

            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return

            # Wait for next token
            sleep_time = (1.0 - self.tokens) / self.rate
            await asyncio.sleep(sleep_time)


class SimpleCache:
    """Simple in-memory cache with TTL."""

    def __init__(self, ttl: float = 60.0) -> None:
        """
        Initialize cache.

        Args:
            ttl: Time-to-live in seconds.
        """
        self.ttl = ttl
        self.cache: Dict[str, tuple[Any, float]] = {}

    def get(self, key: str) -> Optional[Any]:
        """Get value from cache if not expired."""
        if key in self.cache:
            value, timestamp = self.cache[key]
            if time.monotonic() - timestamp < self.ttl:
                return value
            else:
                # Expired, remove from cache
                del self.cache[key]
        return None

    def set(self, key: str, value: Any) -> None:
        """Set value in cache with current timestamp."""
        self.cache[key] = (value, time.monotonic())

    def clear(self) -> None:
        """Clear all cache entries."""
        self.cache.clear()


class KalshiClient:
    """
    Kalshi API client with authentication and rate limiting.

    Uses RSA-PSS signature-based authentication as documented in
    docs/KALSHI_API_REFERENCE.md.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        private_key_path: Optional[Path] = None,
        private_key: Optional[str] = None,
        environment: Optional[str] = None,
        read_only: bool = False,
    ) -> None:
        """
        Initialize Kalshi API client.

        Args:
            api_key: Kalshi API key (UUID). Defaults to settings.
            private_key_path: Path to private key PEM file. Defaults to settings.
            private_key: Base64-encoded private key. Defaults to settings.
            environment: "production" or "demo". Defaults to settings.
            read_only: If True, block all write operations (place_order, cancel_order).
        """
        self.api_key = api_key or settings.kalshi_api_key
        self.environment = environment or settings.kalshi_environment
        self.read_only = read_only
        self.base_url = self._get_base_url()

        # Load private key
        if private_key:
            self.private_key = self._load_private_key_from_string(private_key)
        elif private_key_path:
            self.private_key = self._load_private_key_from_file(private_key_path)
        elif settings.kalshi_private_key:
            self.private_key = self._load_private_key_from_string(
                settings.kalshi_private_key
            )
        elif settings.kalshi_private_key_path:
            self.private_key = self._load_private_key_from_file(
                settings.kalshi_private_key_path
            )
        else:
            raise ValueError(
                "Either private_key or private_key_path must be provided"
            )

        # Rate limiters (Basic tier: 20 read/s, 10 write/s)
        self.read_limiter = RateLimiter(rate=20.0, capacity=20)
        self.write_limiter = RateLimiter(rate=10.0, capacity=10)

        # Cache for market data (60 second TTL)
        self.cache = SimpleCache(ttl=60.0)

        # HTTP client
        self.client: Optional[httpx.AsyncClient] = None

        logger.debug(
            f"Kalshi client initialized: environment={self.environment}, "
            f"base_url={self.base_url}, read_only={self.read_only}"
        )

    def ensure_client(self) -> None:
        """Ensure HTTP client is initialized (for non-context-manager usage)."""
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=30.0, trust_env=False)
            logger.debug("Kalshi HTTP client initialized (non-context-manager)")

    def _get_or_create_client(self) -> httpx.AsyncClient:
        """Get or create HTTP client for current event loop.

        This is needed when running from different event loops (e.g., thread pools).
        """
        try:
            loop = asyncio.get_running_loop()
            # Check if existing client is on same loop
            if self.client is not None:
                try:
                    # Test if client is usable
                    return self.client
                except Exception:
                    pass
        except RuntimeError:
            pass

        # Create a fresh client for this context
        return httpx.AsyncClient(timeout=30.0, trust_env=False)

    def _get_base_url(self) -> str:
        """Get base URL for Kalshi API based on environment."""
        if self.environment == "production":
            return "https://api.elections.kalshi.com"
        return "https://demo-api.kalshi.co"

    def _load_private_key_from_file(self, path: Path) -> rsa.RSAPrivateKey:
        """Load RSA private key from PEM file."""
        with open(path, "rb") as key_file:
            private_key = serialization.load_pem_private_key(
                key_file.read(),
                password=None,
                backend=default_backend(),
            )
        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise ValueError("Private key must be RSA key")
        logger.debug(f"Private key loaded from {path}")
        return private_key

    def _load_private_key_from_string(self, key_str: str) -> rsa.RSAPrivateKey:
        """Load RSA private key from base64-encoded string."""
        key_bytes = base64.b64decode(key_str)
        private_key = serialization.load_pem_private_key(
            key_bytes,
            password=None,
            backend=default_backend(),
        )
        if not isinstance(private_key, rsa.RSAPrivateKey):
            raise ValueError("Private key must be RSA key")
        logger.debug("Private key loaded from string")
        return private_key

    def _sign_request(
        self, timestamp_ms: int, method: str, path: str
    ) -> str:
        """
        Sign API request using RSA-PSS.

        Args:
            timestamp_ms: Unix timestamp in milliseconds.
            method: HTTP method (GET, POST, DELETE).
            path: API path without query parameters.

        Returns:
            Base64-encoded signature.
        """
        # Strip query parameters from path
        path_clean = path.split("?")[0]

        # Create message to sign
        message = f"{timestamp_ms}{method}{path_clean}".encode("utf-8")

        # Sign with RSA-PSS
        signature = self.private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )

        # Base64 encode
        return base64.b64encode(signature).decode("utf-8")

    def _get_auth_headers(self, method: str, path: str) -> Dict[str, str]:
        """
        Generate authentication headers for API request.

        Args:
            method: HTTP method.
            path: API path.

        Returns:
            Dictionary of authentication headers.
        """
        timestamp_ms = int(time.time() * 1000)
        signature = self._sign_request(timestamp_ms, method, path)

        return {
            "KALSHI-ACCESS-KEY": self.api_key,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
            "Content-Type": "application/json",
        }

    async def _request(
        self,
        method: str,
        path: str,
        is_write: bool = False,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """
        Make authenticated API request with rate limiting.

        Args:
            method: HTTP method (GET, POST, DELETE).
            path: API path (e.g., "/trade-api/v2/markets").
            is_write: True for write operations (POST, DELETE).
            **kwargs: Additional arguments for httpx request.

        Returns:
            JSON response as dictionary.

        Raises:
            httpx.HTTPError: On HTTP errors.
        """
        # Apply rate limiting
        if is_write:
            await self.write_limiter.acquire()
        else:
            await self.read_limiter.acquire()

        # Build full URL
        url = f"{self.base_url}{path}"

        # Get authentication headers
        headers = self._get_auth_headers(method, path)
        headers.update(kwargs.pop("headers", {}))

        # Determine which client to use - create temporary one if needed
        # This handles cases where we're called from a different event loop
        use_temp_client = self.client is None
        client = self.client if not use_temp_client else httpx.AsyncClient(timeout=30.0, trust_env=False)

        try:
            # Make request
            logger.debug(f"API request: {method} {path}")
            response = await client.request(
                method=method,
                url=url,
                headers=headers,
                **kwargs,
            )
        finally:
            # Close temp client if we created one
            if use_temp_client and client:
                await client.aclose()

        # Handle errors
        if response.status_code >= 400:
            logger.error(
                f"API error: {method} {path} -> {response.status_code} | {response.text}"
            )
            response.raise_for_status()

        # Parse JSON response
        result: Dict[str, Any] = response.json()
        return result

    async def get(self, path: str, **kwargs: Any) -> Dict[str, Any]:
        """Make GET request."""
        return await self._request("GET", path, is_write=False, **kwargs)

    async def post(self, path: str, **kwargs: Any) -> Dict[str, Any]:
        """Make POST request."""
        return await self._request("POST", path, is_write=True, **kwargs)

    async def delete(self, path: str, **kwargs: Any) -> Dict[str, Any]:
        """Make DELETE request."""
        return await self._request("DELETE", path, is_write=True, **kwargs)

    async def __aenter__(self) -> "KalshiClient":
        """Enter async context manager."""
        self.client = httpx.AsyncClient(timeout=30.0, trust_env=False)
        logger.debug("Kalshi client session started")
        return self

    async def __aexit__(self, *args: Any) -> None:
        """Exit async context manager."""
        if self.client:
            await self.client.aclose()
            self.client = None
            logger.debug("Kalshi client session closed")

    # Market Data Methods

    async def get_markets(
        self,
        status: Optional[str] = None,
        category: Optional[str] = None,
        series_ticker: Optional[str] = None,
        limit: int = 100,
        cursor: Optional[str] = None,
    ) -> tuple[List[Market], Optional[str]]:
        """
        Fetch markets matching filters with pagination support.

        Args:
            status: Filter by market status (open, closed, settled).
            category: Filter by category.
            series_ticker: Filter by series ticker (e.g., "KXHIGHNY").
            limit: Maximum number of markets per page.
            cursor: Pagination cursor from previous response.

        Returns:
            Tuple of (List of Market objects, next cursor or None).
        """
        params: Dict[str, Any] = {"limit": limit}
        if status:
            params["status"] = status
        if category:
            params["category"] = category
        if series_ticker:
            params["series_ticker"] = series_ticker
        if cursor:
            params["cursor"] = cursor

        response = await self.get("/trade-api/v2/markets", params=params)
        markets_data = response.get("markets", [])
        next_cursor = response.get("cursor")

        markets = []
        for market_data in markets_data:
            try:
                # Parse datetime fields
                if "close_time" in market_data and market_data["close_time"]:
                    market_data["close_time"] = datetime.fromisoformat(
                        market_data["close_time"].replace("Z", "+00:00")
                    )
                if "strike_date" in market_data and market_data["strike_date"]:
                    market_data["strike_date"] = datetime.fromisoformat(
                        market_data["strike_date"].replace("Z", "+00:00")
                    )

                market = Market(**market_data)
                markets.append(market)
            except Exception as e:
                logger.warning(
                    f"Failed to parse market data: {e}",
                    ticker=market_data.get("ticker"),
                    error=str(e),
                )
                continue

        logger.info(
            f"Fetched {len(markets)} markets",
            count=len(markets),
            status=status,
            category=category,
        )
        return markets, next_cursor

    async def get_all_markets(
        self,
        status: Optional[str] = None,
        category: Optional[str] = None,
        max_markets: int = 1000,
    ) -> List[Market]:
        """
        Fetch all markets matching filters using pagination.

        Args:
            status: Filter by market status (active, closed, settled).
            category: Filter by category.
            max_markets: Maximum total markets to fetch.

        Returns:
            List of all Market objects.
        """
        all_markets: List[Market] = []
        cursor: Optional[str] = None

        while len(all_markets) < max_markets:
            markets, cursor = await self.get_markets(
                status=status,
                category=category,
                limit=100,
                cursor=cursor,
            )

            if not markets:
                break

            all_markets.extend(markets)
            logger.info(f"Total markets fetched: {len(all_markets)}")

            if not cursor:
                break

            # Small delay to avoid rate limiting
            await asyncio.sleep(0.5)

        return all_markets[:max_markets]

    async def get_market(self, ticker: str, use_cache: bool = True) -> Market:
        """
        Fetch single market details.

        Args:
            ticker: Market ticker symbol.
            use_cache: Whether to use cached data if available.

        Returns:
            Market object.

        Raises:
            httpx.HTTPError: If market not found or API error.
        """
        # Check cache
        cache_key = f"market:{ticker}"
        if use_cache:
            cached = self.cache.get(cache_key)
            if cached is not None:
                logger.debug(f"Cache hit for market {ticker}")
                return cast(Market, cached)

        response = await self.get(f"/trade-api/v2/markets/{ticker}")
        market_data = response.get("market", {})

        # Parse datetime fields
        if "close_time" in market_data and market_data["close_time"]:
            market_data["close_time"] = datetime.fromisoformat(
                market_data["close_time"].replace("Z", "+00:00")
            )
        if "strike_date" in market_data and market_data["strike_date"]:
            market_data["strike_date"] = datetime.fromisoformat(
                market_data["strike_date"].replace("Z", "+00:00")
            )

        market = Market(**market_data)

        # Cache the result
        if use_cache:
            self.cache.set(cache_key, market)

        logger.info(
            f"Fetched market {ticker}",
            ticker=ticker,
            status=market.status,
            yes_bid=market.yes_bid,
            yes_ask=market.yes_ask,
        )
        return market

    async def get_orderbook(self, ticker: str) -> Orderbook:
        """
        Fetch current orderbook for a market.

        Args:
            ticker: Market ticker symbol.

        Returns:
            Orderbook object with bids and asks.

        Raises:
            httpx.HTTPError: If market not found or API error.
        """
        response = await self.get(f"/trade-api/v2/markets/{ticker}/orderbook")
        orderbook_data = response.get("orderbook", {})

        # API returns [[price_cents, quantity], ...] lists for each side.
        # "yes" = buy-YES orders (YES bids), "no" = buy-NO orders (NO bids).
        # YES asks are derived: yes_ask = 100 - no_bid_price.
        # NO asks are derived: no_ask = 100 - yes_bid_price.
        yes_raw = orderbook_data.get("yes") or []
        no_raw = orderbook_data.get("no") or []

        yes_bids = [OrderbookEntry(price=p, quantity=q) for p, q in yes_raw if isinstance(p, int)]
        no_bids = [OrderbookEntry(price=p, quantity=q) for p, q in no_raw if isinstance(p, int)]
        yes_asks = [OrderbookEntry(price=100 - e.price, quantity=e.quantity) for e in no_bids]
        no_asks = [OrderbookEntry(price=100 - e.price, quantity=e.quantity) for e in yes_bids]

        orderbook = Orderbook(
            ticker=ticker,
            yes_bids=yes_bids,
            yes_asks=yes_asks,
            no_bids=no_bids,
            no_asks=no_asks,
        )

        logger.info(
            f"Fetched orderbook for {ticker}",
            ticker=ticker,
            yes_bids=len(orderbook.yes_bids),
            yes_asks=len(orderbook.yes_asks),
            no_bids=len(orderbook.no_bids),
            no_asks=len(orderbook.no_asks),
        )
        return orderbook

    async def get_market_history(
        self, ticker: str, start_ts: Optional[int] = None, end_ts: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Fetch historical market data.

        Args:
            ticker: Market ticker symbol.
            start_ts: Start timestamp (Unix milliseconds).
            end_ts: End timestamp (Unix milliseconds).

        Returns:
            List of historical snapshots.

        Raises:
            httpx.HTTPError: If market not found or API error.
        """
        params: Dict[str, Any] = {}
        if start_ts:
            params["start_ts"] = start_ts
        if end_ts:
            params["end_ts"] = end_ts

        response = await self.get(
            f"/trade-api/v2/markets/{ticker}/history", params=params
        )
        history_data: List[Dict[str, Any]] = response.get("history", [])

        logger.info(
            f"Fetched history for {ticker}",
            ticker=ticker,
            snapshots=len(history_data),
        )
        return history_data

    # Portfolio Methods

    async def get_balance(self) -> Balance:
        """
        Fetch account balance.

        Returns:
            Balance object.

        Raises:
            httpx.HTTPError: On API error.
        """
        response = await self.get("/trade-api/v2/portfolio/balance")
        balance_data = response.get("balance", 0)

        # Handle both old format (dict) and new format (int in cents)
        if isinstance(balance_data, dict):
            return Balance(**balance_data)
        else:
            return Balance(
                balance=balance_data,
                portfolio_value=response.get("portfolio_value"),
                payout=response.get("payout"),
            )

    async def get_positions(self) -> List[Position]:
        """
        Fetch ALL current positions with pagination.

        The Kalshi v2 API paginates position results. Without pagination
        only the first page (~12-20 positions) is returned, causing the
        system to be blind to the rest of the portfolio.

        Returns:
            List of Position objects.

        Raises:
            httpx.HTTPError: On API error.
        """
        all_positions: List[Position] = []
        cursor: Optional[str] = None

        while True:
            params: Dict[str, Any] = {"limit": 200}
            if cursor:
                params["cursor"] = cursor

            response = await self.get(
                "/trade-api/v2/portfolio/positions", params=params
            )

            # Kalshi v2 returns positions under "market_positions"
            positions_data = response.get("market_positions", [])
            if not positions_data:
                # Fallback: try legacy key
                positions_data = response.get("positions", [])

            for pos in positions_data:
                try:
                    all_positions.append(Position(**pos))
                except Exception as e:
                    logger.warning(f"Failed to parse position: {e} | raw={pos}")

            # Check for next page
            cursor = response.get("cursor")
            if not cursor or not positions_data:
                break

            await asyncio.sleep(0.2)  # Rate limit courtesy

        logger.info(f"Fetched {len(all_positions)} positions from Kalshi API")
        return all_positions

    async def get_orders(self, status: Optional[str] = None) -> List[Order]:
        """
        Fetch orders.

        Args:
            status: Filter by order status (pending, filled, cancelled).

        Returns:
            List of Order objects.

        Raises:
            httpx.HTTPError: On API error.
        """
        params: Dict[str, Any] = {}
        if status:
            params["status"] = status

        response = await self.get("/trade-api/v2/portfolio/orders", params=params)
        orders_data = response.get("orders", [])

        orders = []
        for order_data in orders_data:
            # Parse datetime fields
            if "created_at" in order_data:
                order_data["created_at"] = datetime.fromisoformat(
                    order_data["created_at"].replace("Z", "+00:00")
                )
            if "updated_at" in order_data and order_data["updated_at"]:
                order_data["updated_at"] = datetime.fromisoformat(
                    order_data["updated_at"].replace("Z", "+00:00")
                )
            orders.append(Order(**order_data))

        return orders

    async def get_fills(
        self, ticker: Optional[str] = None, limit: int = 100
    ) -> List[Fill]:
        """
        Fetch trade fills.

        Args:
            ticker: Filter by market ticker.
            limit: Maximum number of fills to return.

        Returns:
            List of Fill objects.

        Raises:
            httpx.HTTPError: On API error.
        """
        params: Dict[str, Any] = {"limit": limit}
        if ticker:
            params["ticker"] = ticker

        response = await self.get("/trade-api/v2/portfolio/fills", params=params)
        fills_data = response.get("fills", [])

        fills = []
        for fill_data in fills_data:
            # Parse datetime fields
            if "timestamp" in fill_data:
                fill_data["timestamp"] = datetime.fromisoformat(
                    fill_data["timestamp"].replace("Z", "+00:00")
                )
            fills.append(Fill(**fill_data))

        return fills

    # Trading Methods

    def _validate_order(
        self,
        ticker: str,
        side: str,
        action: str,
        quantity: int,
        order_type: str,
        price: Optional[int],
    ) -> None:
        """
        Validate order parameters before submission.

        Args:
            ticker: Market ticker symbol.
            side: Order side (yes/no).
            action: Order action (buy/sell).
            quantity: Number of contracts.
            order_type: Order type (limit/market).
            price: Limit price in cents.

        Raises:
            ValueError: If validation fails.
        """
        # Validate ticker format
        if not ticker or not isinstance(ticker, str):
            raise ValueError("Ticker must be a non-empty string")

        # Validate side
        if side.lower() not in ("yes", "no"):
            raise ValueError(f"Side must be 'yes' or 'no', got: {side}")

        # Validate action
        if action.lower() not in ("buy", "sell"):
            raise ValueError(f"Action must be 'buy' or 'sell', got: {action}")

        # Validate quantity
        if not isinstance(quantity, int) or quantity <= 0:
            raise ValueError(f"Quantity must be a positive integer, got: {quantity}")

        # Validate order type
        if order_type.lower() not in ("limit", "market"):
            raise ValueError(
                f"Order type must be 'limit' or 'market', got: {order_type}"
            )

        # Validate price for limit orders
        if order_type.lower() == "limit":
            if price is None:
                raise ValueError("Price is required for limit orders")
            if not isinstance(price, int) or price < 1 or price > 99:
                raise ValueError(
                    f"Price must be an integer between 1 and 99 cents, got: {price}"
                )

        # Market orders should not have price
        if order_type.lower() == "market" and price is not None:
            raise ValueError("Price should not be specified for market orders")

    async def place_order(
        self,
        ticker: str,
        side: str,
        action: str,
        quantity: int,
        order_type: str = "limit",
        price: Optional[int] = None,
        reduce_only: bool = False,
    ) -> Order:
        """
        Place an order with validation.

        Args:
            ticker: Market ticker symbol.
            side: Order side (yes/no).
            action: Order action (buy/sell).
            quantity: Number of contracts.
            order_type: Order type (limit/market).
            price: Limit price in cents (required for limit orders).
            reduce_only: If True, order can only reduce an existing position
                (prevents accidental short creation on sells).

        Returns:
            Created Order object.

        Raises:
            httpx.HTTPError: On API error.
            ValueError: If validation fails.
            RuntimeError: If client is in read_only mode.
        """
        # Block write operations in read-only mode
        if self.read_only:
            raise RuntimeError(
                "Cannot place order: client is in read-only mode. "
                "Initialize with read_only=False to enable trading."
            )

        # Validate order parameters
        self._validate_order(ticker, side, action, quantity, order_type, price)

        order_data: Dict[str, Any] = {
            "ticker": ticker,
            "side": side.lower(),
            "action": action.lower(),
            "count": quantity,
            "type": order_type.lower(),
        }

        if price is not None:
            if side.lower() == "yes":
                order_data["yes_price"] = price
            else:
                order_data["no_price"] = price

        # reduce_only prevents accidental short creation on sells,
        # but Kalshi only supports it on IoC (market) orders, not limit orders
        if reduce_only and order_type.lower() == "market":
            order_data["reduce_only"] = True

        response = await self.post("/trade-api/v2/portfolio/orders", json=order_data)
        order_response = response.get("order", {})

        # Parse datetime fields (Kalshi v2 uses created_time / last_update_time)
        for dt_field in ("created_time", "last_update_time", "created_at", "updated_at"):
            if dt_field in order_response and order_response[dt_field]:
                try:
                    order_response[dt_field] = datetime.fromisoformat(
                        order_response[dt_field].replace("Z", "+00:00")
                    )
                except (ValueError, AttributeError):
                    pass

        order = Order(**order_response)
        logger.info(
            f"Order placed: {ticker} {action} {quantity} {side} @ {price}",
            order_id=order.order_id,
        )
        return order

    async def cancel_order(self, order_id: str) -> Dict[str, Any]:
        """
        Cancel an order.

        Args:
            order_id: Order ID to cancel.

        Returns:
            Cancellation response.

        Raises:
            httpx.HTTPError: On API error.
            RuntimeError: If client is in read_only mode.
        """
        # Block write operations in read-only mode
        if self.read_only:
            raise RuntimeError(
                "Cannot cancel order: client is in read-only mode. "
                "Initialize with read_only=False to enable trading."
            )

        response = await self.delete(f"/trade-api/v2/portfolio/orders/{order_id}")
        logger.info(f"Order cancelled: {order_id}")
        return response


def get_read_only_client(
    api_key: Optional[str] = None,
    private_key_path: Optional[Path] = None,
    environment: Optional[str] = None,
) -> KalshiClient:
    """
    Create a read-only Kalshi client for data fetching only.

    This client can fetch market data, prices, orderbooks, and account info
    but cannot place or cancel orders.

    Args:
        api_key: Kalshi API key (UUID). Defaults to settings.
        private_key_path: Path to private key PEM file. Defaults to settings.
        environment: "production" or "demo". Defaults to settings.

    Returns:
        KalshiClient configured in read-only mode.

    Example:
        async with get_read_only_client() as client:
            markets, _ = await client.get_markets(status="open")
            for market in markets:
                orderbook = await client.get_orderbook(market.ticker)
    """
    return KalshiClient(
        api_key=api_key,
        private_key_path=private_key_path,
        environment=environment,
        read_only=True,
    )


# Singleton client instance
_kalshi_client: Optional[KalshiClient] = None


def get_kalshi_client(
    api_key: Optional[str] = None,
    private_key_path: Optional[Path] = None,
    environment: Optional[str] = None,
    read_only: bool = False,
) -> KalshiClient:
    """
    Get or create a singleton Kalshi client.

    This returns a shared client instance for efficiency.
    Use this for most operations.

    Args:
        api_key: Kalshi API key (UUID). Defaults to settings.
        private_key_path: Path to private key PEM file. Defaults to settings.
        environment: "production" or "demo". Defaults to settings.
        read_only: If True, disable write operations. Default False.

    Returns:
        Shared KalshiClient instance.
    """
    global _kalshi_client

    if _kalshi_client is None:
        _kalshi_client = KalshiClient(
            api_key=api_key,
            private_key_path=private_key_path,
            environment=environment,
            read_only=read_only,
        )

    return _kalshi_client
