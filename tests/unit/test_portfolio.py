"""
Unit tests for portfolio tracker.
"""
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from src.execution.portfolio import (
    PortfolioSummary,
    PortfolioTracker,
    Position,
    get_portfolio_summary,
    get_portfolio_tracker,
    get_positions,
)


class TestPosition:
    """Tests for Position dataclass."""

    def test_position_creation(self):
        """Test Position creation."""
        position = Position(
            ticker="BTC-100K",
            side="yes",
            quantity=10,
            average_price=50,
            market_price=60,
            unrealized_pnl=1.0,
            unrealized_pnl_pct=0.20,
            realized_pnl=0.0,
        )

        assert position.ticker == "BTC-100K"
        assert position.side == "yes"
        assert position.quantity == 10

    def test_position_cost_basis(self):
        """Test cost_basis property."""
        position = Position(
            ticker="TEST",
            side="yes",
            quantity=10,
            average_price=50,
            market_price=60,
            unrealized_pnl=1.0,
            unrealized_pnl_pct=0.20,
            realized_pnl=0.0,
        )

        assert position.cost_basis == 5.0  # 10 * 50 / 100

    def test_position_market_value(self):
        """Test market_value property."""
        position = Position(
            ticker="TEST",
            side="yes",
            quantity=10,
            average_price=50,
            market_price=60,
            unrealized_pnl=1.0,
            unrealized_pnl_pct=0.20,
            realized_pnl=0.0,
        )

        assert position.market_value == 6.0  # 10 * 60 / 100

    def test_position_total_pnl(self):
        """Test total_pnl property."""
        position = Position(
            ticker="TEST",
            side="yes",
            quantity=10,
            average_price=50,
            market_price=60,
            unrealized_pnl=1.0,
            unrealized_pnl_pct=0.20,
            realized_pnl=2.0,
        )

        assert position.total_pnl == 3.0


class TestPortfolioTracker:
    """Tests for PortfolioTracker."""

    def test_initialization(self):
        """Test tracker initialization."""
        tracker = PortfolioTracker(initial_balance=5000.0)

        assert tracker.initial_balance == 5000.0

    def test_get_positions_empty(self):
        """Test getting positions when none exist."""
        tracker = PortfolioTracker()

        with patch("src.execution.portfolio.get_db_session") as mock_session_gen:
            mock_session = MagicMock()
            mock_context = MagicMock()
            mock_context.__enter__ = MagicMock(return_value=mock_session)
            mock_context.__exit__ = MagicMock(return_value=False)

            mock_session.query.return_value.all.return_value = []

            mock_session_gen.return_value = iter([mock_context])

            positions = tracker.get_positions(force_refresh=True)
            assert len(positions) == 0

    def test_get_positions_with_data(self):
        """Test getting positions with data."""
        tracker = PortfolioTracker()

        with patch("src.execution.portfolio.get_db_session") as mock_session_gen:
            mock_session = MagicMock()
            mock_context = MagicMock()
            mock_context.__enter__ = MagicMock(return_value=mock_session)
            mock_context.__exit__ = MagicMock(return_value=False)

            # Mock position
            mock_pos = MagicMock()
            mock_pos.ticker = "TEST"
            mock_pos.side = "yes"
            mock_pos.quantity = 10
            mock_pos.average_price = 50
            mock_pos.realized_pnl = 0

            mock_session.query.return_value.all.return_value = [mock_pos]

            mock_session_gen.return_value = iter([mock_context])

            with patch.object(tracker, "_get_current_price", return_value=55):
                with patch.object(tracker, "_get_market_info", return_value={"title": "Test Market", "status": "active"}):
                    positions = tracker.get_positions(force_refresh=True)

                    assert len(positions) == 1
                    assert positions[0].ticker == "TEST"
                    assert positions[0].market_price == 55

    def test_get_positions_caching(self):
        """Test position caching."""
        tracker = PortfolioTracker()

        # First call
        with patch("src.execution.portfolio.get_db_session") as mock_session_gen:
            mock_session = MagicMock()
            mock_context = MagicMock()
            mock_context.__enter__ = MagicMock(return_value=mock_session)
            mock_context.__exit__ = MagicMock(return_value=False)
            mock_session.query.return_value.all.return_value = []
            mock_session_gen.return_value = iter([mock_context])

            positions1 = tracker.get_positions(force_refresh=True)

        # Second call should use cache
        positions2 = tracker.get_positions(force_refresh=False)

        assert positions1 == positions2

    def test_get_cash_balance_from_snapshot(self):
        """Test getting cash balance from snapshot."""
        tracker = PortfolioTracker()

        with patch("src.execution.portfolio.get_db_session") as mock_session_gen:
            mock_session = MagicMock()
            mock_context = MagicMock()
            mock_context.__enter__ = MagicMock(return_value=mock_session)
            mock_context.__exit__ = MagicMock(return_value=False)

            mock_snapshot = MagicMock()
            mock_snapshot.balance = Decimal("2500.00")

            mock_query = MagicMock()
            mock_query.order_by.return_value.first.return_value = mock_snapshot
            mock_session.query.return_value = mock_query

            mock_session_gen.return_value = iter([mock_context])

            balance = tracker.get_cash_balance()
            assert balance == 2500.0

    def test_get_cash_balance_default(self):
        """Test getting cash balance with no snapshot."""
        tracker = PortfolioTracker(initial_balance=1000.0)

        with patch("src.execution.portfolio.get_db_session") as mock_session_gen:
            mock_session = MagicMock()
            mock_context = MagicMock()
            mock_context.__enter__ = MagicMock(return_value=mock_session)
            mock_context.__exit__ = MagicMock(return_value=False)

            mock_query = MagicMock()
            mock_query.order_by.return_value.first.return_value = None
            mock_session.query.return_value = mock_query

            mock_session_gen.return_value = iter([mock_context])

            balance = tracker.get_cash_balance()
            assert balance == 1000.0

    def test_get_summary(self):
        """Test getting portfolio summary."""
        tracker = PortfolioTracker(initial_balance=1000.0)

        mock_position = Position(
            ticker="TEST",
            side="yes",
            quantity=10,
            average_price=50,
            market_price=60,
            unrealized_pnl=1.0,
            unrealized_pnl_pct=0.20,
            realized_pnl=0.5,
        )

        with patch.object(tracker, "get_positions", return_value=[mock_position]):
            with patch.object(tracker, "get_cash_balance", return_value=994.0):
                with patch.object(tracker, "_calculate_daily_return", return_value=0.01):
                    summary = tracker.get_summary()

                    assert summary.cash_balance == 994.0
                    assert summary.total_position_value == 6.0  # 10 * 60 / 100
                    assert summary.total_equity == 1000.0  # 994 + 6
                    assert summary.position_count == 1

    def test_save_snapshot(self):
        """Test saving portfolio snapshot."""
        tracker = PortfolioTracker()

        mock_summary = PortfolioSummary(
            cash_balance=1000.0,
            total_position_value=100.0,
            total_equity=1100.0,
            total_unrealized_pnl=50.0,
            total_realized_pnl=25.0,
            total_pnl=75.0,
            daily_return=0.02,
            total_return=0.10,
            positions=[],
            position_count=0,
            exposure_pct=0.09,
            largest_position_pct=0.05,
        )

        with patch.object(tracker, "get_summary", return_value=mock_summary):
            with patch("src.execution.portfolio.get_db_session") as mock_session_gen:
                mock_session = MagicMock()
                mock_context = MagicMock()
                mock_context.__enter__ = MagicMock(return_value=mock_session)
                mock_context.__exit__ = MagicMock(return_value=False)

                mock_session_gen.return_value = iter([mock_context])

                snapshot = tracker.save_snapshot()

                mock_session.add.assert_called_once()
                mock_session.commit.assert_called_once()

    def test_get_equity_curve(self):
        """Test getting equity curve."""
        tracker = PortfolioTracker()

        with patch("src.execution.portfolio.get_db_session") as mock_session_gen:
            mock_session = MagicMock()
            mock_context = MagicMock()
            mock_context.__enter__ = MagicMock(return_value=mock_session)
            mock_context.__exit__ = MagicMock(return_value=False)

            mock_snapshot1 = MagicMock()
            mock_snapshot1.timestamp = datetime(2026, 1, 26)
            mock_snapshot1.total_equity = Decimal("1000.00")
            mock_snapshot1.daily_return = 0.0

            mock_snapshot2 = MagicMock()
            mock_snapshot2.timestamp = datetime(2026, 1, 27)
            mock_snapshot2.total_equity = Decimal("1050.00")
            mock_snapshot2.daily_return = 0.05

            mock_query = MagicMock()
            mock_query.filter.return_value.order_by.return_value.all.return_value = [
                mock_snapshot1,
                mock_snapshot2,
            ]
            mock_session.query.return_value = mock_query

            mock_session_gen.return_value = iter([mock_context])

            curve = tracker.get_equity_curve(days=30)

            assert len(curve) == 2
            assert curve[0]["equity"] == 1000.0
            assert curve[1]["equity"] == 1050.0

    def test_get_trade_history(self):
        """Test getting trade history."""
        tracker = PortfolioTracker()

        with patch("src.execution.portfolio.get_db_session") as mock_session_gen:
            mock_session = MagicMock()
            mock_context = MagicMock()
            mock_context.__enter__ = MagicMock(return_value=mock_session)
            mock_context.__exit__ = MagicMock(return_value=False)

            mock_trade = MagicMock()
            mock_trade.order_id = "ORD-123"
            mock_trade.ticker = "TEST"
            mock_trade.side = "yes"
            mock_trade.action = "buy"
            mock_trade.quantity = 10
            mock_trade.price = 50
            mock_trade.fee = Decimal("0.10")
            mock_trade.status = "filled"
            mock_trade.timestamp = datetime(2026, 1, 27)

            mock_query = MagicMock()
            mock_query.filter.return_value.order_by.return_value.limit.return_value.all.return_value = [mock_trade]
            mock_session.query.return_value = mock_query

            mock_session_gen.return_value = iter([mock_context])

            trades = tracker.get_trade_history(days=30)

            assert len(trades) == 1
            assert trades[0]["order_id"] == "ORD-123"

    def test_calculate_performance_metrics(self):
        """Test calculating performance metrics."""
        tracker = PortfolioTracker()

        equity_curve = [
            {"timestamp": datetime(2026, 1, 1), "equity": 1000.0, "daily_return": 0},
            {"timestamp": datetime(2026, 1, 2), "equity": 1050.0, "daily_return": 0.05},
            {"timestamp": datetime(2026, 1, 3), "equity": 1025.0, "daily_return": -0.024},
            {"timestamp": datetime(2026, 1, 4), "equity": 1100.0, "daily_return": 0.073},
        ]

        with patch.object(tracker, "get_equity_curve", return_value=equity_curve):
            with patch.object(tracker, "get_trade_history", return_value=[]):
                metrics = tracker.calculate_performance_metrics(days=30)

                assert metrics["total_return"] == 0.1  # 10% return
                assert len(metrics["daily_returns"]) == 3
                assert metrics["max_drawdown"] > 0  # Should detect drawdown


class TestConvenienceFunctions:
    """Tests for module-level convenience functions."""

    def test_get_portfolio_tracker_singleton(self):
        """Test get_portfolio_tracker returns singleton."""
        import src.execution.portfolio as portfolio_module

        portfolio_module._tracker = None

        tracker1 = get_portfolio_tracker()
        tracker2 = get_portfolio_tracker()

        assert tracker1 is tracker2

    def test_get_portfolio_summary_convenience(self):
        """Test get_portfolio_summary convenience function."""
        import src.execution.portfolio as portfolio_module

        portfolio_module._tracker = None

        with patch.object(PortfolioTracker, "get_summary") as mock_summary:
            mock_summary.return_value = PortfolioSummary(
                cash_balance=1000.0,
                total_position_value=0.0,
                total_equity=1000.0,
                total_unrealized_pnl=0.0,
                total_realized_pnl=0.0,
                total_pnl=0.0,
                daily_return=0.0,
                total_return=0.0,
                positions=[],
                position_count=0,
                exposure_pct=0.0,
                largest_position_pct=0.0,
            )

            summary = get_portfolio_summary()
            assert isinstance(summary, PortfolioSummary)

    def test_get_positions_convenience(self):
        """Test get_positions convenience function."""
        import src.execution.portfolio as portfolio_module

        portfolio_module._tracker = None

        with patch.object(PortfolioTracker, "get_positions", return_value=[]):
            positions = get_positions()
            assert isinstance(positions, list)
