"""
Daily report generator for the Kalshi trading system.

Generates markdown daily summaries with trade activity, P&L, and strategy metrics.
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func

from src.data.database import db_manager
from src.data.models import TradeDB, PortfolioSnapshotDB
from src.utils.logging import logger

REPORTS_DIR = Path("logs/reports")


def generate_daily_report(report_date: Optional[date] = None) -> str:
    """
    Generate a markdown daily report for the given date.

    Args:
        report_date: Date to report on. Defaults to today.

    Returns:
        Markdown report content.
    """
    if report_date is None:
        report_date = date.today()

    day_start = datetime.combine(report_date, datetime.min.time())
    day_end = datetime.combine(report_date, datetime.max.time())

    # Extract all data within the session
    day_trade_rows: List[Dict] = []
    day_pnl = 0.0
    day_wins = 0
    day_losses = 0
    total_trades = 0
    total_resolved = 0
    total_pnl = 0.0
    total_wins = 0
    strategy_stats: List[Tuple] = []

    with db_manager.get_session() as session:
        # Trades placed today - extract as dicts
        day_trades = (
            session.query(TradeDB)
            .filter(TradeDB.timestamp >= day_start, TradeDB.timestamp <= day_end)
            .all()
        )
        for t in day_trades:
            day_trade_rows.append({
                "ticker": t.ticker,
                "side": t.side,
                "quantity": t.quantity,
                "price": t.price,
                "strategy": t.strategy or "unknown",
            })

        # Trades resolved today
        resolved_today = (
            session.query(TradeDB)
            .filter(TradeDB.resolved == 1, TradeDB.resolved_at >= day_start, TradeDB.resolved_at <= day_end)
            .all()
        )
        day_pnl = sum(float(t.pnl) for t in resolved_today if t.pnl) if resolved_today else 0.0
        day_wins = sum(1 for t in resolved_today if t.outcome == "win")
        day_losses = sum(1 for t in resolved_today if t.outcome == "loss")

        # All-time stats
        total_trades = session.query(func.count(TradeDB.id)).scalar() or 0
        total_resolved = (
            session.query(func.count(TradeDB.id))
            .filter(TradeDB.resolved == 1)
            .scalar() or 0
        )
        pnl_result = (
            session.query(func.sum(TradeDB.pnl))
            .filter(TradeDB.resolved == 1)
            .scalar()
        )
        total_pnl = float(pnl_result) if pnl_result else 0.0

        total_wins = (
            session.query(func.count(TradeDB.id))
            .filter(TradeDB.outcome == "win")
            .scalar() or 0
        )

        # Strategy breakdown
        strategy_stats = (
            session.query(
                TradeDB.strategy,
                func.count(TradeDB.id),
                func.sum(TradeDB.pnl),
            )
            .filter(TradeDB.resolved == 1)
            .group_by(TradeDB.strategy)
            .all()
        )

    # Build report outside session
    lines = []
    lines.append(f"# Daily Trading Report - {report_date.strftime('%Y-%m-%d')}")
    lines.append("")
    lines.append("## Today's Activity")
    lines.append(f"- **New Trades Placed:** {len(day_trade_rows)}")
    lines.append(f"- **Trades Resolved:** {day_wins + day_losses}")
    lines.append(f"- **Day Wins/Losses:** {day_wins}/{day_losses}")
    lines.append(f"- **Day P&L:** ${day_pnl:+,.2f}")
    lines.append("")

    if day_trade_rows:
        lines.append("### Trades Placed")
        lines.append("| Ticker | Side | Qty | Price | Strategy |")
        lines.append("|--------|------|-----|-------|----------|")
        for t in day_trade_rows[:20]:
            lines.append(
                f"| {t['ticker']} | {t['side'].upper()} | {t['quantity']} | {t['price']}c | {t['strategy']} |"
            )
        if len(day_trade_rows) > 20:
            lines.append(f"| ... and {len(day_trade_rows) - 20} more | | | | |")
        lines.append("")

    lines.append("## Cumulative Performance")
    lines.append(f"- **Total Trades:** {total_trades}")
    lines.append(f"- **Resolved:** {total_resolved}")
    if total_resolved > 0:
        lines.append(f"- **Win Rate:** {total_wins / total_resolved:.1%}")
    else:
        lines.append("- **Win Rate:** N/A")
    lines.append(f"- **Total P&L:** ${total_pnl:+,.2f}")
    lines.append("")

    if strategy_stats:
        lines.append("## Strategy Performance")
        lines.append("| Strategy | Resolved | P&L |")
        lines.append("|----------|----------|-----|")
        for strat, count, pnl in strategy_stats:
            pnl_val = float(pnl) if pnl else 0.0
            lines.append(f"| {strat or 'unknown'} | {count} | ${pnl_val:+,.2f} |")
        lines.append("")

    lines.append("---")
    lines.append(f"*Generated at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}*")

    return "\n".join(lines)


def save_daily_report(report_date: Optional[date] = None) -> Path:
    """
    Generate and save a daily report to logs/reports/.

    Args:
        report_date: Date to report on. Defaults to today.

    Returns:
        Path to the saved report file.
    """
    if report_date is None:
        report_date = date.today()

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    content = generate_daily_report(report_date)
    filename = f"daily_{report_date.strftime('%Y-%m-%d')}.md"
    filepath = REPORTS_DIR / filename

    filepath.write_text(content)
    logger.info(f"Daily report saved to {filepath}")
    return filepath
