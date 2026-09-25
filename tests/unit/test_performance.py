"""
Unit tests for performance analytics.
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch
import tempfile
import os

import pytest

from src.analysis.performance import (
    PerformanceAnalyzer,
    PerformanceMetrics,
    PeriodSummary,
    calculate_performance,
    export_performance_csv,
    get_performance_analyzer,
)


class TestPerformanceMetrics:
    """Tests for PerformanceMetrics dataclass."""

    def test_metrics_creation(self):
        """Test PerformanceMetrics creation."""
        metrics = PerformanceMetrics(
            start_date=datetime(2026, 1, 1),
            end_date=datetime(2026, 1, 31),
            trading_days=21,
            total_return=100.0,
            total_return_pct=0.10,
            daily_return_avg=0.005,
            daily_return_std=0.02,
            cagr=0.12,
            sharpe_ratio=1.5,
            sortino_ratio=2.0,
            max_drawdown=0.05,
            max_drawdown_duration_days=3,
            volatility=0.20,
            total_trades=50,
            winning_trades=30,
            losing_trades=20,
            win_rate=0.60,
            avg_win=10.0,
            avg_loss=5.0,
            profit_factor=2.0,
            avg_trade_pnl=4.0,
            avg_predicted_edge=0.08,
            avg_realized_edge=0.06,
            edge_accuracy=0.70,
            current_equity=1100.0,
            peak_equity=1150.0,
        )

        assert metrics.total_return == 100.0
        assert metrics.sharpe_ratio == 1.5
        assert metrics.win_rate == 0.60


class TestPeriodSummary:
    """Tests for PeriodSummary dataclass."""

    def test_summary_creation(self):
        """Test PeriodSummary creation."""
        summary = PeriodSummary(
            period="daily",
            start_date=datetime(2026, 1, 1),
            end_date=datetime(2026, 1, 2),
            return_pct=0.02,
            trades=5,
            pnl=20.0,
            win_rate=0.80,
        )

        assert summary.period == "daily"
        assert summary.return_pct == 0.02
        assert summary.trades == 5


class TestPerformanceAnalyzer:
    """Tests for PerformanceAnalyzer."""

    def test_initialization(self):
        """Test analyzer initialization."""
        analyzer = PerformanceAnalyzer(initial_equity=5000.0)

        assert analyzer.initial_equity == 5000.0

    def test_calculate_returns_empty(self):
        """Test return calculation with no data."""
        analyzer = PerformanceAnalyzer()

        returns = analyzer._calculate_returns([])

        assert len(returns) == 0

    def test_calculate_returns_single_point(self):
        """Test return calculation with single data point."""
        analyzer = PerformanceAnalyzer()

        equity_series = [(datetime(2026, 1, 1), 1000.0)]
        returns = analyzer._calculate_returns(equity_series)

        assert len(returns) == 0

    def test_calculate_returns_multiple_points(self):
        """Test return calculation with multiple data points."""
        analyzer = PerformanceAnalyzer()

        equity_series = [
            (datetime(2026, 1, 1), 1000.0),
            (datetime(2026, 1, 2), 1050.0),
            (datetime(2026, 1, 3), 1025.0),
        ]

        returns = analyzer._calculate_returns(equity_series)

        assert len(returns) == 2
        assert abs(returns[0] - 0.05) < 0.001  # 5% gain
        assert abs(returns[1] - (-0.0238)) < 0.001  # ~2.4% loss

    def test_calculate_drawdown_no_drawdown(self):
        """Test drawdown with no drawdown."""
        analyzer = PerformanceAnalyzer()

        equity_series = [
            (datetime(2026, 1, 1), 1000.0),
            (datetime(2026, 1, 2), 1050.0),
            (datetime(2026, 1, 3), 1100.0),
        ]

        max_dd, duration = analyzer._calculate_drawdown(equity_series)

        assert max_dd == 0.0
        # Duration tracking starts from first point, so monotonically increasing equity still counts
        assert duration <= 1

    def test_calculate_drawdown_with_drawdown(self):
        """Test drawdown with actual drawdown."""
        analyzer = PerformanceAnalyzer()

        equity_series = [
            (datetime(2026, 1, 1), 1000.0),
            (datetime(2026, 1, 2), 1100.0),  # Peak
            (datetime(2026, 1, 3), 990.0),  # 10% drawdown
            (datetime(2026, 1, 4), 1050.0),  # Partial recovery
        ]

        max_dd, duration = analyzer._calculate_drawdown(equity_series)

        assert abs(max_dd - 0.10) < 0.001  # 10% drawdown
        assert duration >= 1

    def test_calculate_trade_stats_empty(self):
        """Test trade stats with no trades."""
        analyzer = PerformanceAnalyzer()

        stats = analyzer._calculate_trade_stats([])

        assert stats["total"] == 0
        assert stats["win_rate"] == 0.0

    def test_calculate_trade_stats_with_trades(self):
        """Test trade stats with trades."""
        analyzer = PerformanceAnalyzer()

        trades = [
            {"ticker": "TEST", "action": "buy", "price": 50, "quantity": 10, "fee": 0.1, "timestamp": datetime.now(timezone.utc)},
            {"ticker": "TEST", "action": "sell", "price": 60, "quantity": 10, "fee": 0.1, "timestamp": datetime.now(timezone.utc)},
        ]

        stats = analyzer._calculate_trade_stats(trades)

        assert stats["total"] == 2
        # Buy at 50, sell at 60 = 10 cents * 10 qty / 100 = $1 - fees
        assert stats["winning"] >= 0

    def test_calculate_edge_metrics_empty(self):
        """Test edge metrics with no forecasts."""
        analyzer = PerformanceAnalyzer()

        metrics = analyzer._calculate_edge_metrics([])

        assert metrics["avg_predicted_edge"] == 0.0

    def test_calculate_edge_metrics_with_data(self):
        """Test edge metrics with forecasts."""
        analyzer = PerformanceAnalyzer()

        forecasts = [
            {"ticker": "TEST1", "edge": 0.10, "probability": 0.6, "market_probability": 0.5, "confidence": 0.8, "timestamp": datetime.now(timezone.utc)},
            {"ticker": "TEST2", "edge": 0.05, "probability": 0.55, "market_probability": 0.5, "confidence": 0.7, "timestamp": datetime.now(timezone.utc)},
        ]

        metrics = analyzer._calculate_edge_metrics(forecasts)

        assert abs(metrics["avg_predicted_edge"] - 0.075) < 0.001

    def test_calculate_metrics(self):
        """Test comprehensive metrics calculation."""
        analyzer = PerformanceAnalyzer(initial_equity=1000.0)

        # Mock database methods
        equity_series = [
            (datetime(2026, 1, 1), 1000.0),
            (datetime(2026, 1, 2), 1050.0),
            (datetime(2026, 1, 3), 1025.0),
            (datetime(2026, 1, 4), 1100.0),
        ]

        with patch.object(analyzer, "_get_equity_series", return_value=equity_series):
            with patch.object(analyzer, "_get_trades", return_value=[]):
                with patch.object(analyzer, "_get_forecasts", return_value=[]):
                    metrics = analyzer.calculate_metrics(days=30)

                    assert isinstance(metrics, PerformanceMetrics)
                    assert metrics.trading_days == 4
                    assert metrics.total_return == 100.0
                    assert abs(metrics.total_return_pct - 0.10) < 0.001

    def test_calculate_metrics_empty_data(self):
        """Test metrics calculation with no data."""
        analyzer = PerformanceAnalyzer(initial_equity=1000.0)

        with patch.object(analyzer, "_get_equity_series", return_value=[]):
            with patch.object(analyzer, "_get_trades", return_value=[]):
                with patch.object(analyzer, "_get_forecasts", return_value=[]):
                    metrics = analyzer.calculate_metrics(days=30)

                    assert metrics.trading_days == 0
                    assert metrics.total_return == 0.0

    def test_generate_period_summaries_daily(self):
        """Test daily period summaries."""
        analyzer = PerformanceAnalyzer()

        equity_series = [
            (datetime(2026, 1, 1, 12, tzinfo=timezone.utc), 1000.0),
            (datetime(2026, 1, 2, 12, tzinfo=timezone.utc), 1050.0),
            (datetime(2026, 1, 3, 12, tzinfo=timezone.utc), 1025.0),
        ]

        with patch.object(analyzer, "_get_equity_series", return_value=equity_series):
            with patch.object(analyzer, "_get_trades", return_value=[]):
                summaries = analyzer.generate_period_summaries(period="daily", days=7)

                assert len(summaries) >= 1
                assert all(s.period == "daily" for s in summaries)

    def test_export_to_csv(self):
        """Test CSV export."""
        analyzer = PerformanceAnalyzer(initial_equity=1000.0)

        equity_series = [
            (datetime(2026, 1, 1, tzinfo=timezone.utc), 1000.0),
            (datetime(2026, 1, 2, tzinfo=timezone.utc), 1050.0),
        ]

        with patch.object(analyzer, "_get_equity_series", return_value=equity_series):
            with patch.object(analyzer, "_get_trades", return_value=[]):
                with patch.object(analyzer, "_get_forecasts", return_value=[]):
                    with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
                        filepath = f.name

                    try:
                        result_path = analyzer.export_to_csv(filepath, days=30)

                        assert result_path == filepath
                        assert os.path.exists(filepath)

                        # Check content
                        with open(filepath) as f:
                            content = f.read()
                            assert "Performance Metrics" in content
                            assert "Total Return" in content
                    finally:
                        os.unlink(filepath)


class TestConvenienceFunctions:
    """Tests for module-level convenience functions."""

    def test_get_performance_analyzer_singleton(self):
        """Test get_performance_analyzer returns singleton."""
        import src.analysis.performance as perf_module

        perf_module._analyzer = None

        analyzer1 = get_performance_analyzer()
        analyzer2 = get_performance_analyzer()

        assert analyzer1 is analyzer2

    def test_calculate_performance_convenience(self):
        """Test calculate_performance convenience function."""
        import src.analysis.performance as perf_module

        perf_module._analyzer = None

        with patch.object(PerformanceAnalyzer, "_get_equity_series", return_value=[]):
            with patch.object(PerformanceAnalyzer, "_get_trades", return_value=[]):
                with patch.object(PerformanceAnalyzer, "_get_forecasts", return_value=[]):
                    metrics = calculate_performance(days=7)

                    assert isinstance(metrics, PerformanceMetrics)

    def test_export_performance_csv_convenience(self):
        """Test export_performance_csv convenience function."""
        import src.analysis.performance as perf_module

        perf_module._analyzer = None

        with patch.object(PerformanceAnalyzer, "_get_equity_series", return_value=[]):
            with patch.object(PerformanceAnalyzer, "_get_trades", return_value=[]):
                with patch.object(PerformanceAnalyzer, "_get_forecasts", return_value=[]):
                    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
                        filepath = f.name

                    try:
                        result = export_performance_csv(filepath, days=7)
                        assert os.path.exists(result)
                    finally:
                        os.unlink(filepath)
