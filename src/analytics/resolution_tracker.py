"""
Resolution tracker for trade P&L calculation.

Checks market resolutions and calculates realized P&L for all trades.
Works both offline (using markets table) and online (via Kalshi API).

P&L Logic (Kalshi binary options, prices in cents 0-100):
- BUY YES at price P, market resolves YES: profit = (100 - P) * qty
- BUY YES at price P, market resolves NO:  loss  = -P * qty
- BUY NO  at price P, market resolves NO:  profit = (100 - P) * qty
- BUY NO  at price P, market resolves YES: loss  = -P * qty
- Fees deducted from winning trades (2% of payout)
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func, text

from src.data.database import db_manager
from src.data.models import MarketDB, TradeDB
from src.utils.datetime_utils import ensure_utc
from src.utils.logging import logger


@dataclass
class TradeResolution:
    """Resolution result for a single trade."""

    trade_id: int
    ticker: str
    side: str
    action: str
    quantity: int
    price: int  # cents
    fee: float
    strategy: str
    market_result: Optional[str]  # 'yes', 'no', or None
    outcome: Optional[str]  # 'win', 'loss', or None
    pnl: Optional[float]  # dollars
    pnl_percent: Optional[float]
    resolved: bool


@dataclass
class PerformanceSummary:
    """Aggregated performance metrics."""

    total_trades: int = 0
    resolved_trades: int = 0
    unresolved_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    total_cost: float = 0.0
    total_fees: float = 0.0
    roi_percent: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    profit_factor: float = 0.0
    by_strategy: Dict[str, "StrategySummary"] = field(default_factory=dict)


@dataclass
class StrategySummary:
    """Performance summary for a single strategy."""

    strategy: str
    total_trades: int = 0
    resolved_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    total_pnl: float = 0.0
    total_cost: float = 0.0
    avg_pnl: float = 0.0


def _calculate_trade_pnl(
    side: str,
    action: str,
    price: int,
    quantity: int,
    fee: float,
    market_result: str,
) -> Tuple[str, float, float]:
    """
    Calculate P&L for a single trade.

    Kalshi charges a 2% winner fee on $1.00 payout = $0.02/contract.
    The `fee` parameter (from TradeDB.fee) is IGNORED because it was
    an estimate stored at execution time. We calculate the definitive
    fee here based on the actual 2% rate.

    Args:
        side: 'yes' or 'no'
        action: 'buy' or 'sell'
        price: Entry price in cents (0-100)
        quantity: Number of contracts
        fee: Stored fee (ignored — we calculate from KALSHI_WINNER_FEE_RATE)
        market_result: 'yes' or 'no'

    Returns:
        Tuple of (outcome, pnl_dollars, pnl_percent)
    """
    from src.utils.fees import KALSHI_WINNER_FEE_RATE

    cost = (price / 100.0) * quantity  # Cost in dollars
    winner_fee = quantity * KALSHI_WINNER_FEE_RATE  # $0.02/contract

    if action == "buy":
        if side == "yes":
            if market_result == "yes":
                # Won: payout is $1 per contract, minus 2% winner fee
                payout = quantity * 1.0
                pnl = payout - cost - winner_fee
                outcome = "win"
            else:
                # Lost: lose entire cost (no fee on losses)
                pnl = -cost
                outcome = "loss"
        else:  # side == "no"
            if market_result == "no":
                # Won: payout is $1 per contract, minus 2% winner fee
                payout = quantity * 1.0
                pnl = payout - cost - winner_fee
                outcome = "win"
            else:
                # Lost: lose entire cost (no fee on losses)
                pnl = -cost
                outcome = "loss"
    else:
        # Sell orders - reverse logic
        # When you sell, you receive (price * qty) upfront
        received = (price / 100.0) * quantity
        if side == "yes":
            if market_result == "yes":
                # Sold YES, resolved YES: you owe $1/contract
                pnl = received - quantity
                outcome = "loss"
            else:
                # Sold YES, resolved NO: you keep the premium, minus winner fee
                pnl = received - winner_fee
                outcome = "win"
        else:
            if market_result == "no":
                pnl = received - quantity
                outcome = "loss"
            else:
                pnl = received - winner_fee
                outcome = "win"

    # Override outcome based on actual net P&L (fees can turn a "win" into a loss)
    if pnl >= 0:
        outcome = "win"
    else:
        outcome = "loss"

    pnl_percent = (pnl / cost * 100) if cost > 0 else 0.0
    return outcome, round(pnl, 2), round(pnl_percent, 2)


def check_resolved_markets(update_from_api: bool = False) -> List[TradeResolution]:
    """
    Check all trades for market resolution and calculate P&L.

    For each unresolved trade, checks if the market has resolved
    (via the markets table or close_time). Updates the trades table
    with resolution data.

    Args:
        update_from_api: If True, attempt to fetch latest market data
                        from Kalshi API before checking (requires auth).

    Returns:
        List of TradeResolution objects for all trades.
    """
    resolutions: List[TradeResolution] = []

    with db_manager.get_session() as session:
        # Optionally update market data from API
        if update_from_api:
            _refresh_market_data(session)

        # Get all trades with their market data
        trades = (
            session.query(TradeDB, MarketDB)
            .outerjoin(MarketDB, TradeDB.ticker == MarketDB.ticker)
            .order_by(TradeDB.timestamp)
            .all()
        )

        now = datetime.now(timezone.utc)
        updated_count = 0

        for trade, market in trades:
            # Determine market result
            market_result = None
            is_resolved = False

            if trade.resolved == 1 and trade.market_result:
                # Already resolved in a prior run
                market_result = trade.market_result
                is_resolved = True
            elif market and market.result and market.result in ("yes", "no"):
                # Market table has a result
                market_result = market.result
                is_resolved = True
            elif market and market.status == "finalized":
                # Market is finalized but result field might be empty
                # This shouldn't happen normally
                is_resolved = False
            elif market and market.close_time and ensure_utc(market.close_time) < now:
                # Market close time has passed - it should be resolved
                # but we don't know the result yet without API data
                # For impossible event BUY NO trades at 98-99c,
                # we can infer with high confidence
                if (
                    trade.side == "no"
                    and trade.action == "buy"
                    and trade.price >= 95
                    and trade.strategy == "impossible_scanner"
                ):
                    # Near-certain NO resolution for impossible events
                    # bought at 95-99c. These are events that "can't happen"
                    # (e.g., NYC temp above 200F). Conservative assumption: resolved NO.
                    market_result = "no"
                    is_resolved = True
                else:
                    is_resolved = False

            # Calculate P&L if resolved
            outcome = None
            pnl = None
            pnl_percent = None

            if is_resolved and market_result:
                fee = float(trade.fee) if trade.fee else 0.0
                outcome, pnl, pnl_percent = _calculate_trade_pnl(
                    side=trade.side,
                    action=trade.action,
                    price=trade.price,
                    quantity=trade.quantity,
                    fee=fee,
                    market_result=market_result,
                )

                # Update trade record if not already done
                if trade.resolved != 1:
                    trade.resolved = 1
                    trade.outcome = outcome
                    trade.pnl = Decimal(str(pnl))
                    trade.pnl_percent = pnl_percent
                    trade.market_result = market_result
                    trade.resolved_at = now
                    updated_count += 1

            resolutions.append(
                TradeResolution(
                    trade_id=trade.id,
                    ticker=trade.ticker,
                    side=trade.side,
                    action=trade.action,
                    quantity=trade.quantity,
                    price=trade.price,
                    fee=float(trade.fee) if trade.fee else 0.0,
                    strategy=trade.strategy or "unknown",
                    market_result=market_result,
                    outcome=outcome,
                    pnl=pnl,
                    pnl_percent=pnl_percent,
                    resolved=is_resolved,
                )
            )

        session.commit()
        logger.info(
            "Resolution check complete",
            total_trades=len(resolutions),
            resolved=sum(1 for r in resolutions if r.resolved),
            updated=updated_count,
        )

    return resolutions


def _refresh_market_data(session) -> int:
    """
    Refresh market status from Kalshi API for traded tickers.

    Returns number of markets updated.
    """
    updated = 0
    try:
        import asyncio
        from src.api.kalshi_client import KalshiClient

        # Get unique unresolved tickers
        unresolved_tickers = (
            session.query(TradeDB.ticker)
            .filter(TradeDB.resolved == 0)
            .distinct()
            .all()
        )
        tickers = [t[0] for t in unresolved_tickers]

        if not tickers:
            return 0

        async def _fetch_markets():
            nonlocal updated
            async with KalshiClient() as client:
                for ticker in tickers:
                    try:
                        api_market = await client.get_market(ticker)
                        db_market = (
                            session.query(MarketDB)
                            .filter_by(ticker=ticker)
                            .first()
                        )
                        if db_market:
                            db_market.status = api_market.status
                            if api_market.result:
                                db_market.result = api_market.result
                            db_market.updated_at = datetime.now(timezone.utc)
                            updated += 1
                    except Exception as e:
                        logger.debug(f"Could not fetch {ticker}: {e}")

            session.commit()

        asyncio.run(_fetch_markets())

    except Exception as e:
        logger.warning(f"API refresh failed (may not have credentials): {e}")

    return updated


def get_performance_summary(
    resolutions: Optional[List[TradeResolution]] = None,
) -> PerformanceSummary:
    """
    Calculate aggregate performance metrics from trade resolutions.

    Args:
        resolutions: Pre-computed resolutions, or None to run check_resolved_markets().

    Returns:
        PerformanceSummary with wins, losses, win_rate, total_pnl, and strategy breakdown.
    """
    if resolutions is None:
        resolutions = check_resolved_markets()

    summary = PerformanceSummary()
    summary.total_trades = len(resolutions)

    resolved = [r for r in resolutions if r.resolved and r.pnl is not None]
    unresolved = [r for r in resolutions if not r.resolved]
    summary.resolved_trades = len(resolved)
    summary.unresolved_trades = len(unresolved)

    if not resolved:
        return summary

    wins = [r for r in resolved if r.outcome == "win"]
    losses = [r for r in resolved if r.outcome == "loss"]
    summary.wins = len(wins)
    summary.losses = len(losses)
    summary.win_rate = len(wins) / len(resolved) if resolved else 0.0

    from src.utils.fees import KALSHI_WINNER_FEE_RATE
    summary.total_pnl = sum(r.pnl for r in resolved)
    summary.total_cost = sum((r.price / 100.0) * r.quantity for r in resolved)
    # Use authoritative winner fee: $0.02/contract for wins only
    # Do NOT use stored r.fee (old estimates that may be wrong)
    summary.total_fees = sum(
        r.quantity * KALSHI_WINNER_FEE_RATE
        for r in resolved if r.outcome == "win"
    )
    summary.roi_percent = (
        (summary.total_pnl / summary.total_cost * 100) if summary.total_cost > 0 else 0.0
    )

    if wins:
        win_pnls = [r.pnl for r in wins]
        summary.avg_win = sum(win_pnls) / len(win_pnls)
        summary.largest_win = max(win_pnls)

    if losses:
        loss_pnls = [r.pnl for r in losses]
        summary.avg_loss = sum(loss_pnls) / len(loss_pnls)
        summary.largest_loss = min(loss_pnls)

    total_wins_amount = sum(r.pnl for r in wins) if wins else 0
    total_losses_amount = abs(sum(r.pnl for r in losses)) if losses else 0
    summary.profit_factor = (
        total_wins_amount / total_losses_amount if total_losses_amount > 0 else float("inf")
    )

    # Strategy breakdown
    strategies: Dict[str, List[TradeResolution]] = {}
    for r in resolutions:
        strat = r.strategy or "unknown"
        if strat not in strategies:
            strategies[strat] = []
        strategies[strat].append(r)

    for strat_name, strat_trades in strategies.items():
        strat_resolved = [t for t in strat_trades if t.resolved and t.pnl is not None]
        strat_wins = [t for t in strat_resolved if t.outcome == "win"]
        strat_losses = [t for t in strat_resolved if t.outcome == "loss"]

        ss = StrategySummary(
            strategy=strat_name,
            total_trades=len(strat_trades),
            resolved_trades=len(strat_resolved),
            wins=len(strat_wins),
            losses=len(strat_losses),
            win_rate=len(strat_wins) / len(strat_resolved) if strat_resolved else 0.0,
            total_pnl=sum(t.pnl for t in strat_resolved) if strat_resolved else 0.0,
            total_cost=sum((t.price / 100.0) * t.quantity for t in strat_resolved) if strat_resolved else 0.0,
            avg_pnl=sum(t.pnl for t in strat_resolved) / len(strat_resolved) if strat_resolved else 0.0,
        )
        summary.by_strategy[strat_name] = ss

    return summary


def run_resolution_report() -> str:
    """
    Run full resolution check and return a formatted report string.

    Returns:
        Markdown-formatted performance report.
    """
    logger.info("Running resolution tracker on all trades...")
    resolutions = check_resolved_markets()
    summary = get_performance_summary(resolutions)

    lines = []
    lines.append("# Trade Resolution Report")
    lines.append(f"**Generated:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append("")
    lines.append("## Overall Performance")
    lines.append(f"- **Total Trades:** {summary.total_trades}")
    lines.append(f"- **Resolved:** {summary.resolved_trades}")
    lines.append(f"- **Unresolved:** {summary.unresolved_trades}")
    lines.append(f"- **Wins:** {summary.wins}")
    lines.append(f"- **Losses:** {summary.losses}")
    lines.append(f"- **Win Rate:** {summary.win_rate:.1%}")
    lines.append(f"- **Total P&L:** ${summary.total_pnl:+,.2f}")
    lines.append(f"- **Total Cost:** ${summary.total_cost:,.2f}")
    lines.append(f"- **Total Fees:** ${summary.total_fees:,.2f}")
    lines.append(f"- **ROI:** {summary.roi_percent:+.2f}%")
    lines.append(f"- **Avg Win:** ${summary.avg_win:+.2f}")
    lines.append(f"- **Avg Loss:** ${summary.avg_loss:+.2f}")
    lines.append(f"- **Largest Win:** ${summary.largest_win:+.2f}")
    lines.append(f"- **Largest Loss:** ${summary.largest_loss:+.2f}")
    lines.append(f"- **Profit Factor:** {summary.profit_factor:.2f}")
    lines.append("")
    lines.append("## Strategy Breakdown")
    lines.append("")

    for strat_name, ss in sorted(
        summary.by_strategy.items(), key=lambda x: x[1].total_pnl, reverse=True
    ):
        lines.append(f"### {strat_name}")
        lines.append(f"- Trades: {ss.total_trades} (resolved: {ss.resolved_trades})")
        lines.append(f"- Wins/Losses: {ss.wins}/{ss.losses}")
        lines.append(f"- Win Rate: {ss.win_rate:.1%}")
        lines.append(f"- Total P&L: ${ss.total_pnl:+,.2f}")
        lines.append(f"- Total Cost: ${ss.total_cost:,.2f}")
        lines.append(f"- Avg P&L/Trade: ${ss.avg_pnl:+.2f}")
        lines.append("")

    # Unresolved trades summary
    unresolved = [r for r in resolutions if not r.resolved]
    if unresolved:
        lines.append("## Unresolved Trades")
        ticker_counts: Dict[str, int] = {}
        for r in unresolved:
            ticker_counts[r.ticker] = ticker_counts.get(r.ticker, 0) + 1
        for ticker, count in sorted(ticker_counts.items(), key=lambda x: -x[1]):
            lines.append(f"- {ticker}: {count} trades")

    return "\n".join(lines)
