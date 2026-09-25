#!/usr/bin/env python3
"""
Kalshi Weather Trading — Automated Performance Report Generator

Parses the trading database + logs and generates an Excel spreadsheet
with daily P&L, city breakdown, trade type analysis, edge accuracy,
and capital velocity metrics.

Usage:
    python scripts/generate_performance_report.py
    python scripts/generate_performance_report.py --days 7
    python scripts/generate_performance_report.py --output my_report.xlsx
"""
import argparse
import os
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, PieChart, Reference
from openpyxl.chart.label import DataLabelList
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

PROJECT_ROOT = Path(__file__).parent.parent
DB_PATH = PROJECT_ROOT / "data" / "kalshi_trading.db"
LOG_PATH = PROJECT_ROOT / "logs" / "trading.log"

# Ticker prefix → city name mapping
TICKER_CITY_MAP = {
    "NYC": "NYC", "NY": "NYC",
    "CHI": "Chicago", "TCHI": "Chicago",
    "MIA": "Miami", "TMIA": "Miami",
    "DEN": "Denver",
    "AUS": "Austin",
    "PHIL": "Philadelphia",
    "DAL": "Dallas",
    "HOU": "Houston",
    "ATL": "Atlanta",
    "LA": "Los Angeles",
    "DET": "Detroit",
    "MIL": "Milwaukee",
}

def city_from_ticker(ticker: str) -> str:
    """Extract city name from a Kalshi weather ticker like KXHIGHAUS-26FEB12-B80.5."""
    t = ticker.upper()
    for prefix in ("KXHIGH", "KXLOWT", "KXRAIN", "KXSNOW"):
        if t.startswith(prefix):
            rest = t[len(prefix):]
            # Strip date/bracket suffix: find first '-' or digit sequence
            city_code = re.split(r"[-\d]", rest)[0]
            if not city_code:
                continue
            # Remove trailing M (monthly marker)
            if city_code.endswith("M"):
                city_code = city_code[:-1]
            for code, name in TICKER_CITY_MAP.items():
                if city_code == code:
                    return name
            return city_code.title()
    return "OTHER"

# Styles
HEADER_FONT = Font(name="Arial", bold=True, color="FFFFFF", size=11)
HEADER_FILL = PatternFill("solid", fgColor="2F5496")
SUBHEADER_FILL = PatternFill("solid", fgColor="D6E4F0")
SUBHEADER_FONT = Font(name="Arial", bold=True, size=10)
DATA_FONT = Font(name="Arial", size=10)
MONEY_FORMAT = '$#,##0.00;($#,##0.00);"-"'
PCT_FORMAT = "0.0%"
INT_FORMAT = "#,##0"
THIN_BORDER = Border(
    left=Side(style="thin"),
    right=Side(style="thin"),
    top=Side(style="thin"),
    bottom=Side(style="thin"),
)
GREEN_FILL = PatternFill("solid", fgColor="C6EFCE")
RED_FILL = PatternFill("solid", fgColor="FFC7CE")


def style_header_row(ws, row, num_cols):
    for col in range(1, num_cols + 1):
        cell = ws.cell(row=row, column=col)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", wrap_text=True)
        cell.border = THIN_BORDER


def style_data_cell(cell, fmt=None):
    cell.font = DATA_FONT
    cell.border = THIN_BORDER
    if fmt:
        cell.number_format = fmt


def auto_width(ws):
    for col in ws.columns:
        max_len = 0
        col_letter = get_column_letter(col[0].column)
        for cell in col:
            try:
                val = str(cell.value or "")
                max_len = max(max_len, len(val))
            except:
                pass
        ws.column_dimensions[col_letter].width = min(max_len + 3, 30)


def parse_log_entries(log_path, days_back=30):
    """Parse weather trade entries from log file to get edge and city data."""
    entries = []
    cutoff = datetime.now() - timedelta(days=days_back)
    pattern = re.compile(
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\.\d+ \| INFO.*"
        r"WEATHER TRADE #(\d+): BUY (YES|NO) @ (\d+)¢ \| "
        r"(\S+) \| (\S+) \| Qty: (\d+) \| Edge: (-?[\d.]+)%"
    )
    if not log_path.exists():
        return entries
    with open(log_path, "r", errors="replace") as f:
        for line in f:
            m = pattern.search(line)
            if m:
                ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                if ts < cutoff:
                    continue
                ticker = m.group(5)
                entries.append({
                    "timestamp": ts,
                    "date": ts.strftime("%Y-%m-%d"),
                    "trade_num": int(m.group(2)),
                    "side": m.group(3).lower(),
                    "price_cents": int(m.group(4)),
                    "ticker": ticker,
                    "trade_type": m.group(6),
                    "quantity": int(m.group(7)),
                    "edge_pct": float(m.group(8)),
                    "city": city_from_ticker(ticker),
                })
    return entries


def get_db_trades(db_path, days_back=30):
    """Get trade data from SQLite database."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cutoff = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, ticker, side, action, quantity, price, fee, status,
               timestamp, resolved, outcome, pnl, pnl_percent, strategy
        FROM trades
        WHERE timestamp >= ? AND quantity > 0 AND price > 0
        ORDER BY timestamp
    """, (cutoff,))
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def get_bankroll_from_log(log_path):
    """Extract bankroll sync entries from log."""
    entries = []
    pattern = re.compile(
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*Bankroll synced.*"
        r"cash=\$([0-9.]+).*positions=\$([0-9.]+)"
    )
    if not log_path.exists():
        return entries
    with open(log_path, "r", errors="replace") as f:
        for line in f:
            m = pattern.search(line)
            if m:
                entries.append({
                    "timestamp": m.group(1),
                    "cash": float(m.group(2)),
                    "positions": float(m.group(3)),
                    "total": float(m.group(2)) + float(m.group(3)),
                })
    return entries


def build_daily_summary(ws, db_trades, log_entries):
    """Sheet 1: Daily P&L summary."""
    ws.title = "Daily Summary"
    headers = [
        "Date", "Total Trades", "Buys", "Sells", "Win Rate",
        "Gross Profit", "Gross Loss", "Net P&L", "Cumulative P&L",
        "Avg Edge (Entry)", "Fees Paid", "Capital Deployed",
    ]
    ws.append(headers)
    style_header_row(ws, 1, len(headers))

    daily = defaultdict(lambda: {
        "buys": 0, "sells": 0, "wins": 0, "losses": 0,
        "gross_profit": 0, "gross_loss": 0, "fees": 0, "deployed": 0,
    })
    for t in db_trades:
        d = t["timestamp"][:10]
        if t["action"] == "buy":
            daily[d]["buys"] += 1
            daily[d]["deployed"] += (t["quantity"] * t["price"]) / 100.0
        else:
            daily[d]["sells"] += 1
        if t["fee"]:
            daily[d]["fees"] += t["fee"] / 100.0
        if t["resolved"] and t["pnl"] is not None:
            pnl = t["pnl"] / 100.0
            if pnl > 0:
                daily[d]["wins"] += 1
                daily[d]["gross_profit"] += pnl
            elif pnl < 0:
                daily[d]["losses"] += 1
                daily[d]["gross_loss"] += pnl

    edges_by_date = defaultdict(list)
    for e in log_entries:
        edges_by_date[e["date"]].append(e["edge_pct"])

    cum_pnl = 0
    row_num = 2
    for d in sorted(daily.keys()):
        dd = daily[d]
        total = dd["buys"] + dd["sells"]
        decided = dd["wins"] + dd["losses"]
        win_rate = dd["wins"] / decided if decided > 0 else 0
        net = dd["gross_profit"] + dd["gross_loss"]
        cum_pnl += net
        avg_edge = (sum(edges_by_date.get(d, [0])) / len(edges_by_date.get(d, [1]))) if edges_by_date.get(d) else 0

        row = [d, total, dd["buys"], dd["sells"], win_rate,
               dd["gross_profit"], dd["gross_loss"], net, cum_pnl,
               avg_edge / 100.0, dd["fees"], dd["deployed"]]
        ws.append(row)
        for col, val in enumerate(row, 1):
            cell = ws.cell(row=row_num, column=col)
            if col == 5:
                style_data_cell(cell, PCT_FORMAT)
            elif col == 10:
                style_data_cell(cell, PCT_FORMAT)
            elif col in (6, 7, 8, 9, 11, 12):
                style_data_cell(cell, MONEY_FORMAT)
            elif col in (2, 3, 4):
                style_data_cell(cell, INT_FORMAT)
            else:
                style_data_cell(cell)
            if col == 8:
                cell.fill = GREEN_FILL if val >= 0 else RED_FILL
        row_num += 1

    auto_width(ws)
    return row_num


def build_city_breakdown(ws, log_entries, db_trades):
    """Sheet 2: Performance by city."""
    ws.title = "By City"
    headers = ["City", "Total Entries", "NO Trades", "YES Trades",
               "NO %", "Avg Edge", "Total Deployed", "Est. P&L"]
    ws.append(headers)
    style_header_row(ws, 1, len(headers))

    city_data = defaultdict(lambda: {
        "entries": 0, "no_trades": 0, "yes_trades": 0,
        "edges": [], "deployed": 0, "pnl": 0,
    })

    for e in log_entries:
        cd = city_data[e["city"]]
        cd["entries"] += 1
        if e["side"] == "no":
            cd["no_trades"] += 1
        else:
            cd["yes_trades"] += 1
        cd["edges"].append(e["edge_pct"])
        cd["deployed"] += (e["price_cents"] * e["quantity"]) / 100.0

    row_num = 2
    for city in sorted(city_data.keys()):
        cd = city_data[city]
        total = cd["entries"]
        no_pct = cd["no_trades"] / total if total > 0 else 0
        avg_edge = sum(cd["edges"]) / len(cd["edges"]) if cd["edges"] else 0
        row = [city, total, cd["no_trades"], cd["yes_trades"],
               no_pct, avg_edge / 100.0, cd["deployed"], cd["pnl"]]
        ws.append(row)
        for col, val in enumerate(row, 1):
            cell = ws.cell(row=row_num, column=col)
            if col in (5, 6):
                style_data_cell(cell, PCT_FORMAT)
            elif col in (7, 8):
                style_data_cell(cell, MONEY_FORMAT)
            elif col in (2, 3, 4):
                style_data_cell(cell, INT_FORMAT)
            else:
                style_data_cell(cell)
        row_num += 1

    auto_width(ws)


def build_trade_type_breakdown(ws, log_entries):
    """Sheet 3: Performance by trade type (no_exclusion, yes_convergence, etc)."""
    ws.title = "By Trade Type"
    headers = ["Trade Type", "Count", "Avg Edge", "Avg Price (¢)",
               "Total Deployed", "NO Side %", "Cities Used"]
    ws.append(headers)
    style_header_row(ws, 1, len(headers))

    type_data = defaultdict(lambda: {
        "count": 0, "edges": [], "prices": [], "deployed": 0,
        "no_count": 0, "cities": set(),
    })

    for e in log_entries:
        td = type_data[e["trade_type"]]
        td["count"] += 1
        td["edges"].append(e["edge_pct"])
        td["prices"].append(e["price_cents"])
        td["deployed"] += (e["price_cents"] * e["quantity"]) / 100.0
        if e["side"] == "no":
            td["no_count"] += 1
        td["cities"].add(e["city"])

    row_num = 2
    for tt in sorted(type_data.keys()):
        td = type_data[tt]
        avg_edge = sum(td["edges"]) / len(td["edges"]) if td["edges"] else 0
        avg_price = sum(td["prices"]) / len(td["prices"]) if td["prices"] else 0
        no_pct = td["no_count"] / td["count"] if td["count"] > 0 else 0
        row = [tt, td["count"], avg_edge / 100.0, avg_price,
               td["deployed"], no_pct, ", ".join(sorted(td["cities"]))]
        ws.append(row)
        for col, val in enumerate(row, 1):
            cell = ws.cell(row=row_num, column=col)
            if col in (3, 6):
                style_data_cell(cell, PCT_FORMAT)
            elif col == 5:
                style_data_cell(cell, MONEY_FORMAT)
            elif col in (2, 4):
                style_data_cell(cell, INT_FORMAT)
            else:
                style_data_cell(cell)
        row_num += 1

    auto_width(ws)


def build_edge_analysis(ws, log_entries, db_trades):
    """Sheet 4: Edge accuracy — predicted edge vs actual outcome."""
    ws.title = "Edge Analysis"
    headers = [
        "Edge Bucket", "Trade Count", "Expected Win Rate",
        "Actual Win Rate", "Accuracy Gap",
        "Avg Entry Price", "Positive Edge %",
    ]
    ws.append(headers)
    style_header_row(ws, 1, len(headers))

    # Build ticker -> total P&L lookup from DB (sum all fills per ticker)
    outcomes = defaultdict(float)
    resolved_tickers = set()
    for t in db_trades:
        if t["resolved"]:
            resolved_tickers.add(t["ticker"])
            if t["pnl"] is not None:
                outcomes[t["ticker"]] += t["pnl"]

    buckets = [
        ("Negative (< 0%)", -999, 0),
        ("0-5%", 0, 5),
        ("5-10%", 5, 10),
        ("10-20%", 10, 20),
        ("20-40%", 20, 40),
        ("40-60%", 40, 60),
        ("60%+", 60, 999),
    ]

    row_num = 2
    for label, lo, hi in buckets:
        in_bucket = [e for e in log_entries if lo <= e["edge_pct"] < hi]
        count = len(in_bucket)
        if count == 0:
            continue

        # Expected win rate: avg model probability
        avg_edge = sum(e["edge_pct"] for e in in_bucket) / count
        positive_pct = sum(1 for e in in_bucket if e["edge_pct"] > 0) / count

        wins = sum(1 for e in in_bucket if outcomes.get(e["ticker"], 0) > 0)
        resolved = sum(1 for e in in_bucket if e["ticker"] in resolved_tickers)
        actual_wr = wins / resolved if resolved > 0 else 0
        expected_wr = min(1.0, 0.5 + avg_edge / 200.0)
        avg_price = sum(e["price_cents"] for e in in_bucket) / count

        row = [label, count, expected_wr, actual_wr,
               actual_wr - expected_wr, avg_price, positive_pct]
        ws.append(row)
        for col, val in enumerate(row, 1):
            cell = ws.cell(row=row_num, column=col)
            if col in (3, 4, 5, 7):
                style_data_cell(cell, PCT_FORMAT)
            elif col == 2:
                style_data_cell(cell, INT_FORMAT)
            elif col == 6:
                style_data_cell(cell, INT_FORMAT)
            else:
                style_data_cell(cell)
        row_num += 1

    auto_width(ws)


def build_trade_log(ws, log_entries):
    """Sheet 5: Full trade log detail."""
    ws.title = "Trade Log"
    headers = ["Timestamp", "Ticker", "City", "Side", "Trade Type",
               "Price (¢)", "Qty", "Edge %", "Cost ($)"]
    ws.append(headers)
    style_header_row(ws, 1, len(headers))

    for i, e in enumerate(log_entries, 2):
        row = [
            e["timestamp"].strftime("%Y-%m-%d %H:%M"),
            e["ticker"], e["city"], e["side"].upper(), e["trade_type"],
            e["price_cents"], e["quantity"], e["edge_pct"],
            (e["price_cents"] * e["quantity"]) / 100.0,
        ]
        ws.append(row)
        for col, val in enumerate(row, 1):
            cell = ws.cell(row=i, column=col)
            if col == 8:
                style_data_cell(cell, "0.0")
                cell.fill = GREEN_FILL if val > 0 else RED_FILL
            elif col == 9:
                style_data_cell(cell, MONEY_FORMAT)
            elif col in (6, 7):
                style_data_cell(cell, INT_FORMAT)
            else:
                style_data_cell(cell)

    auto_width(ws)


def build_key_metrics(ws, db_trades, log_entries, bankroll_entries):
    """Sheet 6: Key metrics dashboard."""
    ws.title = "Key Metrics"

    total_buys = sum(1 for t in db_trades if t["action"] == "buy" and t["quantity"] > 0)
    total_sells = sum(1 for t in db_trades if t["action"] == "sell" and t["quantity"] > 0)
    resolved = [t for t in db_trades if t["resolved"] and t["pnl"] is not None]
    total_pnl = sum(t["pnl"] / 100.0 for t in resolved)
    wins = sum(1 for t in resolved if t["pnl"] > 0)
    losses = sum(1 for t in resolved if t["pnl"] < 0)
    win_rate = wins / len(resolved) if resolved else 0

    weather_buys = [e for e in log_entries if e["side"] in ("yes", "no")]
    no_count = sum(1 for e in log_entries if e["side"] == "no")
    yes_count = sum(1 for e in log_entries if e["side"] == "yes")
    neg_edge_trades = sum(1 for e in log_entries if e["edge_pct"] < 0)

    avg_edge = sum(e["edge_pct"] for e in log_entries) / len(log_entries) if log_entries else 0
    avg_entry_price = sum(e["price_cents"] for e in log_entries) / len(log_entries) if log_entries else 0

    latest_bankroll = bankroll_entries[-1] if bankroll_entries else {"cash": 0, "positions": 0, "total": 0}

    metrics = [
        ("ACCOUNT OVERVIEW", None, None),
        ("Current Cash", latest_bankroll["cash"], MONEY_FORMAT),
        ("Current Positions Value", latest_bankroll["positions"], MONEY_FORMAT),
        ("Total Portfolio", latest_bankroll["total"], MONEY_FORMAT),
        ("", None, None),
        ("TRADE STATISTICS", None, None),
        ("Total Buy Orders", total_buys, INT_FORMAT),
        ("Total Sell Orders", total_sells, INT_FORMAT),
        ("Resolved Trades", len(resolved), INT_FORMAT),
        ("Win Rate", win_rate, PCT_FORMAT),
        ("Wins", wins, INT_FORMAT),
        ("Losses", losses, INT_FORMAT),
        ("", None, None),
        ("P&L", None, None),
        ("Total Realized P&L", total_pnl, MONEY_FORMAT),
        ("Avg P&L per Resolved Trade", total_pnl / len(resolved) if resolved else 0, MONEY_FORMAT),
        ("", None, None),
        ("EDGE ANALYSIS", None, None),
        ("Average Entry Edge", avg_edge / 100.0, PCT_FORMAT),
        ("Negative Edge Trades", neg_edge_trades, INT_FORMAT),
        ("Avg Entry Price", avg_entry_price, "0\"¢\""),
        ("", None, None),
        ("SIDE BIAS", None, None),
        ("NO Trades", no_count, INT_FORMAT),
        ("YES Trades", yes_count, INT_FORMAT),
        ("NO Ratio", no_count / (no_count + yes_count) if (no_count + yes_count) > 0 else 0, PCT_FORMAT),
        ("", None, None),
        ("RISK FLAGS", None, None),
        ("Negative Edge Entries", neg_edge_trades, INT_FORMAT),
        ("Trades Below $0.03", sum(1 for e in log_entries if e["price_cents"] < 3), INT_FORMAT),
    ]

    for i, (label, value, fmt) in enumerate(metrics, 1):
        label_cell = ws.cell(row=i, column=1, value=label)
        if value is None and label:
            label_cell.font = Font(name="Arial", bold=True, size=12, color="2F5496")
        else:
            label_cell.font = DATA_FONT
        label_cell.border = THIN_BORDER

        if value is not None:
            val_cell = ws.cell(row=i, column=2, value=value)
            style_data_cell(val_cell, fmt)
            if "P&L" in label and isinstance(value, (int, float)):
                val_cell.fill = GREEN_FILL if value >= 0 else RED_FILL

    ws.column_dimensions["A"].width = 30
    ws.column_dimensions["B"].width = 18


def main():
    parser = argparse.ArgumentParser(description="Generate Kalshi trading performance report")
    parser.add_argument("--days", type=int, default=30, help="Days of history to include")
    parser.add_argument("--output", type=str, default=None, help="Output file path")
    args = parser.parse_args()

    output_path = args.output or str(
        PROJECT_ROOT / f"data/performance_report_{datetime.now().strftime('%Y%m%d')}.xlsx"
    )

    print(f"Parsing logs ({args.days} days)...")
    log_entries = parse_log_entries(LOG_PATH, days_back=args.days)
    print(f"  Found {len(log_entries)} weather trade entries in logs")

    print("Loading database trades...")
    db_trades = get_db_trades(DB_PATH, days_back=args.days)
    print(f"  Found {len(db_trades)} trades in database")

    print("Loading bankroll history...")
    bankroll = get_bankroll_from_log(LOG_PATH)
    print(f"  Found {len(bankroll)} bankroll snapshots")

    wb = Workbook()

    print("Building Key Metrics sheet...")
    build_key_metrics(wb.active, db_trades, log_entries, bankroll)

    print("Building Daily Summary sheet...")
    ws_daily = wb.create_sheet()
    build_daily_summary(ws_daily, db_trades, log_entries)

    print("Building City Breakdown sheet...")
    ws_city = wb.create_sheet()
    build_city_breakdown(ws_city, log_entries, db_trades)

    print("Building Trade Type sheet...")
    ws_type = wb.create_sheet()
    build_trade_type_breakdown(ws_type, log_entries)

    print("Building Edge Analysis sheet...")
    ws_edge = wb.create_sheet()
    build_edge_analysis(ws_edge, log_entries, db_trades)

    print("Building Trade Log sheet...")
    ws_log = wb.create_sheet()
    build_trade_log(ws_log, log_entries)

    print(f"Saving to {output_path}...")
    wb.save(output_path)
    print(f"Done! Report saved to {output_path}")
    return output_path


if __name__ == "__main__":
    main()
