"""
Performance attribution analytics for trading signals.

Breaks down P&L by various dimensions to understand what's working
and what isn't.
"""
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func

from src.data.database import get_db_session
from src.data.models import MarketDB, SignalExecutionDB
from src.utils.logging import logger


@dataclass
class PnLBreakdown:
    """P&L breakdown by a single dimension."""

    dimension: str  # e.g., "signal_strength", "market_category"
    groups: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # Each group contains: {total_pnl, trade_count, win_count, loss_count, avg_pnl, win_rate}


@dataclass
class TradeActivity:
    """Trade activity by time period."""

    period: str  # "hour", "day", "week"
    data: Dict[str, int] = field(default_factory=dict)  # period_key -> trade_count


@dataclass
class PerformanceMetrics:
    """Overall performance metrics."""

    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    total_pnl: float = 0.0
    avg_pnl: float = 0.0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    avg_edge: float = 0.0


class PerformanceAttribution:
    """
    Performance attribution engine.

    Analyzes resolved trades to understand P&L drivers:
    - By signal strength (strong, moderate, weak)
    - By market category (politics, crypto, weather, etc.)
    - By edge bucket (5-7%, 7-10%, 10%+)
    - By time of day
    - By holding period
    """

    def __init__(self) -> None:
        """Initialize performance attribution."""
        logger.info("Performance attribution initialized")

    def get_overall_metrics(
        self,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None,
        paper_only: bool = True,
    ) -> PerformanceMetrics:
        """
        Get overall performance metrics.

        Args:
            start_date: Filter start date.
            end_date: Filter end date.
            paper_only: Only include paper trades.

        Returns:
            PerformanceMetrics with aggregate stats.
        """
        metrics = PerformanceMetrics()

        try:
            with next(get_db_session()) as session:
                query = session.query(SignalExecutionDB).filter(
                    SignalExecutionDB.resolved_at.isnot(None)
                )

                if paper_only:
                    query = query.filter(SignalExecutionDB.is_paper == 1)
                if start_date:
                    query = query.filter(SignalExecutionDB.executed_at >= start_date)
                if end_date:
                    query = query.filter(SignalExecutionDB.executed_at <= end_date)

                executions = query.all()

                if not executions:
                    return metrics

                # Calculate metrics
                wins = [e for e in executions if e.pnl and e.pnl > 0]
                losses = [e for e in executions if e.pnl and e.pnl < 0]

                metrics.total_trades = len(executions)
                metrics.winning_trades = len(wins)
                metrics.losing_trades = len(losses)
                metrics.total_pnl = sum(e.pnl or 0 for e in executions)
                metrics.avg_pnl = metrics.total_pnl / metrics.total_trades if metrics.total_trades > 0 else 0
                metrics.win_rate = len(wins) / metrics.total_trades if metrics.total_trades > 0 else 0

                if wins:
                    metrics.avg_win = sum(e.pnl for e in wins) / len(wins)
                    metrics.largest_win = max(e.pnl for e in wins)

                if losses:
                    metrics.avg_loss = sum(e.pnl for e in losses) / len(losses)
                    metrics.largest_loss = min(e.pnl for e in losses)

                # Profit factor
                total_wins = sum(e.pnl for e in wins) if wins else 0
                total_losses = abs(sum(e.pnl for e in losses)) if losses else 0
                metrics.profit_factor = total_wins / total_losses if total_losses > 0 else float('inf')

                # Average edge
                edges = [e.edge for e in executions if e.edge]
                metrics.avg_edge = sum(edges) / len(edges) if edges else 0

        except Exception as e:
            logger.error("Failed to get overall metrics", error=str(e))

        return metrics

    def get_pnl_by_strength(
        self,
        paper_only: bool = True,
    ) -> PnLBreakdown:
        """
        Get P&L breakdown by signal strength.

        Returns:
            PnLBreakdown with groups: strong, moderate, weak
        """
        breakdown = PnLBreakdown(dimension="signal_strength")

        try:
            with next(get_db_session()) as session:
                query = session.query(SignalExecutionDB).filter(
                    SignalExecutionDB.resolved_at.isnot(None)
                )
                if paper_only:
                    query = query.filter(SignalExecutionDB.is_paper == 1)

                executions = query.all()

                # Group by strength
                by_strength = defaultdict(list)
                for e in executions:
                    strength = e.strength or "unknown"
                    by_strength[strength].append(e)

                # Calculate stats for each group
                for strength, trades in by_strength.items():
                    wins = [t for t in trades if t.pnl and t.pnl > 0]
                    breakdown.groups[strength] = {
                        "total_pnl": sum(t.pnl or 0 for t in trades),
                        "trade_count": len(trades),
                        "win_count": len(wins),
                        "loss_count": len(trades) - len(wins),
                        "avg_pnl": sum(t.pnl or 0 for t in trades) / len(trades) if trades else 0,
                        "win_rate": len(wins) / len(trades) if trades else 0,
                    }

        except Exception as e:
            logger.error("Failed to get PnL by strength", error=str(e))

        return breakdown

    def get_pnl_by_category(
        self,
        paper_only: bool = True,
    ) -> PnLBreakdown:
        """
        Get P&L breakdown by market category.

        Returns:
            PnLBreakdown with groups by category (politics, crypto, etc.)
        """
        breakdown = PnLBreakdown(dimension="market_category")

        try:
            with next(get_db_session()) as session:
                query = session.query(SignalExecutionDB).filter(
                    SignalExecutionDB.resolved_at.isnot(None)
                )
                if paper_only:
                    query = query.filter(SignalExecutionDB.is_paper == 1)

                executions = query.all()

                # Get category for each ticker
                ticker_category = {}
                tickers = list(set(e.ticker for e in executions))
                markets = session.query(MarketDB).filter(MarketDB.ticker.in_(tickers)).all()
                for m in markets:
                    ticker_category[m.ticker] = m.category or "Unknown"

                # Group by category
                by_category = defaultdict(list)
                for e in executions:
                    category = ticker_category.get(e.ticker, "Unknown")
                    by_category[category].append(e)

                # Calculate stats for each group
                for category, trades in by_category.items():
                    wins = [t for t in trades if t.pnl and t.pnl > 0]
                    breakdown.groups[category] = {
                        "total_pnl": sum(t.pnl or 0 for t in trades),
                        "trade_count": len(trades),
                        "win_count": len(wins),
                        "loss_count": len(trades) - len(wins),
                        "avg_pnl": sum(t.pnl or 0 for t in trades) / len(trades) if trades else 0,
                        "win_rate": len(wins) / len(trades) if trades else 0,
                    }

        except Exception as e:
            logger.error("Failed to get PnL by category", error=str(e))

        return breakdown

    def get_pnl_by_edge(
        self,
        paper_only: bool = True,
        buckets: List[Tuple[float, float]] = None,
    ) -> PnLBreakdown:
        """
        Get P&L breakdown by edge bucket.

        Args:
            paper_only: Only include paper trades.
            buckets: List of (min, max) edge buckets. Default: [(0.05, 0.07), (0.07, 0.10), (0.10, 1.0)]

        Returns:
            PnLBreakdown with groups by edge bucket.
        """
        if buckets is None:
            buckets = [(0.05, 0.07), (0.07, 0.10), (0.10, 1.0)]

        breakdown = PnLBreakdown(dimension="edge_bucket")

        try:
            with next(get_db_session()) as session:
                query = session.query(SignalExecutionDB).filter(
                    SignalExecutionDB.resolved_at.isnot(None)
                )
                if paper_only:
                    query = query.filter(SignalExecutionDB.is_paper == 1)

                executions = query.all()

                # Group by edge bucket
                by_bucket = defaultdict(list)
                for e in executions:
                    edge = e.edge or 0
                    for min_edge, max_edge in buckets:
                        if min_edge <= edge < max_edge:
                            bucket_name = f"{min_edge*100:.0f}-{max_edge*100:.0f}%"
                            by_bucket[bucket_name].append(e)
                            break

                # Calculate stats for each bucket
                for bucket, trades in by_bucket.items():
                    wins = [t for t in trades if t.pnl and t.pnl > 0]
                    breakdown.groups[bucket] = {
                        "total_pnl": sum(t.pnl or 0 for t in trades),
                        "trade_count": len(trades),
                        "win_count": len(wins),
                        "loss_count": len(trades) - len(wins),
                        "avg_pnl": sum(t.pnl or 0 for t in trades) / len(trades) if trades else 0,
                        "win_rate": len(wins) / len(trades) if trades else 0,
                    }

        except Exception as e:
            logger.error("Failed to get PnL by edge", error=str(e))

        return breakdown

    def get_trade_activity_by_hour(
        self,
        paper_only: bool = True,
    ) -> TradeActivity:
        """
        Get trade count by hour of day.

        Returns:
            TradeActivity with hourly counts.
        """
        activity = TradeActivity(period="hour")

        try:
            with next(get_db_session()) as session:
                query = session.query(SignalExecutionDB)
                if paper_only:
                    query = query.filter(SignalExecutionDB.is_paper == 1)

                executions = query.all()

                # Count by hour
                for e in executions:
                    if e.executed_at:
                        hour = e.executed_at.hour
                        hour_key = f"{hour:02d}:00"
                        activity.data[hour_key] = activity.data.get(hour_key, 0) + 1

                # Fill in missing hours
                for h in range(24):
                    hour_key = f"{h:02d}:00"
                    if hour_key not in activity.data:
                        activity.data[hour_key] = 0

        except Exception as e:
            logger.error("Failed to get trade activity by hour", error=str(e))

        return activity

    def get_trade_activity_by_day(
        self,
        days: int = 30,
        paper_only: bool = True,
    ) -> TradeActivity:
        """
        Get trade count by day for the last N days.

        Args:
            days: Number of days to look back.
            paper_only: Only include paper trades.

        Returns:
            TradeActivity with daily counts.
        """
        activity = TradeActivity(period="day")
        start_date = datetime.now(timezone.utc) - timedelta(days=days)

        try:
            with next(get_db_session()) as session:
                query = session.query(SignalExecutionDB).filter(
                    SignalExecutionDB.executed_at >= start_date
                )
                if paper_only:
                    query = query.filter(SignalExecutionDB.is_paper == 1)

                executions = query.all()

                # Count by day
                for e in executions:
                    if e.executed_at:
                        day_key = e.executed_at.strftime("%Y-%m-%d")
                        activity.data[day_key] = activity.data.get(day_key, 0) + 1

        except Exception as e:
            logger.error("Failed to get trade activity by day", error=str(e))

        return activity

    def get_detailed_trades(
        self,
        limit: int = 100,
        paper_only: bool = True,
    ) -> List[Dict]:
        """
        Get detailed trade list for analysis.

        Returns:
            List of trade dicts with all relevant fields.
        """
        trades = []

        try:
            with next(get_db_session()) as session:
                query = session.query(SignalExecutionDB)
                if paper_only:
                    query = query.filter(SignalExecutionDB.is_paper == 1)

                executions = (
                    query.order_by(SignalExecutionDB.executed_at.desc())
                    .limit(limit)
                    .all()
                )

                # Get market titles
                tickers = list(set(e.ticker for e in executions))
                markets = session.query(MarketDB).filter(MarketDB.ticker.in_(tickers)).all()
                ticker_info = {m.ticker: {"title": m.title, "category": m.category} for m in markets}

                for e in executions:
                    market_info = ticker_info.get(e.ticker, {})
                    trades.append({
                        "signal_id": e.signal_id,
                        "ticker": e.ticker,
                        "title": market_info.get("title", "Unknown"),
                        "category": market_info.get("category", "Unknown"),
                        "action": e.action,
                        "side": e.side,
                        "quantity": e.quantity,
                        "entry_price": e.price,
                        "fill_price": e.fill_price,
                        "edge": e.edge,
                        "strength": e.strength,
                        "status": e.status,
                        "pnl": float(e.pnl) if e.pnl else None,
                        "pnl_percent": e.pnl_percent,
                        "resolution": e.resolution,
                        "executed_at": e.executed_at.isoformat() if e.executed_at else None,
                        "resolved_at": e.resolved_at.isoformat() if e.resolved_at else None,
                    })

        except Exception as e:
            logger.error("Failed to get detailed trades", error=str(e))

        return trades

    def get_attribution_summary(self, paper_only: bool = True) -> Dict[str, Any]:
        """
        Get comprehensive attribution summary.

        Returns:
            Dict with all attribution breakdowns.
        """
        return {
            "overall": self.get_overall_metrics(paper_only=paper_only).__dict__,
            "by_strength": self.get_pnl_by_strength(paper_only=paper_only).groups,
            "by_category": self.get_pnl_by_category(paper_only=paper_only).groups,
            "by_edge": self.get_pnl_by_edge(paper_only=paper_only).groups,
            "activity_by_hour": self.get_trade_activity_by_hour(paper_only=paper_only).data,
            "activity_by_day": self.get_trade_activity_by_day(paper_only=paper_only).data,
        }


# Global instance
_attribution: Optional[PerformanceAttribution] = None


def get_performance_attribution() -> PerformanceAttribution:
    """Get or create global performance attribution instance."""
    global _attribution
    if _attribution is None:
        _attribution = PerformanceAttribution()
    return _attribution
