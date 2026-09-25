"""
Discover weather markets on Kalshi and store them in the database.

Run this to bootstrap weather market data:
    python3 scripts/discover_weather_markets.py

Uses the Kalshi public API series_ticker param to efficiently find weather markets.
"""
import sys
import time

sys.path.insert(0, ".")

import requests
import sqlite3
from datetime import datetime, timezone

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

# Exact series tickers that contain weather markets
WEATHER_SERIES = [
    "KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA", "KXHIGHAUS", "KXHIGHLA",
    "KXHIGHDEN", "KXHIGHATL", "KXHIGHPHIL", "KXHIGHSEA",
    "KXLOWTNYC", "KXLOWTMIA", "KXLOWTCHI",
    "KXLOWNY", "KXLOWCHI", "KXLOWMIA", "KXLOWAUS", "KXLOWLA",
    "KXLOWDEN", "KXLOWATL", "KXLOWPHIL", "KXLOWSEA",
    "KXHMONTHRANGE",
    "KXRAINNY", "KXSNOWNY",
    "KXTEMP",
    "SNOWNY", "SNOWCHI",
]


def upsert_market(cursor, m, status_override=None, result_override=None):
    """Insert or update a market in the database."""
    now = datetime.now(timezone.utc).isoformat()
    cursor.execute("""
        INSERT INTO markets (ticker, title, category, status, result, close_time, volume, open_interest, created_at, updated_at)
        VALUES (?, ?, 'Climate and Weather', ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(ticker) DO UPDATE SET
            category='Climate and Weather',
            status=COALESCE(excluded.status, markets.status),
            result=COALESCE(excluded.result, markets.result),
            close_time=COALESCE(excluded.close_time, markets.close_time),
            volume=excluded.volume,
            open_interest=excluded.open_interest,
            updated_at=excluded.updated_at
    """, (
        m.get("ticker"),
        m.get("title"),
        status_override or m.get("status", "active"),
        result_override or m.get("result"),
        m.get("close_time"),
        m.get("volume", 0),
        m.get("open_interest", 0),
        now, now,
    ))


def discover_weather_markets():
    """Scan Kalshi API for weather markets using series_ticker param."""
    session = requests.Session()
    session.headers.update({"Accept": "application/json"})

    conn = sqlite3.connect("data/kalshi_trading.db")
    cursor = conn.cursor()

    total_active = 0
    total_settled = 0

    print("=" * 60)
    print("WEATHER MARKET DISCOVERY")
    print("=" * 60)

    # Phase 1: Active/open markets
    print("\n--- Active Markets ---")
    for series in WEATHER_SERIES:
        api_cursor = None
        series_count = 0

        while True:
            params = {"series_ticker": series, "limit": 200}
            if api_cursor:
                params["cursor"] = api_cursor

            try:
                resp = session.get(f"{BASE_URL}/markets", params=params, timeout=30)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                print(f"  {series}: API error: {e}")
                break

            markets = data.get("markets", [])
            if not markets:
                break

            for m in markets:
                upsert_market(cursor, m)
                series_count += 1

            api_cursor = data.get("cursor")
            if not api_cursor:
                break
            time.sleep(0.2)

        if series_count > 0:
            total_active += series_count
            # Show sample
            sample = markets[0] if markets else {}
            yes = sample.get("yes_ask", "?")
            title = sample.get("title", "")[:45]
            print(f"  {series:20s}: {series_count:4d} markets  (e.g. yes_ask={yes} | {title})")

        time.sleep(0.1)

    conn.commit()
    print(f"\n  Active total: {total_active}")

    # Phase 2: Settled markets (for training data)
    print("\n--- Settled Markets ---")
    for series in WEATHER_SERIES:
        api_cursor = None
        series_count = 0

        while True:
            params = {"series_ticker": series, "status": "settled", "limit": 200}
            if api_cursor:
                params["cursor"] = api_cursor

            try:
                resp = session.get(f"{BASE_URL}/markets", params=params, timeout=30)
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                break

            markets = data.get("markets", [])
            if not markets:
                break

            for m in markets:
                upsert_market(cursor, m, status_override="settled")
                series_count += 1

            api_cursor = data.get("cursor")
            if not api_cursor:
                break
            time.sleep(0.2)

        if series_count > 0:
            total_settled += series_count
            print(f"  {series:20s}: {series_count:4d} settled")

        time.sleep(0.1)

    conn.commit()
    conn.close()

    print(f"\n  Settled total: {total_settled}")
    print(f"\n{'='*60}")
    print(f"TOTAL: {total_active} active + {total_settled} settled = {total_active + total_settled} weather markets")
    print(f"{'='*60}")


if __name__ == "__main__":
    discover_weather_markets()
