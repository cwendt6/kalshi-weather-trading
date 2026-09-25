#!/usr/bin/env python3
"""
Backfill historical resolved markets from Kalshi.

Fetches settled markets and their price history, storing them in the database
for ML training purposes.

Usage:
    python scripts/backfill_historical.py [--max-markets 500] [--dry-run]
"""
import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from src.api.kalshi_client import KalshiClient
from src.data.database import get_db_session, init_db
from src.data.models import MarketDB, PriceDB
from src.utils.logging import logger


async def fetch_settled_markets(
    client: KalshiClient,
    max_markets: int = 500,
) -> list:
    """
    Fetch all settled markets from Kalshi.

    Args:
        client: KalshiClient instance.
        max_markets: Maximum markets to fetch.

    Returns:
        List of Market objects with results.
    """
    logger.info(f"Fetching up to {max_markets} settled markets...")

    all_markets = await client.get_all_markets(
        status="settled",
        max_markets=max_markets,
    )

    # Filter to only markets with results
    markets_with_results = [m for m in all_markets if m.result is not None]

    logger.info(
        f"Found {len(markets_with_results)} settled markets with results "
        f"(out of {len(all_markets)} total)"
    )

    return markets_with_results


async def fetch_market_history(
    client: KalshiClient,
    ticker: str,
) -> list:
    """
    Fetch price history for a market.

    Args:
        client: KalshiClient instance.
        ticker: Market ticker.

    Returns:
        List of price history dictionaries.
    """
    try:
        history = await client.get_market_history(ticker)
        return history
    except Exception as e:
        logger.warning(f"Failed to fetch history for {ticker}: {e}")
        return []


def store_market(session, market) -> bool:
    """
    Store market in database if not already exists.

    Args:
        session: Database session.
        market: Market object from API.

    Returns:
        True if new market was stored, False if already exists.
    """
    # Check if market already exists
    existing = session.query(MarketDB).filter_by(ticker=market.ticker).first()
    if existing:
        # Update result if not set
        if existing.result is None and market.result:
            existing.result = market.result
            existing.updated_at = datetime.utcnow()
            logger.debug(f"Updated result for {market.ticker}: {market.result}")
            return True
        return False

    # Create new market record
    market_db = MarketDB(
        ticker=market.ticker,
        title=market.title,
        category=market.category,
        status=market.status,
        close_time=market.close_time,
        strike_date=market.strike_date,
        result=market.result,
        volume=market.volume,
        open_interest=market.open_interest,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    session.add(market_db)

    logger.debug(f"Stored market {market.ticker}: {market.result}")
    return True


def store_price_history(session, ticker: str, history: list) -> int:
    """
    Store price history in database.

    Args:
        session: Database session.
        ticker: Market ticker.
        history: List of price history dictionaries.

    Returns:
        Number of new price records stored.
    """
    stored = 0

    for snapshot in history:
        try:
            # Parse timestamp
            ts = snapshot.get("ts") or snapshot.get("timestamp")
            if isinstance(ts, (int, float)):
                # Unix timestamp in seconds or milliseconds
                if ts > 1e11:  # Milliseconds
                    timestamp = datetime.utcfromtimestamp(ts / 1000)
                else:
                    timestamp = datetime.utcfromtimestamp(ts)
            elif isinstance(ts, str):
                timestamp = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            else:
                continue

            # Check if price already exists
            existing = (
                session.query(PriceDB)
                .filter_by(ticker=ticker, timestamp=timestamp)
                .first()
            )
            if existing:
                continue

            # Create price record
            price_db = PriceDB(
                ticker=ticker,
                timestamp=timestamp,
                yes_bid=snapshot.get("yes_bid"),
                yes_ask=snapshot.get("yes_ask"),
                no_bid=snapshot.get("no_bid"),
                no_ask=snapshot.get("no_ask"),
                volume=snapshot.get("volume"),
                open_interest=snapshot.get("open_interest"),
                created_at=datetime.utcnow(),
            )
            session.add(price_db)
            stored += 1

        except Exception as e:
            logger.warning(f"Failed to parse price snapshot: {e}")
            continue

    return stored


async def backfill_historical_data(
    max_markets: int = 500,
    dry_run: bool = False,
) -> dict:
    """
    Main backfill function.

    Args:
        max_markets: Maximum markets to backfill.
        dry_run: If True, don't actually store data.

    Returns:
        Statistics dictionary.
    """
    stats = {
        "markets_fetched": 0,
        "markets_stored": 0,
        "markets_skipped": 0,
        "prices_stored": 0,
        "errors": 0,
    }

    # Initialize database
    if not dry_run:
        init_db()

    async with KalshiClient(read_only=True) as client:
        # Fetch settled markets
        markets = await fetch_settled_markets(client, max_markets)
        stats["markets_fetched"] = len(markets)

        if dry_run:
            logger.info(f"[DRY RUN] Would process {len(markets)} markets")
            for m in markets[:10]:
                logger.info(f"  - {m.ticker}: {m.title[:50]}... ({m.result})")
            return stats

        # Process each market
        with next(get_db_session()) as session:
            for i, market in enumerate(markets):
                try:
                    # Store market
                    is_new = store_market(session, market)
                    if is_new:
                        stats["markets_stored"] += 1
                    else:
                        stats["markets_skipped"] += 1

                    # Fetch and store price history
                    history = await fetch_market_history(client, market.ticker)
                    if history:
                        prices_stored = store_price_history(session, market.ticker, history)
                        stats["prices_stored"] += prices_stored

                    # Commit periodically
                    if (i + 1) % 10 == 0:
                        session.commit()
                        logger.info(
                            f"Progress: {i + 1}/{len(markets)} markets processed"
                        )

                except Exception as e:
                    logger.error(f"Error processing {market.ticker}: {e}")
                    stats["errors"] += 1
                    continue

            # Final commit
            session.commit()

    return stats


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Backfill historical resolved markets from Kalshi"
    )
    parser.add_argument(
        "--max-markets",
        type=int,
        default=500,
        help="Maximum number of markets to fetch (default: 500)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch data but don't store in database",
    )
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info("Starting historical data backfill")
    logger.info(f"Max markets: {args.max_markets}")
    logger.info(f"Dry run: {args.dry_run}")
    logger.info("=" * 60)

    try:
        stats = asyncio.run(
            backfill_historical_data(
                max_markets=args.max_markets,
                dry_run=args.dry_run,
            )
        )

        logger.info("=" * 60)
        logger.info("Backfill complete!")
        logger.info(f"Markets fetched: {stats['markets_fetched']}")
        logger.info(f"Markets stored: {stats['markets_stored']}")
        logger.info(f"Markets skipped (already exist): {stats['markets_skipped']}")
        logger.info(f"Price records stored: {stats['prices_stored']}")
        logger.info(f"Errors: {stats['errors']}")
        logger.info("=" * 60)

    except KeyboardInterrupt:
        logger.info("Backfill interrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Backfill failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
