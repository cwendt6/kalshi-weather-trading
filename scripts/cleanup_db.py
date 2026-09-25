#!/usr/bin/env python3
"""
Database cleanup script for the Kalshi trading system.

Removes stale data that causes 404 errors and log spam:
- ESPORTS markets (expired/delisted)
- Orphaned price snapshots referencing non-existent markets
- Paper trades older than 7 days

Safe to run multiple times (idempotent).
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.data.database import get_db_session
from src.data.models import MarketDB, PriceDB, TradeDB


def cleanup_esports_markets() -> int:
    """Delete all markets where ticker contains 'ESPORTS'."""
    with next(get_db_session()) as session:
        count = session.query(MarketDB).filter(
            MarketDB.ticker.like("%ESPORTS%")
        ).count()

        if count > 0:
            session.query(MarketDB).filter(
                MarketDB.ticker.like("%ESPORTS%")
            ).delete(synchronize_session=False)
            session.commit()

    return count


def cleanup_orphaned_prices() -> int:
    """Delete price snapshots that reference non-existent markets."""
    with next(get_db_session()) as session:
        # Find price tickers with no matching market
        orphaned = (
            session.query(PriceDB)
            .filter(
                ~PriceDB.ticker.in_(
                    session.query(MarketDB.ticker)
                )
            )
        )
        count = orphaned.count()

        if count > 0:
            orphaned.delete(synchronize_session=False)
            session.commit()

    return count


def cleanup_old_paper_trades(days: int = 7) -> int:
    """Delete paper trades older than N days."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    with next(get_db_session()) as session:
        old_paper = session.query(TradeDB).filter(
            TradeDB.status == "simulated",
            TradeDB.timestamp < cutoff,
        )
        count = old_paper.count()

        if count > 0:
            old_paper.delete(synchronize_session=False)
            session.commit()

    return count


def cleanup_stale_inactive_markets() -> int:
    """Delete markets marked as inactive (404'd) with no associated trades."""
    with next(get_db_session()) as session:
        stale = session.query(MarketDB).filter(
            MarketDB.status == "inactive_404",
        )
        count = stale.count()

        if count > 0:
            # Also clean up their price snapshots
            stale_tickers = [str(m.ticker) for m in stale.all()]
            if stale_tickers:
                session.query(PriceDB).filter(
                    PriceDB.ticker.in_(stale_tickers)
                ).delete(synchronize_session=False)
                session.query(MarketDB).filter(
                    MarketDB.status == "inactive_404",
                ).delete(synchronize_session=False)
                session.commit()

    return count


def main() -> None:
    print("=" * 60)
    print("Kalshi Trading System - Database Cleanup")
    print("=" * 60)
    print()

    # 1. ESPORTS markets
    esports_count = cleanup_esports_markets()
    print(f"  ESPORTS markets deleted:        {esports_count:>6}")

    # 2. Orphaned price snapshots
    orphaned_count = cleanup_orphaned_prices()
    print(f"  Orphaned price snapshots:       {orphaned_count:>6}")

    # 3. Old paper trades (>7 days)
    paper_count = cleanup_old_paper_trades(days=7)
    print(f"  Old paper trades (>7d):         {paper_count:>6}")

    # 4. Stale inactive markets (404'd)
    stale_count = cleanup_stale_inactive_markets()
    print(f"  Stale inactive (404) markets:   {stale_count:>6}")

    total = esports_count + orphaned_count + paper_count + stale_count
    print()
    print(f"  Total records cleaned:          {total:>6}")
    print()

    if total == 0:
        print("  Database is already clean.")
    else:
        print("  Cleanup complete.")

    print("=" * 60)


if __name__ == "__main__":
    main()
