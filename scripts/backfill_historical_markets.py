"""
Backfill historical settled markets from Kalshi's public API.

The API endpoint GET /trade-api/v2/markets?status=settled returns all
resolved markets with their outcomes. No authentication required.
Uses cursor-based pagination and streams results to DB to avoid OOM.

Run: python3 scripts/backfill_historical_markets.py [--max-pages N]
"""
import argparse
import asyncio
import sys
import time
from datetime import datetime
from typing import Optional

import httpx

# Ensure project root is on path
sys.path.insert(0, ".")

from src.utils.category_inference import get_category

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"


def store_batch(markets: list) -> tuple:
    """Store a batch of markets into MarketDB. Returns (stored, updated, errors)."""
    from src.data.database import get_db_session
    from src.data.models import MarketDB

    stored = 0
    updated = 0
    errors = 0

    with next(get_db_session()) as session:
        for m in markets:
            try:
                ticker = m.get("ticker", "")
                if not ticker:
                    continue

                existing = session.query(MarketDB).filter(
                    MarketDB.ticker == ticker
                ).first()

                if existing:
                    if m.get("result") and not existing.result:
                        existing.result = m["result"]
                        existing.status = "settled"
                        existing.updated_at = datetime.utcnow()
                        updated += 1
                else:
                    close_time = None
                    if m.get("close_time"):
                        try:
                            close_time = datetime.fromisoformat(
                                m["close_time"].replace("Z", "+00:00")
                            )
                        except (ValueError, TypeError):
                            pass

                    inferred_cat = get_category(
                        ticker=ticker,
                        title=m.get("title", ""),
                        existing_category=m.get("category", "unknown"),
                    )
                    market_db = MarketDB(
                        ticker=ticker,
                        title=m.get("title", ""),
                        category=inferred_cat,
                        status="settled",
                        close_time=close_time,
                        result=m.get("result"),
                        volume=m.get("volume", 0),
                        open_interest=m.get("open_interest", 0),
                        created_at=datetime.utcnow(),
                        updated_at=datetime.utcnow(),
                    )
                    session.add(market_db)
                    stored += 1

            except Exception as e:
                errors += 1
                if errors < 10:
                    print(f"  Error storing {m.get('ticker', '?')}: {e}")
                continue

        session.commit()

    return stored, updated, errors


async def main(max_pages: int = 0) -> None:
    print("=" * 60)
    print("KALSHI HISTORICAL MARKET BACKFILL")
    print("=" * 60)
    print(f"\nUsing public API: {BASE_URL}")
    print("No authentication required.")
    if max_pages:
        print(f"Limit: {max_pages} pages ({max_pages * 1000} markets max)")
    print()

    cursor: Optional[str] = None
    page = 0
    total_fetched = 0
    total_stored = 0
    total_updated = 0
    total_errors = 0
    categories: dict = {}
    yes_count = 0
    no_count = 0
    climate_examples: list = []
    start = time.time()

    async with httpx.AsyncClient(timeout=30.0) as client:
        while True:
            page += 1
            if max_pages and page > max_pages:
                print(f"\n  Reached page limit ({max_pages}). Stopping fetch.")
                break

            params: dict = {
                "status": "settled",
                "limit": 1000,
            }
            if cursor:
                params["cursor"] = cursor

            try:
                response = await client.get(f"{BASE_URL}/markets", params=params)
                response.raise_for_status()
                data = response.json()
            except Exception as e:
                print(f"  Error on page {page}: {e}")
                if page > 1:
                    break
                raise

            markets = data.get("markets", [])
            cursor = data.get("cursor")

            if not markets:
                break

            total_fetched += len(markets)

            # Track stats inline
            for m in markets:
                result = m.get("result")
                if result == "yes":
                    yes_count += 1
                elif result == "no":
                    no_count += 1
                cat = m.get("category", "unknown")
                categories[cat] = categories.get(cat, 0) + 1
                # Track climate/weather examples
                if len(climate_examples) < 20:
                    cat_lower = (cat or "").lower()
                    if "climate" in cat_lower or "weather" in cat_lower:
                        climate_examples.append(m)

            # Store this batch immediately (streaming to DB)
            stored, updated, errors = store_batch(markets)
            total_stored += stored
            total_updated += updated
            total_errors += errors

            if page % 50 == 0:
                elapsed = time.time() - start
                print(
                    f"  Page {page}: {total_fetched} fetched, "
                    f"{total_stored} stored, {total_updated} updated "
                    f"({elapsed:.0f}s)"
                )
                sys.stdout.flush()

            if not cursor:
                break

            # Rate limit: ~2 requests per second
            await asyncio.sleep(0.5)

    elapsed = time.time() - start

    # Summary
    print(f"\n{'=' * 60}")
    print("BACKFILL COMPLETE")
    print(f"{'=' * 60}")
    print(f"  Total pages: {page}")
    print(f"  Total settled markets fetched: {total_fetched}")
    print(f"  New markets stored: {total_stored}")
    print(f"  Markets updated with results: {total_updated}")
    print(f"  Errors: {total_errors}")
    print(f"  Time: {elapsed:.1f}s")
    print(f"\n  Results: {yes_count} yes, {no_count} no")
    print("  Top categories:")
    for cat, count in sorted(categories.items(), key=lambda x: -x[1])[:15]:
        print(f"    {cat}: {count}")

    if climate_examples:
        print(f"\n  Climate/Weather markets found: {categories.get('Climate and Weather', 0)}")
        for m in climate_examples[:10]:
            print(f"    {m.get('ticker')}: {m.get('title', '')[:60]} [{m.get('result')}]")
    else:
        print("\n  No climate/weather markets in settled results")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill historical settled markets")
    parser.add_argument(
        "--max-pages", type=int, default=0,
        help="Max pages to fetch (0 = unlimited, each page = 1000 markets)"
    )
    args = parser.parse_args()
    asyncio.run(main(max_pages=args.max_pages))
