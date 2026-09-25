"""
Market data collection service with scheduled fetching.

Collects market data from Kalshi and stores it in the database for analysis.
Uses orderbook API as fallback when market-level bid/ask data is unavailable.
Includes resolution checking and rotating sample collection for ML training data.
"""
import asyncio
import random
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

import httpx
from src.api.kalshi_client import KalshiClient
from src.api.models import Market
from src.data.database import get_db_session
from src.data.models import MarketDB, PriceDB, PositionDB
from src.data.weather_market_discovery import get_weather_market_discovery
from src.utils.category_inference import get_category


def _sanitize_datetime(value):
    """
    Sanitize datetime values from the Kalshi API.

    Handles ISO 8601 strings with trailing 'Z' that SQLAlchemy can't parse.
    Converts string datetimes to proper Python datetime objects.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        # Remove trailing Z and replace T with space for SQLAlchemy compatibility
        cleaned = value.replace("Z", "+00:00") if value.endswith("Z") else value
        try:
            return datetime.fromisoformat(cleaned)
        except (ValueError, TypeError):
            return None
    return value
from src.utils.logging import logger


class MarketDataCollector:
    """
    Collects and stores market data from Kalshi API.

    Fetches active markets every 5 minutes and orderbook snapshots
    for tracked markets every minute.
    """

    def __init__(
        self,
        markets_interval: int = 300,
        prices_interval: int = 60,
        track_all_active: bool = True,
        tracked_tickers: Optional[Set[str]] = None,
    ) -> None:
        """
        Initialize market data collector.

        Args:
            markets_interval: Seconds between market list updates (default: 300 = 5 min).
            prices_interval: Seconds between price snapshots (default: 60 = 1 min).
            track_all_active: If True, track all active markets. If False, only track tracked_tickers.
            tracked_tickers: Set of specific tickers to track. If None and track_all_active=False, tracks nothing.
        """
        self.markets_interval = markets_interval
        self.prices_interval = prices_interval
        self.track_all_active = track_all_active
        self.tracked_tickers = tracked_tickers or set()
        self.running = False
        self.markets_task: Optional[asyncio.Task[None]] = None
        self.prices_task: Optional[asyncio.Task[None]] = None

        # Track stale tickers that returned 404 — skip for 24 hours
        self._stale_tickers: Dict[str, datetime] = {}
        self._STALE_COOLDOWN_HOURS = 24

        logger.info(
            "Market data collector initialized",
            markets_interval=markets_interval,
            prices_interval=prices_interval,
            track_all_active=track_all_active,
            tracked_count=len(self.tracked_tickers),
        )

    async def _fetch_markets_with_retry(
        self, client: KalshiClient
    ) -> List[Market]:
        """
        Fetch markets with pagination through all available pages.

        Paginates through the Kalshi API (1000 per page) up to a cap of
        ~5000 markets to balance coverage against API cost.  Previous
        implementation only fetched the first 1000 and discarded the cursor.

        Args:
            client: Kalshi API client.

        Returns:
            List of Market objects.

        Raises:
            httpx.HTTPError: If all retries fail.
        """
        all_markets: List[Market] = []
        cursor: Optional[str] = None
        max_pages = 5  # 5 × 1000 = 5000 markets max

        for page in range(max_pages):
            try:
                markets, cursor = await client.get_markets(
                    status="open", limit=1000, cursor=cursor,
                )
            except Exception as e:
                if page == 0:
                    raise  # First page failure is fatal
                logger.warning(f"Market pagination stopped at page {page}: {e}")
                break

            if not markets:
                break

            all_markets.extend(markets)

            if not cursor:
                break  # No more pages

            # Brief pause between pages to respect rate limits
            await asyncio.sleep(0.3)

        logger.info(
            "Market fetch complete",
            total=len(all_markets),
            pages=min(page + 1, max_pages),
        )
        return all_markets

    async def _fetch_weather_markets(self, client: KalshiClient) -> List[Market]:
        """
        Fetch only weather markets using dynamic discovery.

        Uses WeatherMarketDiscovery to query the API for available weather series
        rather than hardcoded lists. This prevents phantom markets and adapts to
        new markets as Kalshi adds them.

        Returns list of Market objects for weather series only.
        """
        discovery = get_weather_market_discovery()

        try:
            # Dynamically discover available weather series from API
            weather_series = await discovery.discover_weather_series(client, use_cache=True)

            if not weather_series:
                logger.warning("No weather series discovered, using fallback list")
                weather_series = discovery.KNOWN_WEATHER_SERIES
        except Exception as e:
            logger.warning(f"Weather series discovery failed, using fallback list: {e}")
            weather_series = discovery.KNOWN_WEATHER_SERIES

        all_markets: List[Market] = []
        series_with_markets = 0

        # Fetch markets for each discovered series
        for series in weather_series:
            try:
                api_cursor: Optional[str] = None
                series_market_count = 0

                while True:
                    # Only fetch OPEN markets (not closed/settled historical ones)
                    markets, next_cursor = await client.get_markets(
                        series_ticker=series, limit=200, cursor=api_cursor,
                        status="open",  # Filter to active markets only
                    )
                    if not markets:
                        break
                    all_markets.extend(markets)
                    series_market_count += len(markets)
                    api_cursor = next_cursor
                    if not api_cursor:
                        break
                    await asyncio.sleep(0.1)

                if series_market_count > 0:
                    series_with_markets += 1
                    logger.debug(f"Series {series}: {series_market_count} markets")

            except Exception as e:
                logger.debug(f"No markets for weather series {series}: {e}")
                continue

            await asyncio.sleep(0.05)  # Rate limit between series

        logger.info(
            f"Weather-only mode: fetched {len(all_markets)} weather markets "
            f"from {series_with_markets}/{len(weather_series)} discovered series"
        )
        return all_markets

    async def _collect_markets(self, client: KalshiClient) -> None:
        """
        Fetch and store active markets.

        In weather-only mode, fetches only weather series (~200-400 markets).
        In normal mode, fetches all active markets (~20K+).

        Args:
            client: Kalshi API client.
        """
        try:
            # Check weather-only mode from settings
            from config.settings import settings
            weather_only = settings.weather_only_mode

            if weather_only:
                logger.debug("Fetching weather markets only")
                markets = await self._fetch_weather_markets(client)
            else:
                logger.debug("Fetching active markets")
                markets = await self._fetch_markets_with_retry(client)

            if not markets:
                logger.warning("No active markets returned from API")
                return

            # Store markets in database
            stored_count = 0
            updated_count = 0
            with next(get_db_session()) as session:
                for market in markets:
                    existing = (
                        session.query(MarketDB).filter_by(ticker=market.ticker).first()
                    )

                    if existing:
                        # Update existing market (enrich category if unknown)
                        existing.title = market.title  # type: ignore[assignment]
                        # Force re-inference if current category is unknown/empty
                        existing_cat = str(existing.category or "")
                        api_cat = market.category or ""
                        if existing_cat == "unknown" or not existing_cat:
                            # Don't pass "unknown" — force inference from ticker/title
                            new_cat = get_category(
                                ticker=market.ticker,
                                title=market.title or "",
                                existing_category="" if api_cat == "unknown" else api_cat,
                            )
                        else:
                            new_cat = get_category(
                                ticker=market.ticker,
                                title=market.title,
                                existing_category=api_cat or existing_cat,
                            )
                        existing.category = new_cat  # type: ignore[assignment]
                        existing.status = market.status  # type: ignore[assignment]
                        existing.close_time = _sanitize_datetime(market.close_time)  # type: ignore[assignment]
                        existing.strike_date = market.strike_date  # type: ignore[assignment]
                        existing.result = market.result or existing.result  # type: ignore[assignment]
                        existing.volume = market.volume  # type: ignore[assignment]
                        existing.open_interest = market.open_interest  # type: ignore[assignment]
                        existing.updated_at = datetime.now(timezone.utc)  # type: ignore[assignment]
                        updated_count += 1
                    else:
                        # Create new market record (infer category if not provided)
                        inferred_cat = get_category(
                            ticker=market.ticker,
                            title=market.title,
                            existing_category=market.category or "",
                        )
                        market_db = MarketDB(
                            ticker=market.ticker,
                            title=market.title,
                            category=inferred_cat,
                            status=market.status,
                            close_time=_sanitize_datetime(market.close_time),
                            strike_date=market.strike_date,
                            result=market.result,
                            volume=market.volume,
                            open_interest=market.open_interest,
                            created_at=datetime.now(timezone.utc),
                            updated_at=datetime.now(timezone.utc),
                        )
                        session.add(market_db)
                        stored_count += 1

                session.commit()

            logger.info(
                "Market collection complete",
                total_markets=len(markets),
                new=stored_count,
                updated=updated_count,
            )

        except Exception as e:
            logger.error(
                "Market collection failed",
                error=str(e),
                error_type=type(e).__name__,
            )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        retry=retry_if_exception_type(httpx.HTTPError),
    )
    async def _fetch_market_with_retry(
        self, client: KalshiClient, ticker: str
    ) -> Market:
        """
        Fetch single market with automatic retry.

        Args:
            client: Kalshi API client.
            ticker: Market ticker to fetch.

        Returns:
            Market object.

        Raises:
            httpx.HTTPError: If all retries fail.
        """
        return await client.get_market(ticker, use_cache=False)

    @staticmethod
    def _is_price_valid(price: Optional[int]) -> bool:
        """Check if a price value is real (not a default/null)."""
        return price is not None and price > 0 and price < 100

    async def _get_best_prices(
        self, client: KalshiClient, ticker: str, market: Market
    ) -> Dict[str, Any]:
        """
        Get best bid/ask prices, using orderbook as fallback.

        First checks market-level data. If any bid/ask field is None, 0, or 100
        (defaults indicating no real data), falls back to the orderbook endpoint
        to extract the best bid/ask from actual order levels.

        Args:
            client: Kalshi API client.
            ticker: Market ticker.
            market: Market object with initial price data.

        Returns:
            Dict with yes_bid, yes_ask, no_bid, no_ask, volume, open_interest.
        """
        yes_bid = market.yes_bid
        yes_ask = market.yes_ask
        no_bid = market.no_bid
        no_ask = market.no_ask

        prices_valid = all(
            self._is_price_valid(p) for p in [yes_bid, yes_ask, no_bid, no_ask]
        )

        if not prices_valid:
            try:
                orderbook = await client.get_orderbook(ticker)

                if orderbook.yes_bids:
                    yes_bid = max(entry.price for entry in orderbook.yes_bids)
                if orderbook.yes_asks:
                    yes_ask = min(entry.price for entry in orderbook.yes_asks)
                if orderbook.no_bids:
                    no_bid = max(entry.price for entry in orderbook.no_bids)
                if orderbook.no_asks:
                    no_ask = min(entry.price for entry in orderbook.no_asks)

                logger.debug(
                    "Used orderbook for prices",
                    ticker=ticker,
                    yes_bid=yes_bid,
                    yes_ask=yes_ask,
                    no_bid=no_bid,
                    no_ask=no_ask,
                )
            except Exception as e:
                logger.warning(
                    "Orderbook fetch failed, using market prices",
                    ticker=ticker,
                    error=str(e),
                )

        return {
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "no_bid": no_bid,
            "no_ask": no_ask,
            "volume": market.volume,
            "open_interest": market.open_interest,
        }

    def _is_stale(self, ticker: str) -> bool:
        """Check if a ticker is on the stale cooldown list."""
        if ticker not in self._stale_tickers:
            return False
        cooldown_expires = self._stale_tickers[ticker] + timedelta(hours=self._STALE_COOLDOWN_HOURS)
        if datetime.now(timezone.utc) >= cooldown_expires:
            del self._stale_tickers[ticker]
            return False
        return True

    def _mark_stale(self, ticker: str) -> None:
        """Mark a ticker as stale (404) and update DB status."""
        self._stale_tickers[ticker] = datetime.now(timezone.utc)
        logger.warning(
            f"Market returned 404, marking stale for {self._STALE_COOLDOWN_HOURS}h",
            ticker=ticker,
        )
        try:
            with next(get_db_session()) as session:
                market = session.query(MarketDB).filter_by(ticker=ticker).first()
                if market:
                    market.status = "inactive_404"  # type: ignore[assignment]
                    market.updated_at = datetime.now(timezone.utc)  # type: ignore[assignment]
                    session.commit()
        except Exception as e:
            logger.debug(f"Failed to mark {ticker} as inactive: {e}")

    async def _fetch_single_price(
        self, client: KalshiClient, ticker: str
    ) -> Optional[tuple]:
        """
        Fetch price for a single market. Returns (prices_dict, used_orderbook) or None if default.

        Used by _collect_prices for concurrent batch processing.
        Handles 404 errors by marking the market as stale.
        """
        # Skip if on cooldown from a previous 404
        if self._is_stale(ticker):
            return None

        try:
            market = await self._fetch_market_with_retry(client, ticker)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                self._mark_stale(ticker)
                return None
            raise

        market_prices_valid = all(
            self._is_price_valid(p)
            for p in [market.yes_bid, market.yes_ask, market.no_bid, market.no_ask]
        )

        used_orderbook = not market_prices_valid

        prices = await self._get_best_prices(client, ticker, market)

        # Skip if still all defaults after orderbook lookup
        if (
            not self._is_price_valid(prices["yes_ask"])
            and not self._is_price_valid(prices["no_ask"])
        ):
            return None

        return (prices, used_orderbook)

    async def _collect_prices(self, client: KalshiClient) -> None:
        """
        Fetch and store current prices for active markets worth tracking.

        Uses tiered filtering to reduce 20K+ markets down to a manageable set.
        Now that we use math-based probability engine (no LLM), we can cover
        more markets since analysis is ~10ms per market (not 35s per LLM call).

        Tier 1 (high priority): volume >= 100 and closes within 48 hours
        Tier 2 (medium priority): volume >= 10 and closes within 7 days
        Tier 3 (broader): any volume > 0 and closes within 14 days
        Explicitly tracked tickers always included.

        Args:
            client: Kalshi API client.
        """
        try:
            # Check weather-only mode
            from config.settings import settings
            weather_only = settings.weather_only_mode

            # Determine which markets to track
            tickers_to_fetch: List[str] = []

            if weather_only:
                # In weather-only mode, fetch prices for ALL weather markets
                # (typically 200-400 — no tiered filtering needed)
                with next(get_db_session()) as session:
                    from sqlalchemy import or_
                    weather_markets = session.query(MarketDB.ticker).filter(
                        MarketDB.status == "active",
                        or_(
                            MarketDB.ticker.like("KXHIGH%"),
                            MarketDB.ticker.like("KXLOW%"),
                            MarketDB.ticker.op("GLOB")("KX*SNOWM*"),
                            MarketDB.ticker.like("SNOW%"),
                            MarketDB.ticker.like("KXRAIN%"),
                            MarketDB.ticker.like("RAIN%"),
                            MarketDB.ticker.like("KXHMONTH%"),
                            MarketDB.category.ilike("%climate%"),
                            MarketDB.category.ilike("%weather%"),
                        ),
                    ).all()
                    tickers_to_fetch = [str(m.ticker) for m in weather_markets]
                    # Filter out false positives
                    tickers_to_fetch = [
                        t for t in tickers_to_fetch
                        if not any(t.startswith(p) for p in (
                            "KXMODEL", "KXPGA", "KXTRUMP",
                        ))
                    ]

                    # Also include open position tickers
                    ticker_set = set(tickers_to_fetch)
                    position_tickers = [
                        str(p.ticker)
                        for p in session.query(PositionDB.ticker)
                        .filter(PositionDB.quantity > 0)
                        .all()
                        if str(p.ticker) not in ticker_set
                    ]
                    if position_tickers:
                        tickers_to_fetch.extend(position_tickers)

                logger.info(
                    f"Weather-only price collection: {len(tickers_to_fetch)} tickers"
                )

            elif self.track_all_active:
                with next(get_db_session()) as session:
                    now = datetime.now(timezone.utc)
                    cutoff_48h = now + timedelta(hours=48)
                    cutoff_7d = now + timedelta(days=7)
                    cutoff_14d = now + timedelta(days=14)

                    # Tier 1: Liquid + closing soon (< 48 hours)
                    tier1_markets = (
                        session.query(MarketDB)
                        .filter(
                            MarketDB.status == "active",
                            MarketDB.close_time.isnot(None),
                            MarketDB.close_time <= cutoff_48h,
                            MarketDB.close_time > now,
                            (
                                (MarketDB.volume.isnot(None)) & (MarketDB.volume >= 100)
                            ) | (
                                (MarketDB.open_interest.isnot(None)) & (MarketDB.open_interest >= 50)
                            ),
                        )
                        .all()
                    )

                    # Tier 2: Some liquidity + closing within 7 days
                    tier1_tickers = {str(m.ticker) for m in tier1_markets}
                    tier2_markets = (
                        session.query(MarketDB)
                        .filter(
                            MarketDB.status == "active",
                            MarketDB.close_time.isnot(None),
                            MarketDB.close_time <= cutoff_7d,
                            MarketDB.close_time > now,
                            MarketDB.volume.isnot(None),
                            MarketDB.volume >= 10,
                            ~MarketDB.ticker.in_(tier1_tickers) if tier1_tickers else True,
                        )
                        .all()
                    )

                    # Tier 3: Any activity + closing within 14 days (broad coverage)
                    # Prioritize by: soonest close_time first, then highest volume
                    # to catch the most actionable markets within the API budget
                    tier12_tickers = tier1_tickers | {str(m.ticker) for m in tier2_markets}
                    tier3_markets = (
                        session.query(MarketDB)
                        .filter(
                            MarketDB.status == "active",
                            MarketDB.close_time.isnot(None),
                            MarketDB.close_time <= cutoff_14d,
                            MarketDB.close_time > now,
                            MarketDB.volume.isnot(None),
                            MarketDB.volume > 0,
                            ~MarketDB.ticker.in_(tier12_tickers) if tier12_tickers else True,
                        )
                        .order_by(MarketDB.close_time.asc(), MarketDB.volume.desc())
                        .limit(500)  # Increased from 200; ~30sec at 20 reads/sec
                        .all()
                    )

                    tickers_to_fetch = [str(m.ticker) for m in tier1_markets]
                    tickers_to_fetch.extend(str(m.ticker) for m in tier2_markets)
                    tickers_to_fetch.extend(str(m.ticker) for m in tier3_markets)

                    logger.info(
                        "Price collection tiers",
                        tier1_high_priority=len(tier1_markets),
                        tier2_medium_priority=len(tier2_markets),
                        tier3_broad_coverage=len(tier3_markets),
                        total=len(tickers_to_fetch),
                    )

                    # Also include explicitly tracked tickers
                    ticker_set = set(tickers_to_fetch)
                    tickers_to_fetch.extend(
                        t for t in self.tracked_tickers if t not in ticker_set
                    )

                    # CRITICAL: Always include tickers for open positions
                    # Without fresh prices we can't detect profit/loss for exits
                    ticker_set = set(tickers_to_fetch)
                    position_tickers = [
                        str(p.ticker)
                        for p in session.query(PositionDB.ticker)
                        .filter(PositionDB.quantity > 0)
                        .all()
                        if str(p.ticker) not in ticker_set
                    ]
                    if position_tickers:
                        tickers_to_fetch.extend(position_tickers)
                        logger.info(
                            "Added open position tickers to price collection",
                            position_tickers=len(position_tickers),
                        )
            else:
                tickers_to_fetch = list(self.tracked_tickers)

            # Filter out stale (404'd) tickers before fetching
            pre_filter = len(tickers_to_fetch)
            tickers_to_fetch = [t for t in tickers_to_fetch if not self._is_stale(t)]
            stale_skipped = pre_filter - len(tickers_to_fetch)

            if not tickers_to_fetch:
                logger.info("No liquid markets to track for price collection")
                return

            logger.info(
                f"Collecting prices for {len(tickers_to_fetch)} liquid markets (filtered from 20K+)",
                count=len(tickers_to_fetch),
                stale_skipped=stale_skipped if stale_skipped > 0 else None,
            )

            stored_count = 0
            failed_count = 0
            orderbook_count = 0
            skipped_default = 0
            timestamp = datetime.now(timezone.utc)

            # Process in concurrent batches of 10 to respect rate limits (20 reads/sec)
            batch_size = 10
            for i in range(0, len(tickers_to_fetch), batch_size):
                batch = tickers_to_fetch[i:i + batch_size]
                results = await asyncio.gather(
                    *[self._fetch_single_price(client, ticker) for ticker in batch],
                    return_exceptions=True,
                )

                for ticker, result in zip(batch, results):
                    if isinstance(result, Exception):
                        logger.warning(
                            f"Failed to collect price for {ticker}",
                            ticker=ticker,
                            error=str(result),
                        )
                        failed_count += 1
                        continue

                    if result is None:
                        skipped_default += 1
                        continue

                    prices, used_orderbook = result
                    if used_orderbook:
                        orderbook_count += 1

                    with next(get_db_session()) as db_session:
                        price_db = PriceDB(
                            ticker=ticker,
                            timestamp=timestamp,
                            yes_bid=prices["yes_bid"],
                            yes_ask=prices["yes_ask"],
                            no_bid=prices["no_bid"],
                            no_ask=prices["no_ask"],
                            volume=prices["volume"],
                            open_interest=prices["open_interest"],
                            created_at=datetime.now(timezone.utc),
                        )
                        db_session.add(price_db)
                        db_session.commit()

                    stored_count += 1

                # Brief pause between batches to respect rate limits
                await asyncio.sleep(0.3)

            logger.info(
                "Price collection cycle complete",
                total_tickers=len(tickers_to_fetch),
                valid_prices=stored_count,
                orderbook_fallbacks=orderbook_count,
                skipped_defaults=skipped_default,
                failed=failed_count,
            )

        except Exception as e:
            logger.error(
                "Price collection failed",
                error=str(e),
                error_type=type(e).__name__,
            )

    async def _check_resolutions(self, client: KalshiClient) -> None:
        """Check for newly resolved markets and update outcomes."""
        try:
            with next(get_db_session()) as session:
                # Get markets we think are still active but may have resolved
                active_tickers = [
                    str(m.ticker)
                    for m in session.query(MarketDB).filter(
                        MarketDB.status.in_(["active", "open", "closed"]),
                        MarketDB.result.is_(None),
                    ).limit(200).all()
                ]

            if not active_tickers:
                return

            resolved_count = 0
            for ticker in active_tickers:
                try:
                    market = await client.get_market(ticker, use_cache=False)
                    if market.result:
                        with next(get_db_session()) as session:
                            db_market = session.query(MarketDB).filter(
                                MarketDB.ticker == ticker
                            ).first()
                            if db_market:
                                db_market.result = market.result  # type: ignore[assignment]
                                db_market.status = "settled"  # type: ignore[assignment]
                                db_market.updated_at = datetime.now(timezone.utc)  # type: ignore[assignment]
                                resolved_count += 1
                            session.commit()
                    await asyncio.sleep(0.2)
                except httpx.HTTPStatusError as e:
                    if e.response.status_code == 404:
                        self._mark_stale(ticker)
                    continue
                except Exception:
                    continue

            if resolved_count > 0:
                logger.info(f"Updated {resolved_count} newly resolved markets")

        except Exception as e:
            logger.error(f"Resolution check failed: {e}")

    async def _collect_sample_prices(self, client: KalshiClient) -> None:
        """
        Collect prices for a rotating sample of active markets.

        Builds ML training data over time by sampling markets that aren't
        already being tracked in the main price collection loop.
        """
        try:
            with next(get_db_session()) as session:
                # Markets with known liquidity (had valid prices in last 24h)
                liquid_tickers = {
                    str(row[0])
                    for row in session.query(PriceDB.ticker).filter(
                        PriceDB.yes_ask > 0,
                        PriceDB.yes_ask < 100,
                        PriceDB.timestamp > datetime.now(timezone.utc) - timedelta(hours=24),
                    ).distinct().all()
                }

                # All active tickers
                all_active = [
                    str(row[0])
                    for row in session.query(MarketDB.ticker).filter(
                        MarketDB.status == "active",
                    ).all()
                ]

            # Always re-check liquid markets + a random sample of others
            untracked = [t for t in all_active if t not in liquid_tickers]
            sample_size = min(50, len(untracked))
            sample = random.sample(untracked, sample_size) if untracked else []
            tickers_to_check = list(liquid_tickers) + sample

            if not tickers_to_check:
                return

            stored = 0
            for ticker in tickers_to_check:
                try:
                    market = await self._fetch_market_with_retry(client, ticker)
                    prices = await self._get_best_prices(client, ticker, market)

                    if (
                        self._is_price_valid(prices["yes_ask"])
                        or self._is_price_valid(prices["no_ask"])
                    ):
                        with next(get_db_session()) as session:
                            price_db = PriceDB(
                                ticker=ticker,
                                timestamp=datetime.now(timezone.utc),
                                yes_bid=prices["yes_bid"],
                                yes_ask=prices["yes_ask"],
                                no_bid=prices["no_bid"],
                                no_ask=prices["no_ask"],
                                volume=prices["volume"],
                                open_interest=prices["open_interest"],
                                created_at=datetime.now(timezone.utc),
                            )
                            session.add(price_db)
                            session.commit()
                            stored += 1

                    await asyncio.sleep(0.1)
                except Exception:
                    continue

            if stored > 0:
                logger.info(
                    f"Sample price collection: {stored} prices from "
                    f"{len(liquid_tickers)} liquid + {sample_size} sampled markets"
                )

        except Exception as e:
            logger.error(f"Sample price collection failed: {e}")


    async def _discover_weather_markets(self, client: KalshiClient) -> int:
        """
        Discover weather markets using dynamic discovery.

        Uses WeatherMarketDiscovery to find available weather series dynamically
        rather than relying on hardcoded lists. Updates database with newly
        discovered markets.

        Returns count of newly discovered markets.
        """
        discovery = get_weather_market_discovery()
        discovered = 0

        try:
            # Dynamically discover available weather series (short cache for discovery loop)
            try:
                weather_series = await discovery.discover_weather_series(client, use_cache=True)
                if not weather_series:
                    logger.info("No weather series discovered from API")
                    return discovered
            except Exception as e:
                logger.warning(f"Weather series discovery failed: {e}")
                return discovered

            # Fetch all markets from discovered series
            for series in weather_series:
                try:
                    api_cursor = None
                    series_discovered = 0

                    while True:
                        try:
                            # Only fetch OPEN markets
                            markets, next_cursor = await client.get_markets(
                                series_ticker=series,
                                limit=200,
                                cursor=api_cursor,
                                status="open",  # Filter to active markets only
                            )
                        except Exception as e:
                            logger.debug(f"Error fetching markets for {series}: {e}")
                            break

                        if not markets:
                            break

                        with next(get_db_session()) as session:
                            for market in markets:
                                existing = (
                                    session.query(MarketDB)
                                    .filter_by(ticker=market.ticker)
                                    .first()
                                )
                                if not existing:
                                    inferred_cat = get_category(
                                        ticker=market.ticker,
                                        title=market.title,
                                        existing_category=market.category or "",
                                    )
                                    market_db = MarketDB(
                                        ticker=market.ticker,
                                        title=market.title,
                                        category=inferred_cat,
                                        status=market.status,
                                        close_time=_sanitize_datetime(market.close_time),
                                        strike_date=market.strike_date,
                                        result=market.result,
                                        volume=market.volume,
                                        open_interest=market.open_interest,
                                        created_at=datetime.now(timezone.utc),
                                        updated_at=datetime.now(timezone.utc),
                                    )
                                    session.add(market_db)
                                    discovered += 1
                                    series_discovered += 1
                                    self.tracked_tickers.add(market.ticker)

                            session.commit()

                        api_cursor = next_cursor
                        if not api_cursor:
                            break
                        await asyncio.sleep(0.2)

                    if series_discovered > 0:
                        logger.debug(f"Series {series}: discovered {series_discovered} new markets")

                except Exception as e:
                    logger.warning(f"Error discovering markets for weather series {series}: {e}")
                    continue

                await asyncio.sleep(0.1)  # Rate limit between series

            if discovered > 0:
                logger.info(f"Discovered {discovered} new weather markets from {len(weather_series)} series")

        except Exception as e:
            logger.error(f"Weather market discovery failed: {e}")

        return discovered

    async def _markets_collection_loop(self) -> None:
        """Background task that periodically collects market data."""
        from config.settings import settings
        weather_only = settings.weather_only_mode
        logger.info(
            f"Starting markets collection loop"
            f"{' (weather-only mode)' if weather_only else ''}"
        )
        resolution_check_counter = 0
        weather_check_counter = 0

        async with KalshiClient() as client:
            while self.running:
                try:
                    await self._collect_markets(client)

                    # Check resolutions every 12 cycles (~1 hour at 5min interval)
                    resolution_check_counter += 1
                    if resolution_check_counter % 12 == 0:
                        await self._check_resolutions(client)

                    # Discover weather markets dynamically:
                    # - Weather-only mode: every 3 cycles (~15 min) with API caching
                    # - Normal mode: every 12 cycles (~1 hour) with API caching
                    # Discovery uses cached results when fresh to minimize API calls
                    weather_check_counter += 1
                    weather_interval = 3 if weather_only else 12
                    if weather_check_counter % weather_interval == 0:
                        await self._discover_weather_markets(client)

                except Exception as e:
                    logger.error(
                        "Markets collection loop error",
                        error=str(e),
                        error_type=type(e).__name__,
                    )

                # Wait for next collection
                await asyncio.sleep(self.markets_interval)

        logger.info("Markets collection loop stopped")

    async def _prices_collection_loop(self) -> None:
        """Background task that periodically collects price snapshots."""
        logger.info("Starting prices collection loop")
        sample_counter = 0

        async with KalshiClient() as client:
            while self.running:
                try:
                    await self._collect_prices(client)

                    # Collect rotating sample every 10 cycles (~10 min at 1-min interval)
                    sample_counter += 1
                    if sample_counter % 10 == 0:
                        await self._collect_sample_prices(client)

                except Exception as e:
                    logger.error(
                        "Prices collection loop error",
                        error=str(e),
                        error_type=type(e).__name__,
                    )

                # Wait for next collection
                await asyncio.sleep(self.prices_interval)

        logger.info("Prices collection loop stopped")

    async def start(self) -> None:
        """Start the data collection service."""
        if self.running:
            logger.warning("Collector is already running")
            return

        self.running = True
        logger.info("Starting market data collector")

        # Start background tasks
        self.markets_task = asyncio.create_task(self._markets_collection_loop())
        self.prices_task = asyncio.create_task(self._prices_collection_loop())

        logger.info("Market data collector started")

    async def stop(self) -> None:
        """Stop the data collection service."""
        if not self.running:
            logger.warning("Collector is not running")
            return

        logger.info("Stopping market data collector")
        self.running = False

        # Cancel background tasks
        if self.markets_task:
            self.markets_task.cancel()
            try:
                await self.markets_task
            except asyncio.CancelledError:
                pass

        if self.prices_task:
            self.prices_task.cancel()
            try:
                await self.prices_task
            except asyncio.CancelledError:
                pass

        logger.info("Market data collector stopped")

    def add_tracked_ticker(self, ticker: str) -> None:
        """
        Add a ticker to the tracked set.

        Args:
            ticker: Ticker symbol to track.
        """
        self.tracked_tickers.add(ticker)
        logger.info(f"Added {ticker} to tracked markets", ticker=ticker)

    def remove_tracked_ticker(self, ticker: str) -> None:
        """
        Remove a ticker from the tracked set.

        Args:
            ticker: Ticker symbol to remove.
        """
        self.tracked_tickers.discard(ticker)
        logger.info(f"Removed {ticker} from tracked markets", ticker=ticker)

    def get_collection_stats(self) -> dict:
        """
        Get current collection statistics.

        Returns:
            Dictionary with collection stats.
        """
        with next(get_db_session()) as session:
            total_markets = session.query(MarketDB).count()
            active_markets = session.query(MarketDB).filter_by(status="active").count()
            total_prices = session.query(PriceDB).count()

            # Get latest price timestamp
            latest_price = (
                session.query(PriceDB).order_by(PriceDB.timestamp.desc()).first()
            )
            latest_price_time = latest_price.timestamp if latest_price else None

        return {
            "running": self.running,
            "total_markets": total_markets,
            "active_markets": active_markets,
            "total_price_snapshots": total_prices,
            "latest_price_snapshot": latest_price_time.isoformat()
            if latest_price_time
            else None,
            "tracked_tickers_count": len(self.tracked_tickers),
            "markets_interval": self.markets_interval,
            "prices_interval": self.prices_interval,
        }
