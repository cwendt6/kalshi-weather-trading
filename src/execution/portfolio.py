"""
Portfolio tracker for positions and P&L.

Tracks positions, calculates unrealized P&L, total portfolio value,
and maintains equity curve for performance analysis.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional

from src.data.database import get_db_session
from src.data.models import MarketDB, PortfolioSnapshotDB, PositionDB, PriceDB, TradeDB
from src.utils.logging import logger


@dataclass
class Position:
    """A portfolio position."""

    ticker: str
    side: str  # "yes" or "no"
    quantity: int
    average_price: int  # Entry price in cents
    market_price: int  # Current price in cents

    # P&L
    unrealized_pnl: float
    unrealized_pnl_pct: float
    realized_pnl: float

    # Market info
    market_title: str = ""
    market_status: str = ""

    @property
    def cost_basis(self) -> float:
        """Total cost basis in dollars."""
        return self.quantity * self.average_price / 100.0

    @property
    def market_value(self) -> float:
        """Current market value in dollars."""
        return self.quantity * self.market_price / 100.0

    @property
    def total_pnl(self) -> float:
        """Total P&L (realized + unrealized)."""
        return self.realized_pnl + self.unrealized_pnl


@dataclass
class PortfolioSummary:
    """Portfolio summary with all positions and metrics."""

    # Cash and value
    cash_balance: float
    total_position_value: float
    total_equity: float

    # P&L
    total_unrealized_pnl: float
    total_realized_pnl: float
    total_pnl: float

    # Returns
    daily_return: float
    total_return: float

    # Positions
    positions: List[Position]
    position_count: int

    # Risk metrics
    exposure_pct: float
    largest_position_pct: float

    timestamp: datetime = datetime.now(timezone.utc)


class PortfolioTracker:
    """
    Tracks portfolio positions and performance.

    Features:
    - Sync positions from database
    - Calculate unrealized P&L per position
    - Calculate total portfolio value
    - Track historical equity curve
    - Store snapshots for performance analysis
    """

    def __init__(self, initial_balance: float = 1000.0) -> None:
        """
        Initialize portfolio tracker.

        Args:
            initial_balance: Starting cash balance.
        """
        self.initial_balance = initial_balance
        self._cached_positions: Optional[List[Position]] = None
        self._cache_timestamp: Optional[datetime] = None
        self._cache_ttl = timedelta(seconds=30)

        logger.info("Portfolio tracker initialized", initial_balance=initial_balance)

    def _get_current_price(self, ticker: str) -> Optional[int]:
        """Get current market price for a ticker."""
        with next(get_db_session()) as session:
            price = (
                session.query(PriceDB)
                .filter(PriceDB.ticker == ticker)
                .order_by(PriceDB.timestamp.desc())
                .first()
            )

            if price:
                # Use mid price
                # CRITICAL: Do NOT use `or 50` — it treats a real bid of 0
                # as falsy and falls back to 50, inflating settled positions.
                yes_bid = int(price.yes_bid) if price.yes_bid is not None else 50
                yes_ask = int(price.yes_ask) if price.yes_ask is not None else 50
                return (yes_bid + yes_ask) // 2

            return None

    def _get_market_info(self, ticker: str) -> Dict[str, str]:
        """Get market info for a ticker."""
        with next(get_db_session()) as session:
            market = session.query(MarketDB).filter_by(ticker=ticker).first()

            if market:
                return {
                    "title": str(market.title or ""),
                    "status": str(market.status or ""),
                }

            return {"title": "", "status": ""}

    def get_positions(self, force_refresh: bool = False) -> List[Position]:
        """
        Get all current positions with P&L.

        Args:
            force_refresh: Bypass cache and fetch fresh data.

        Returns:
            List of Position objects.
        """
        # Check cache
        now = datetime.now(timezone.utc)
        if (
            not force_refresh
            and self._cached_positions is not None
            and self._cache_timestamp is not None
            and now - self._cache_timestamp < self._cache_ttl
        ):
            return self._cached_positions

        positions = []

        with next(get_db_session()) as session:
            db_positions = session.query(PositionDB).all()

            for pos in db_positions:
                ticker = str(pos.ticker)
                quantity = int(pos.quantity or 0)
                average_price = int(pos.average_price or 0)
                side = str(pos.side or "unknown")
                realized_pnl = float(pos.realized_pnl or 0)

                if quantity <= 0:
                    continue

                # Get current price
                market_price = self._get_current_price(ticker) or average_price

                # Calculate unrealized P&L
                # Note: market_price = YES mid-price from PriceDB
                # For YES: PnL = current_YES - entry_YES
                # For NO: entry is NO cost (e.g. 98¢), NO value = 100 - YES_mid
                #   PnL = (100 - YES_mid) - NO_entry_cost
                if side == "yes":
                    unrealized_pnl = quantity * (market_price - average_price) / 100.0
                else:
                    unrealized_pnl = quantity * ((100 - market_price) - average_price) / 100.0

                cost_basis = quantity * average_price / 100.0
                unrealized_pnl_pct = unrealized_pnl / cost_basis if cost_basis > 0 else 0

                # Get market info
                market_info = self._get_market_info(ticker)

                positions.append(
                    Position(
                        ticker=ticker,
                        side=side,
                        quantity=quantity,
                        average_price=average_price,
                        market_price=market_price,
                        unrealized_pnl=unrealized_pnl,
                        unrealized_pnl_pct=unrealized_pnl_pct,
                        realized_pnl=realized_pnl,
                        market_title=market_info["title"],
                        market_status=market_info["status"],
                    )
                )

        # Update cache
        self._cached_positions = positions
        self._cache_timestamp = now

        return positions

    def get_cash_balance(self) -> float:
        """Get current cash balance."""
        with next(get_db_session()) as session:
            # Get latest snapshot
            snapshot = (
                session.query(PortfolioSnapshotDB)
                .order_by(PortfolioSnapshotDB.timestamp.desc())
                .first()
            )

            if snapshot and snapshot.balance:
                return float(snapshot.balance)

            return self.initial_balance

    def get_summary(self) -> PortfolioSummary:
        """Get complete portfolio summary."""
        positions = self.get_positions(force_refresh=True)
        cash_balance = self.get_cash_balance()

        # Calculate totals
        total_position_value = sum(p.market_value for p in positions)
        total_unrealized_pnl = sum(p.unrealized_pnl for p in positions)
        total_realized_pnl = sum(p.realized_pnl for p in positions)
        total_equity = cash_balance + total_position_value

        # Calculate returns
        daily_return = self._calculate_daily_return(total_equity)
        total_return = (total_equity - self.initial_balance) / self.initial_balance

        # Risk metrics
        exposure_pct = total_position_value / total_equity if total_equity > 0 else 0
        largest_position_pct = (
            max(p.market_value for p in positions) / total_equity
            if positions and total_equity > 0
            else 0
        )

        return PortfolioSummary(
            cash_balance=cash_balance,
            total_position_value=total_position_value,
            total_equity=total_equity,
            total_unrealized_pnl=total_unrealized_pnl,
            total_realized_pnl=total_realized_pnl,
            total_pnl=total_unrealized_pnl + total_realized_pnl,
            daily_return=daily_return,
            total_return=total_return,
            positions=positions,
            position_count=len(positions),
            exposure_pct=exposure_pct,
            largest_position_pct=largest_position_pct,
            timestamp=datetime.now(timezone.utc),
        )

    def _calculate_daily_return(self, current_equity: float) -> float:
        """Calculate today's return."""
        with next(get_db_session()) as session:
            today_start = datetime.now(timezone.utc).replace(
                hour=0, minute=0, second=0, microsecond=0
            )

            # Get yesterday's closing equity
            yesterday_snapshot = (
                session.query(PortfolioSnapshotDB)
                .filter(PortfolioSnapshotDB.timestamp < today_start)
                .order_by(PortfolioSnapshotDB.timestamp.desc())
                .first()
            )

            if yesterday_snapshot and yesterday_snapshot.total_equity:
                start_equity = float(yesterday_snapshot.total_equity)
                if start_equity > 0:
                    return (current_equity - start_equity) / start_equity

            return 0.0

    def save_snapshot(self) -> PortfolioSnapshotDB:
        """Save current portfolio snapshot to database."""
        summary = self.get_summary()

        with next(get_db_session()) as session:
            snapshot = PortfolioSnapshotDB(
                timestamp=summary.timestamp,
                balance=Decimal(str(summary.cash_balance)),
                total_position_value=Decimal(str(summary.total_position_value)),
                unrealized_pnl=Decimal(str(summary.total_unrealized_pnl)),
                realized_pnl=Decimal(str(summary.total_realized_pnl)),
                total_equity=Decimal(str(summary.total_equity)),
                daily_return=summary.daily_return,
            )

            session.add(snapshot)
            session.commit()

            logger.info(
                "Portfolio snapshot saved",
                equity=summary.total_equity,
                pnl=summary.total_pnl,
            )

            # Refresh to get ID
            session.refresh(snapshot)
            return snapshot

    def get_equity_curve(
        self,
        days: int = 30,
    ) -> List[Dict[str, Any]]:
        """
        Get historical equity curve.

        Args:
            days: Number of days of history.

        Returns:
            List of dicts with timestamp and equity.
        """
        with next(get_db_session()) as session:
            since = datetime.now(timezone.utc) - timedelta(days=days)

            snapshots = (
                session.query(PortfolioSnapshotDB)
                .filter(PortfolioSnapshotDB.timestamp >= since)
                .order_by(PortfolioSnapshotDB.timestamp.asc())
                .all()
            )

            return [
                {
                    "timestamp": s.timestamp,
                    "equity": float(s.total_equity or 0),
                    "daily_return": float(s.daily_return or 0),
                }
                for s in snapshots
            ]

    def get_trade_history(
        self,
        ticker: Optional[str] = None,
        days: int = 30,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        Get trade history.

        Args:
            ticker: Optional filter by ticker.
            days: Number of days of history.
            limit: Maximum records to return.

        Returns:
            List of trade dicts.
        """
        with next(get_db_session()) as session:
            query = session.query(TradeDB)

            if ticker:
                query = query.filter(TradeDB.ticker == ticker)

            since = datetime.now(timezone.utc) - timedelta(days=days)
            query = query.filter(TradeDB.timestamp >= since)

            trades = (
                query.order_by(TradeDB.timestamp.desc())
                .limit(limit)
                .all()
            )

            return [
                {
                    "order_id": t.order_id,
                    "ticker": t.ticker,
                    "side": t.side,
                    "action": t.action,
                    "quantity": t.quantity,
                    "price": t.price,
                    "fee": float(t.fee or 0),
                    "status": t.status,
                    "timestamp": t.timestamp,
                }
                for t in trades
            ]

    def calculate_performance_metrics(self, days: int = 30) -> Dict[str, Any]:
        """
        Calculate performance metrics.

        Args:
            days: Period for calculation.

        Returns:
            Dict of performance metrics.
        """
        equity_curve = self.get_equity_curve(days=days)

        if len(equity_curve) < 2:
            return {
                "total_return": 0.0,
                "daily_returns": [],
                "sharpe_ratio": 0.0,
                "max_drawdown": 0.0,
                "win_rate": 0.0,
            }

        # Calculate returns
        equities = [p["equity"] for p in equity_curve]
        daily_returns = [
            (equities[i] - equities[i - 1]) / equities[i - 1]
            if equities[i - 1] > 0
            else 0
            for i in range(1, len(equities))
        ]

        # Total return
        total_return = (equities[-1] - equities[0]) / equities[0] if equities[0] > 0 else 0

        # Sharpe ratio (assuming 0 risk-free rate)
        import statistics

        if len(daily_returns) > 1:
            mean_return = statistics.mean(daily_returns)
            std_return = statistics.stdev(daily_returns)
            sharpe_ratio = (
                mean_return / std_return * (252 ** 0.5) if std_return > 0 else 0
            )
        else:
            sharpe_ratio = 0.0

        # Max drawdown
        peak = equities[0]
        max_drawdown = 0.0
        for equity in equities:
            if equity > peak:
                peak = equity
            drawdown = (peak - equity) / peak if peak > 0 else 0
            max_drawdown = max(max_drawdown, drawdown)

        # Win rate from trades
        trades = self.get_trade_history(days=days)
        if trades:
            winning_trades = sum(1 for t in trades if t.get("fee", 0) < 0)  # Simplified
            win_rate = winning_trades / len(trades) if trades else 0
        else:
            win_rate = 0.0

        return {
            "total_return": total_return,
            "daily_returns": daily_returns,
            "sharpe_ratio": sharpe_ratio,
            "max_drawdown": max_drawdown,
            "win_rate": win_rate,
            "period_days": days,
        }


# Global instance
_tracker: Optional[PortfolioTracker] = None


def get_portfolio_tracker() -> PortfolioTracker:
    """Get or create the global portfolio tracker instance."""
    global _tracker
    if _tracker is None:
        _tracker = PortfolioTracker()
    return _tracker


def get_portfolio_summary() -> PortfolioSummary:
    """Convenience function to get portfolio summary."""
    return get_portfolio_tracker().get_summary()


def get_positions() -> List[Position]:
    """Convenience function to get positions."""
    return get_portfolio_tracker().get_positions()
