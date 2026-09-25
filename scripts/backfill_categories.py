"""
Backfill category for markets with category='unknown'.

Uses ticker prefix inference to assign categories to the 359K+ markets
that were imported from the public API without category data.

Run: python3 scripts/backfill_categories.py [--dry-run] [--batch-size N]
"""
import argparse
import sys
import time

sys.path.insert(0, ".")

from src.data.database import get_db_session
from src.data.models import MarketDB
from src.utils.category_inference import get_category


def backfill_categories(dry_run: bool = False, batch_size: int = 5000) -> None:
    print("=" * 60)
    print("CATEGORY BACKFILL")
    print("=" * 60)

    start = time.time()
    total_updated = 0
    total_unknown = 0
    category_counts: dict = {}

    with next(get_db_session()) as session:
        # Count unknown markets
        unknown_count = (
            session.query(MarketDB)
            .filter(MarketDB.category.in_(["unknown", None, ""]))
            .count()
        )
        total_count = session.query(MarketDB).count()

        print(f"\nTotal markets: {total_count:,}")
        print(f"Unknown category: {unknown_count:,}")
        print(f"Dry run: {dry_run}")
        print()

        # Process in batches
        offset = 0
        while True:
            markets = (
                session.query(MarketDB)
                .filter(MarketDB.category.in_(["unknown", None, ""]))
                .limit(batch_size)
                .offset(offset)
                .all()
            )

            if not markets:
                break

            batch_updated = 0
            for market in markets:
                inferred = get_category(
                    ticker=market.ticker or "",
                    title=market.title or "",
                    existing_category=market.category or "",
                )

                if inferred != "unknown":
                    category_counts[inferred] = category_counts.get(inferred, 0) + 1
                    if not dry_run:
                        market.category = inferred
                    batch_updated += 1
                else:
                    total_unknown += 1

            total_updated += batch_updated

            if not dry_run:
                session.commit()

            offset += batch_size
            elapsed = time.time() - start
            print(
                f"  Batch {offset // batch_size}: "
                f"{total_updated:,} categorized, "
                f"{total_unknown:,} still unknown "
                f"({elapsed:.1f}s)"
            )
            sys.stdout.flush()

    elapsed = time.time() - start

    print(f"\n{'=' * 60}")
    print("BACKFILL COMPLETE")
    print(f"{'=' * 60}")
    print(f"  Markets updated: {total_updated:,}")
    print(f"  Still unknown: {total_unknown:,}")
    print(f"  Time: {elapsed:.1f}s")
    print(f"\n  Category breakdown:")
    for cat, count in sorted(category_counts.items(), key=lambda x: -x[1]):
        print(f"    {cat}: {count:,}")

    if dry_run:
        print("\n  (DRY RUN — no changes written to database)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill market categories from ticker prefixes")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing to DB")
    parser.add_argument("--batch-size", type=int, default=5000, help="Batch size for processing")
    args = parser.parse_args()
    backfill_categories(dry_run=args.dry_run, batch_size=args.batch_size)
