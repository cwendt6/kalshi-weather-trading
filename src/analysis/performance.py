"""
Performance analytics for trading system.

Calculates comprehensive trading metrics:
- Return metrics (total, daily, CAGR)
- Risk metrics (Sharpe, Sortino, max drawdown)
- Trade metrics (win rate, profit factor)
- Edge analysis (realized vs predicted)
"""
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from statistics import mean, stdev
from typing import Any, Dict, List, Optional, Tuple

from src.data.database import get_db_session
from src.data.models import ForecastDB, PortfolioSnapshotDB, TradeDB
from src.utils.logging import logger


@dataclass
class PerformanceMetrics:
    """Comprehensive performance metrics."""

    # Period
    start_date: datetime
    end_date: datetime
    trading_days: int

    # Return metrics
    total_return: float
    total_return_pct: float
    daily_return_avg: float
    daily_return_std: float
    cagr: float  # Compound Annual Growth Rate

    # Risk metrics
    sharpe_ratio: float
    sortino_ratio: float
    max_drawdown: float
    max_drawdown_duration_days: int
    volatility: float  # Annualized

    # Trade metrics
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    avg_win: float
    avg_loss: float
    profit_factor: float
    avg_trade_pnl: float

    # Edge metrics
    avg_predicted_edge: float
    avg_realized_edge: float
    edge_accuracy: float  # How often edge prediction was directionally correct

    # Current state
    current_equity: float
    peak_equity: float


@dataclass
class PeriodSummary:
    """Summary for a specific time period."""

    period: str  # "daily", "weekly", "monthly"
    start_date: datetime
    end_date: datetime
    return_pct: float
    trades: int
    pnl: float
    win_rate: float


class PerformanceAnalyzer:
    """
    Analyzes trading performance across multiple dimensions.

    Provides:
    - Return calculations
    - Risk-adjusted metrics
    - Trade statistics
    - Edge analysis
    - Period summaries
    - CSV export
    """

    # Annualization factors
    TRADING_DAYS_PER_YEAR = 252
    RISK_FREE_RATE = 0.05  # 5% annual risk-free rate

    def __init__(self, initial_equity: float = 1000.0) -> None:
        """
        Initialize performance analyzer.

        Args:
            initial_equity: Starting equity for calculations.
        """
        self.initial_equity = initial_equity

        logger.info("Performance analyzer initialized", initial_equity=initial_equity)

    def _get_equity_series(
        self, start_date: datetime, end_date: datetime
    ) -> List[Tuple[datetime, float]]:
        """Get equity time series from database."""
        with next(get_db_session()) as session:
            snapshots = (
                session.query(PortfolioSnapshotDB)
                .filter(PortfolioSnapshotDB.timestamp >= start_date)
                .filter(PortfolioSnapshotDB.timestamp <= end_date)
                .order_by(PortfolioSnapshotDB.timestamp.asc())
                .all()
            )

            result: List[Tuple[datetime, float]] = []
            for s in snapshots:
                ts = s.timestamp
                if isinstance(ts, datetime):
                    result.append((ts, float(s.total_equity or 0)))
            return result

    def _get_trades(
        self, start_date: datetime, end_date: datetime
    ) -> List[Dict[str, Any]]:
        """Get trades from database."""
        with next(get_db_session()) as session:
            trades = (
                session.query(TradeDB)
                .filter(TradeDB.timestamp >= start_date)
                .filter(TradeDB.timestamp <= end_date)
                .filter(TradeDB.status == "filled")
                .order_by(TradeDB.timestamp.asc())
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
                    "timestamp": t.timestamp,
                }
                for t in trades
            ]

    def _get_forecasts(
        self, start_date: datetime, end_date: datetime
    ) -> List[Dict[str, Any]]:
        """Get forecasts from database."""
        with next(get_db_session()) as session:
            forecasts = (
                session.query(ForecastDB)
                .filter(ForecastDB.timestamp >= start_date)
                .filter(ForecastDB.timestamp <= end_date)
                .order_by(ForecastDB.timestamp.asc())
                .all()
            )

            return [
                {
                    "ticker": f.ticker,
                    "probability": float(f.probability or 0.5),
                    "edge": float(f.edge or 0),
                    "market_probability": float(f.market_probability or 0.5),
                    "confidence": float(f.confidence or 0.5),
                    "timestamp": f.timestamp,
                }
                for f in forecasts
            ]

    def _calculate_returns(
        self, equity_series: List[Tuple[datetime, float]]
    ) -> List[float]:
        """Calculate daily returns from equity series."""
        if len(equity_series) < 2:
            return []

        returns = []
        for i in range(1, len(equity_series)):
            prev_equity = equity_series[i - 1][1]
            curr_equity = equity_series[i][1]
            if prev_equity > 0:
                daily_return = (curr_equity - prev_equity) / prev_equity
                returns.append(daily_return)

        return returns

    def _calculate_drawdown(
        self, equity_series: List[Tuple[datetime, float]]
    ) -> Tuple[float, int]:
        """Calculate max drawdown and duration."""
        if not equity_series:
            return 0.0, 0

        peak = equity_series[0][1]
        max_drawdown = 0.0
        max_duration = 0
        current_duration = 0

        for _, equity in equity_series:
            if equity > peak:
                peak = equity
                current_duration = 0
            else:
                drawdown = (peak - equity) / peak if peak > 0 else 0
                max_drawdown = max(max_drawdown, drawdown)
                current_duration += 1
                max_duration = max(max_duration, current_duration)

        return max_drawdown, max_duration

    def _calculate_trade_stats(
        self, trades: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Calculate trade statistics."""
        if not trades:
            return {
                "total": 0,
                "winning": 0,
                "losing": 0,
                "win_rate": 0.0,
                "avg_win": 0.0,
                "avg_loss": 0.0,
                "profit_factor": 0.0,
                "avg_pnl": 0.0,
            }

        # Group trades by ticker to calculate P&L
        # For simplicity, we'll track buy/sell pairs
        positions: Dict[str, List[Dict]] = {}
        pnls = []

        for trade in trades:
            ticker = trade["ticker"]
            if ticker not in positions:
                positions[ticker] = []

            if trade["action"] == "buy":
                positions[ticker].append(trade)
            else:  # sell
                if positions[ticker]:
                    # Match with earliest buy (FIFO)
                    buy_trade = positions[ticker].pop(0)
                    pnl = (trade["price"] - buy_trade["price"]) * trade["quantity"] / 100.0
                    pnl -= trade["fee"]
                    pnls.append(pnl)

        if not pnls:
            return {
                "total": len(trades),
                "winning": 0,
                "losing": 0,
                "win_rate": 0.0,
                "avg_win": 0.0,
                "avg_loss": 0.0,
                "profit_factor": 0.0,
                "avg_pnl": 0.0,
            }

        winning = [p for p in pnls if p > 0]
        losing = [p for p in pnls if p < 0]

        gross_profit = sum(winning) if winning else 0
        gross_loss = abs(sum(losing)) if losing else 0

        return {
            "total": len(trades),
            "winning": len(winning),
            "losing": len(losing),
            "win_rate": len(winning) / len(pnls) if pnls else 0,
            "avg_win": mean(winning) if winning else 0,
            "avg_loss": abs(mean(losing)) if losing else 0,
            "profit_factor": gross_profit / gross_loss if gross_loss > 0 else float("inf"),
            "avg_pnl": mean(pnls) if pnls else 0,
        }

    def _calculate_edge_metrics(
        self, forecasts: List[Dict[str, Any]]
    ) -> Dict[str, float]:
        """Calculate edge prediction accuracy."""
        if not forecasts:
            return {
                "avg_predicted_edge": 0.0,
                "avg_realized_edge": 0.0,
                "edge_accuracy": 0.0,
            }

        edges = [f["edge"] for f in forecasts if f.get("edge")]

        # For realized edge, we'd need market resolution data
        # For now, use predicted edge
        return {
            "avg_predicted_edge": mean(edges) if edges else 0.0,
            "avg_realized_edge": 0.0,  # Would need resolution data
            "edge_accuracy": 0.0,  # Would need resolution data
        }

    def calculate_metrics(
        self,
        days: int = 30,
        end_date: Optional[datetime] = None,
    ) -> PerformanceMetrics:
        """
        Calculate comprehensive performance metrics.

        Args:
            days: Number of days to analyze.
            end_date: End date (defaults to now).

        Returns:
            PerformanceMetrics with all calculations.
        """
        if end_date is None:
            end_date = datetime.now(timezone.utc)

        start_date = end_date - timedelta(days=days)

        # Get data
        equity_series = self._get_equity_series(start_date, end_date)
        trades = self._get_trades(start_date, end_date)
        forecasts = self._get_forecasts(start_date, end_date)

        # Calculate returns
        returns = self._calculate_returns(equity_series)

        # Return metrics
        if equity_series:
            start_equity = equity_series[0][1] if equity_series else self.initial_equity
            end_equity = equity_series[-1][1] if equity_series else self.initial_equity
            total_return = end_equity - start_equity
            total_return_pct = total_return / start_equity if start_equity > 0 else 0
        else:
            start_equity = self.initial_equity
            end_equity = self.initial_equity
            total_return = 0.0
            total_return_pct = 0.0

        daily_return_avg = mean(returns) if returns else 0.0
        daily_return_std = stdev(returns) if len(returns) > 1 else 0.0

        # CAGR
        trading_days = len(equity_series)
        if trading_days > 0 and start_equity > 0:
            years = trading_days / self.TRADING_DAYS_PER_YEAR
            cagr = (end_equity / start_equity) ** (1 / years) - 1 if years > 0 else 0
        else:
            cagr = 0.0

        # Risk metrics
        volatility = daily_return_std * (self.TRADING_DAYS_PER_YEAR ** 0.5)

        # Sharpe ratio
        excess_return = daily_return_avg - self.RISK_FREE_RATE / self.TRADING_DAYS_PER_YEAR
        sharpe = (
            excess_return / daily_return_std * (self.TRADING_DAYS_PER_YEAR ** 0.5)
            if daily_return_std > 0
            else 0
        )

        # Sortino ratio (uses downside deviation)
        negative_returns = [r for r in returns if r < 0]
        downside_std = stdev(negative_returns) if len(negative_returns) > 1 else 0.0
        sortino = (
            excess_return / downside_std * (self.TRADING_DAYS_PER_YEAR ** 0.5)
            if downside_std > 0
            else 0
        )

        # Drawdown
        max_drawdown, max_dd_duration = self._calculate_drawdown(equity_series)

        # Trade stats
        trade_stats = self._calculate_trade_stats(trades)

        # Edge metrics
        edge_metrics = self._calculate_edge_metrics(forecasts)

        # Peak equity
        peak_equity = max(e for _, e in equity_series) if equity_series else self.initial_equity

        return PerformanceMetrics(
            start_date=start_date,
            end_date=end_date,
            trading_days=trading_days,
            total_return=total_return,
            total_return_pct=total_return_pct,
            daily_return_avg=daily_return_avg,
            daily_return_std=daily_return_std,
            cagr=cagr,
            sharpe_ratio=sharpe,
            sortino_ratio=sortino,
            max_drawdown=max_drawdown,
            max_drawdown_duration_days=max_dd_duration,
            volatility=volatility,
            total_trades=trade_stats["total"],
            winning_trades=trade_stats["winning"],
            losing_trades=trade_stats["losing"],
            win_rate=trade_stats["win_rate"],
            avg_win=trade_stats["avg_win"],
            avg_loss=trade_stats["avg_loss"],
            profit_factor=trade_stats["profit_factor"],
            avg_trade_pnl=trade_stats["avg_pnl"],
            avg_predicted_edge=edge_metrics["avg_predicted_edge"],
            avg_realized_edge=edge_metrics["avg_realized_edge"],
            edge_accuracy=edge_metrics["edge_accuracy"],
            current_equity=end_equity,
            peak_equity=peak_equity,
        )

    def generate_period_summaries(
        self,
        period: str = "daily",
        days: int = 30,
    ) -> List[PeriodSummary]:
        """
        Generate summaries by time period.

        Args:
            period: "daily", "weekly", or "monthly".
            days: Days of history.

        Returns:
            List of PeriodSummary objects.
        """
        end_date = datetime.now(timezone.utc)
        start_date = end_date - timedelta(days=days)

        equity_series = self._get_equity_series(start_date, end_date)
        trades = self._get_trades(start_date, end_date)

        summaries = []

        # Group by period
        if period == "daily":
            delta = timedelta(days=1)
        elif period == "weekly":
            delta = timedelta(weeks=1)
        else:  # monthly
            delta = timedelta(days=30)

        current_start = start_date
        while current_start < end_date:
            current_end = min(current_start + delta, end_date)

            # Get equity for period
            period_equity = [
                (ts, eq) for ts, eq in equity_series
                if current_start <= ts < current_end
            ]

            # Get trades for period
            period_trades = [
                t for t in trades
                if current_start <= t["timestamp"] < current_end
            ]

            if period_equity:
                start_eq = period_equity[0][1]
                end_eq = period_equity[-1][1]
                return_pct = (end_eq - start_eq) / start_eq if start_eq > 0 else 0
                pnl = end_eq - start_eq
            else:
                return_pct = 0.0
                pnl = 0.0

            # Calculate win rate for period trades
            trade_stats = self._calculate_trade_stats(period_trades)

            summaries.append(
                PeriodSummary(
                    period=period,
                    start_date=current_start,
                    end_date=current_end,
                    return_pct=return_pct,
                    trades=len(period_trades),
                    pnl=pnl,
                    win_rate=trade_stats["win_rate"],
                )
            )

            current_start = current_end

        return summaries

    def export_to_csv(
        self,
        filepath: str,
        days: int = 30,
    ) -> str:
        """
        Export performance data to CSV.

        Args:
            filepath: Output file path.
            days: Days of history.

        Returns:
            Path to created file.
        """
        metrics = self.calculate_metrics(days=days)
        summaries = self.generate_period_summaries(period="daily", days=days)

        # Create CSV content
        output = StringIO()
        writer = csv.writer(output)

        # Metrics section
        writer.writerow(["Performance Metrics"])
        writer.writerow(["Metric", "Value"])
        writer.writerow(["Start Date", metrics.start_date.strftime("%Y-%m-%d")])
        writer.writerow(["End Date", metrics.end_date.strftime("%Y-%m-%d")])
        writer.writerow(["Trading Days", metrics.trading_days])
        writer.writerow(["Total Return", f"${metrics.total_return:.2f}"])
        writer.writerow(["Total Return %", f"{metrics.total_return_pct:.2%}"])
        writer.writerow(["CAGR", f"{metrics.cagr:.2%}"])
        writer.writerow(["Sharpe Ratio", f"{metrics.sharpe_ratio:.2f}"])
        writer.writerow(["Sortino Ratio", f"{metrics.sortino_ratio:.2f}"])
        writer.writerow(["Max Drawdown", f"{metrics.max_drawdown:.2%}"])
        writer.writerow(["Volatility", f"{metrics.volatility:.2%}"])
        writer.writerow(["Total Trades", metrics.total_trades])
        writer.writerow(["Win Rate", f"{metrics.win_rate:.1%}"])
        writer.writerow(["Profit Factor", f"{metrics.profit_factor:.2f}"])
        writer.writerow([])

        # Daily summaries section
        writer.writerow(["Daily Performance"])
        writer.writerow(["Date", "Return %", "Trades", "P&L", "Win Rate"])
        for summary in summaries:
            writer.writerow([
                summary.start_date.strftime("%Y-%m-%d"),
                f"{summary.return_pct:.2%}",
                summary.trades,
                f"${summary.pnl:.2f}",
                f"{summary.win_rate:.0%}",
            ])

        # Write to file
        content = output.getvalue()
        Path(filepath).write_text(content)

        logger.info("Performance exported to CSV", filepath=filepath)

        return filepath


# Global instance
_analyzer: Optional[PerformanceAnalyzer] = None


def get_performance_analyzer() -> PerformanceAnalyzer:
    """Get or create the global performance analyzer instance."""
    global _analyzer
    if _analyzer is None:
        _analyzer = PerformanceAnalyzer()
    return _analyzer


def calculate_performance(days: int = 30) -> PerformanceMetrics:
    """Convenience function to calculate performance metrics."""
    return get_performance_analyzer().calculate_metrics(days=days)


def export_performance_csv(filepath: str, days: int = 30) -> str:
    """Convenience function to export performance to CSV."""
    return get_performance_analyzer().export_to_csv(filepath, days=days)
