#!/usr/bin/env python3
"""
Database cleanup script for weather-only trading mode.

Removes non-weather market data to reduce noise and DB size:
- Markets without weather ticker prefixes (KXHIGH, KXLOW, KXTEMP, SNOW, KXRAIN, KXHMONTH)
- Orphaned price snapshots for deleted markets
- Non-weather paper trades

Usage:
    python scripts/cleanup_for_weather.py --dry-run   # Preview without deleting
    python scripts/cleanup_for_weather.py              # Execute cleanup
"""
import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.data.database import get_db_session
from src.data.models import MarketDB, PriceDB, TradeDB

# Weather ticker prefixes — markets starting with these are kept
WEATHER_PREFIXES = (
    "KXHIGH", "KXLOW", "KXTEMP", "SNOW", "KXRAIN",
    "KXHMONTH", "KXLOWTNYC", "KXLOWTMIA", "KXLOWTCHI",
)


def _is_weather_ticker(ticker: str) -> bool:
    """Check if a ticker belongs to a weather market."""
    return any(ticker.startswith(prefix) for prefix in WEATHER_PREFIXES)


def count_non_weather_markets() -> int:
    """Count markets that are NOT weather markets."""
    with next(get_db_session()) as session:
        all_markets = session.query(MarketDB).all()
        return sum(1 for m in all_markets if not _is_weather_ticker(str(m.ticker)))


def delete_non_weather_markets(dry_run: bool = True) -> int:
    """Delete markets that are NOT weather markets."""
    with next(get_db_session()) as session:
        all_markets = session.query(MarketDB).all()
        non_weather = [m for m in all_markets if not _is_weather_ticker(str(m.ticker))]
        count = len(non_weather)

        if not dry_run and count > 0:
            non_weather_tickers = [str(m.ticker) for m in non_weather]
            # Delete in batches to avoid memory issues
            batch_size = 5000
            for i in range(0, len(non_weather_tickers), batch_size):
                batch = non_weather_tickers[i:i + batch_size]
                session.query(MarketDB).filter(
                    MarketDB.ticker.in_(batch)
                ).delete(synchronize_session=False)
            session.commit()

    return count


def count_orphaned_prices() -> int:
    """Count price snapshots that reference non-existent markets."""
    with next(get_db_session()) as session:
        return session.query(PriceDB).filter(
            ~PriceDB.ticker.in_(session.query(MarketDB.ticker))
        ).count()


def delete_orphaned_prices(dry_run: bool = True) -> int:
    """Delete price snapshots that reference non-existent markets."""
    with next(get_db_session()) as session:
        orphaned = session.query(PriceDB).filter(
            ~PriceDB.ticker.in_(session.query(MarketDB.ticker))
        )
        count = orphaned.count()

        if not dry_run and count > 0:
            orphaned.delete(synchronize_session=False)
            session.commit()

    return count


def count_non_weather_trades() -> int:
    """Count paper trades not related to weather markets."""
    with next(get_db_session()) as session:
        all_trades = session.query(TradeDB).filter(
            TradeDB.status == "simulated"
        ).all()
        return sum(
            1 for t in all_trades
            if not _is_weather_ticker(str(t.ticker))
        )


def delete_non_weather_trades(dry_run: bool = True) -> int:
    """Delete paper trades not related to weather markets."""
    with next(get_db_session()) as session:
        all_trades = session.query(TradeDB).filter(
            TradeDB.status == "simulated"
        ).all()
        non_weather = [t for t in all_trades if not _is_weather_ticker(str(t.ticker))]
        count = len(non_weather)

        if not dry_run and count > 0:
            non_weather_ids = [t.id for t in non_weather]
            batch_size = 5000
            for i in range(0, len(non_weather_ids), batch_size):
                batch = non_weather_ids[i:i + batch_size]
                session.query(TradeDB).filter(
                    TradeDB.id.in_(batch)
                ).delete(synchronize_session=False)
            session.commit()

    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="Clean database for weather-only mode")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be deleted without actually deleting",
    )
    args = parser.parse_args()
    dry_run = args.dry_run

    mode_str = "DRY RUN" if dry_run else "EXECUTING"
    print("=" * 60)
    print(f"Weather-Only Database Cleanup ({mode_str})")
    print("=" * 60)
    print()

    # Count weather markets we're keeping
    with next(get_db_session()) as session:
        weather_count = sum(
            1 for m in session.query(MarketDB).all()
            if _is_weather_ticker(str(m.ticker))
        )
    print(f"  Weather markets (keeping):      {weather_count:>8}")
    print()

    # 1. Non-weather markets
    market_count = delete_non_weather_markets(dry_run=dry_run)
    verb = "would delete" if dry_run else "deleted"
    print(f"  Non-weather markets {verb}: {market_count:>8}")

    # 2. Orphaned price snapshots
    price_count = delete_orphaned_prices(dry_run=dry_run)
    print(f"  Orphaned prices {verb}:     {price_count:>8}")

    # 3. Non-weather paper trades
    trade_count = delete_non_weather_trades(dry_run=dry_run)
    print(f"  Non-weather trades {verb}:  {trade_count:>8}")

    total = market_count + price_count + trade_count
    print()
    print(f"  Total records {verb}:       {total:>8}")
    print()

    if dry_run:
        print("  This was a dry run. Run without --dry-run to execute.")
    elif total == 0:
        print("  Database is already clean for weather-only mode.")
    else:
        print("  Cleanup complete.")

    print("=" * 60)


if __name__ == "__main__":
    main()
