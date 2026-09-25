"""
Unit tests for order execution engine.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.execution.executor import (
    ExecutionResult,
    Order,
    OrderAction,
    OrderExecutor,
    OrderSide,
    OrderStatus,
    execute_order,
    get_executor,
)


class TestOrder:
    """Tests for Order dataclass."""

    def test_order_creation(self):
        """Test Order creation."""
        order = Order(
            order_id="ORD-123",
            ticker="BTC-100K",
            side=OrderSide.YES,
            action=OrderAction.BUY,
            quantity=10,
            price=50,
        )

        assert order.order_id == "ORD-123"
        assert order.ticker == "BTC-100K"
        assert order.side == OrderSide.YES
        assert order.action == OrderAction.BUY
        assert order.quantity == 10
        assert order.price == 50
        assert order.status == OrderStatus.PENDING

    def test_order_is_complete(self):
        """Test is_complete property."""
        order = Order(
            order_id="ORD-123",
            ticker="TEST",
            side=OrderSide.YES,
            action=OrderAction.BUY,
            quantity=10,
            price=50,
        )

        assert order.is_complete is False

        order.status = OrderStatus.FILLED
        assert order.is_complete is True

        order.status = OrderStatus.CANCELLED
        assert order.is_complete is True

        order.status = OrderStatus.SUBMITTED
        assert order.is_complete is False

    def test_order_remaining_quantity(self):
        """Test remaining_quantity property."""
        order = Order(
            order_id="ORD-123",
            ticker="TEST",
            side=OrderSide.YES,
            action=OrderAction.BUY,
            quantity=10,
            price=50,
            filled_quantity=3,
        )

        assert order.remaining_quantity == 7

    def test_order_notional_value(self):
        """Test notional_value property."""
        order = Order(
            order_id="ORD-123",
            ticker="TEST",
            side=OrderSide.YES,
            action=OrderAction.BUY,
            quantity=10,
            price=50,
        )

        assert order.notional_value == 5.0  # 10 * 50 / 100


class TestOrderExecutor:
    """Tests for OrderExecutor."""

    def test_initialization(self):
        """Test executor initialization."""
        executor = OrderExecutor(paper_trading=True, default_timeout=300)

        assert executor.paper_trading is True
        assert executor.default_timeout == 300

    def test_create_order(self):
        """Test order creation."""
        executor = OrderExecutor(paper_trading=True)

        order = executor.create_order(
            ticker="TEST",
            side="yes",
            action="buy",
            quantity=10,
            price=50,
        )

        assert order.ticker == "TEST"
        assert order.side == OrderSide.YES
        assert order.action == OrderAction.BUY
        assert order.quantity == 10
        assert order.price == 50
        assert order.order_id.startswith("ORD-")

    def test_validate_order_valid(self):
        """Test order validation with valid params."""
        executor = OrderExecutor(paper_trading=True)

        order = Order(
            order_id="ORD-123",
            ticker="TEST",
            side=OrderSide.YES,
            action=OrderAction.BUY,
            quantity=10,
            price=50,
        )

        # Mock market exists
        with patch("src.execution.executor.get_db_session") as mock_session_gen:
            mock_session = MagicMock()
            mock_context = MagicMock()
            mock_context.__enter__ = MagicMock(return_value=mock_session)
            mock_context.__exit__ = MagicMock(return_value=False)

            mock_market = MagicMock()
            mock_market.status = "active"

            mock_query = MagicMock()
            mock_query.filter_by.return_value.first.return_value = mock_market
            mock_session.query.return_value = mock_query

            mock_session_gen.return_value = iter([mock_context])

            is_valid, error = executor._validate_order(order)
            assert is_valid is True
            assert error == ""

    def test_validate_order_quantity_too_low(self):
        """Test order validation with quantity too low."""
        executor = OrderExecutor(paper_trading=True)

        order = Order(
            order_id="ORD-123",
            ticker="TEST",
            side=OrderSide.YES,
            action=OrderAction.BUY,
            quantity=0,
            price=50,
        )

        is_valid, error = executor._validate_order(order)
        assert is_valid is False
        assert "below minimum" in error

    def test_validate_order_price_out_of_range(self):
        """Test order validation with price out of range."""
        executor = OrderExecutor(paper_trading=True)

        order = Order(
            order_id="ORD-123",
            ticker="TEST",
            side=OrderSide.YES,
            action=OrderAction.BUY,
            quantity=10,
            price=100,  # Max is 99
        )

        is_valid, error = executor._validate_order(order)
        assert is_valid is False
        assert "above maximum" in error

    def test_submit_order_paper_trading(self):
        """Test order submission in paper trading mode."""
        executor = OrderExecutor(paper_trading=True)

        order = executor.create_order(
            ticker="TEST",
            side="yes",
            action="buy",
            quantity=10,
            price=50,
        )

        # Mock validation and risk check
        with patch.object(executor, "_validate_order", return_value=(True, "")):
            with patch.object(executor, "_check_risk") as mock_risk:
                mock_risk_result = MagicMock()
                mock_risk_result.approved = True
                mock_risk_result.violations = []
                mock_risk_result.messages = []
                mock_risk.return_value = mock_risk_result

                with patch.object(executor, "_store_trade"):
                    with patch.object(executor, "_update_position"):
                        result = executor.submit_order(order)

                        assert result.success is True
                        assert order.status == OrderStatus.FILLED
                        assert order.filled_quantity == 10

    def test_submit_order_validation_failed(self):
        """Test order submission with validation failure."""
        executor = OrderExecutor(paper_trading=True)

        order = executor.create_order(
            ticker="TEST",
            side="yes",
            action="buy",
            quantity=10,
            price=50,
        )

        with patch.object(executor, "_validate_order", return_value=(False, "Market not found")):
            result = executor.submit_order(order)

            assert result.success is False
            assert order.status == OrderStatus.REJECTED
            assert "Validation failed" in result.message

    def test_submit_order_risk_rejected(self):
        """Test order submission with risk rejection."""
        executor = OrderExecutor(paper_trading=True)

        order = executor.create_order(
            ticker="TEST",
            side="yes",
            action="buy",
            quantity=10,
            price=50,
        )

        with patch.object(executor, "_validate_order", return_value=(True, "")):
            with patch.object(executor, "_check_risk") as mock_risk:
                mock_risk_result = MagicMock()
                mock_risk_result.approved = False
                mock_risk_result.violations = [MagicMock(value="daily_loss_limit")]
                mock_risk_result.messages = ["Daily loss limit exceeded"]
                mock_risk.return_value = mock_risk_result

                result = executor.submit_order(order)

                assert result.success is False
                assert order.status == OrderStatus.REJECTED
                assert "Risk check failed" in result.message

    def test_cancel_order(self):
        """Test order cancellation."""
        executor = OrderExecutor(paper_trading=True)

        order = executor.create_order(
            ticker="TEST",
            side="yes",
            action="buy",
            quantity=10,
            price=50,
        )
        order.status = OrderStatus.SUBMITTED
        executor._pending_orders.append(order.order_id)

        result = executor.cancel_order(order.order_id)

        assert result.success is True
        assert order.status == OrderStatus.CANCELLED
        assert order.order_id not in executor._pending_orders

    def test_cancel_order_not_found(self):
        """Test cancellation of non-existent order."""
        executor = OrderExecutor(paper_trading=True)

        result = executor.cancel_order("NONEXISTENT")

        assert result.success is False
        assert "not found" in result.message

    def test_cancel_order_already_complete(self):
        """Test cancellation of completed order."""
        executor = OrderExecutor(paper_trading=True)

        order = executor.create_order(
            ticker="TEST",
            side="yes",
            action="buy",
            quantity=10,
            price=50,
        )
        order.status = OrderStatus.FILLED

        result = executor.cancel_order(order.order_id)

        assert result.success is False
        assert "Cannot cancel" in result.message

    def test_check_expired_orders(self):
        """Test expired order detection."""
        executor = OrderExecutor(paper_trading=True)

        order = executor.create_order(
            ticker="TEST",
            side="yes",
            action="buy",
            quantity=10,
            price=50,
            timeout_seconds=0,  # Immediate expiry
        )
        order.status = OrderStatus.SUBMITTED
        order.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        executor._pending_orders.append(order.order_id)

        expired = executor.check_expired_orders()

        assert len(expired) == 1
        assert expired[0].status == OrderStatus.EXPIRED

    def test_get_order(self):
        """Test getting order by ID."""
        executor = OrderExecutor(paper_trading=True)

        order = executor.create_order(
            ticker="TEST",
            side="yes",
            action="buy",
            quantity=10,
            price=50,
        )

        retrieved = executor.get_order(order.order_id)
        assert retrieved is order

        not_found = executor.get_order("NONEXISTENT")
        assert not_found is None

    def test_get_pending_orders(self):
        """Test getting pending orders."""
        executor = OrderExecutor(paper_trading=True)

        order1 = executor.create_order("TEST1", "yes", "buy", 10, 50)
        order2 = executor.create_order("TEST2", "no", "buy", 5, 60)

        order1.status = OrderStatus.SUBMITTED
        order2.status = OrderStatus.SUBMITTED
        executor._pending_orders = [order1.order_id, order2.order_id]

        pending = executor.get_pending_orders()
        assert len(pending) == 2

    def test_execute_convenience(self):
        """Test execute convenience method."""
        executor = OrderExecutor(paper_trading=True)

        with patch.object(executor, "_validate_order", return_value=(True, "")):
            with patch.object(executor, "_check_risk") as mock_risk:
                mock_risk_result = MagicMock()
                mock_risk_result.approved = True
                mock_risk_result.violations = []
                mock_risk_result.messages = []
                mock_risk.return_value = mock_risk_result

                with patch.object(executor, "_store_trade"):
                    with patch.object(executor, "_update_position"):
                        result = executor.execute(
                            ticker="TEST",
                            side="yes",
                            action="buy",
                            quantity=10,
                            price=50,
                        )

                        assert result.success is True


class TestConvenienceFunctions:
    """Tests for module-level convenience functions."""

    def test_get_executor_singleton(self):
        """Test get_executor returns singleton."""
        import src.execution.executor as executor_module

        executor_module._executor = None

        exec1 = get_executor(paper_trading=True)
        exec2 = get_executor()

        assert exec1 is exec2

    def test_execute_order_convenience(self):
        """Test execute_order convenience function."""
        import src.execution.executor as executor_module

        executor_module._executor = None

        with patch.object(OrderExecutor, "_validate_order", return_value=(True, "")):
            with patch.object(OrderExecutor, "_check_risk") as mock_risk:
                mock_risk_result = MagicMock()
                mock_risk_result.approved = True
                mock_risk_result.violations = []
                mock_risk_result.messages = []
                mock_risk.return_value = mock_risk_result

                with patch.object(OrderExecutor, "_store_trade"):
                    with patch.object(OrderExecutor, "_update_position"):
                        result = execute_order("TEST", "yes", "buy", 10, 50)
                        assert isinstance(result, ExecutionResult)


class TestOrderEnums:
    """Tests for order enums."""

    def test_order_status_values(self):
        """Test OrderStatus enum values."""
        assert OrderStatus.PENDING.value == "pending"
        assert OrderStatus.FILLED.value == "filled"
        assert OrderStatus.CANCELLED.value == "cancelled"

    def test_order_side_values(self):
        """Test OrderSide enum values."""
        assert OrderSide.YES.value == "yes"
        assert OrderSide.NO.value == "no"

    def test_order_action_values(self):
        """Test OrderAction enum values."""
        assert OrderAction.BUY.value == "buy"
        assert OrderAction.SELL.value == "sell"
