"""
Unit tests for risk management module.
"""
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.execution.risk_manager import (
    RiskCheckResult,
    RiskEvent,
    RiskManager,
    RiskStatus,
    RiskViolation,
    check_trade_risk,
    get_risk_manager,
    is_trading_allowed,
)


class TestRiskCheckResult:
    """Tests for RiskCheckResult dataclass."""

    def test_result_creation_approved(self):
        """Test creating an approved result."""
        result = RiskCheckResult(
            status=RiskStatus.APPROVED,
            approved=True,
            violations=[],
            messages=[],
            daily_pnl=50.0,
            daily_loss_limit=100.0,
            current_exposure=200.0,
            max_exposure=500.0,
            position_value=50.0,
            max_position_value=100.0,
            current_drawdown=0.05,
            max_drawdown=0.15,
        )

        assert result.status == RiskStatus.APPROVED
        assert result.approved is True
        assert len(result.violations) == 0

    def test_result_creation_rejected(self):
        """Test creating a rejected result."""
        result = RiskCheckResult(
            status=RiskStatus.REJECTED,
            approved=False,
            violations=[RiskViolation.DAILY_LOSS_LIMIT],
            messages=["Daily loss limit exceeded"],
            daily_pnl=-60.0,
            daily_loss_limit=50.0,
            current_exposure=200.0,
            max_exposure=500.0,
            position_value=50.0,
            max_position_value=100.0,
            current_drawdown=0.05,
            max_drawdown=0.15,
            ticker="TEST",
            requested_amount=50.0,
        )

        assert result.status == RiskStatus.REJECTED
        assert result.approved is False
        assert RiskViolation.DAILY_LOSS_LIMIT in result.violations


class TestRiskEvent:
    """Tests for RiskEvent dataclass."""

    def test_event_creation(self):
        """Test creating a risk event."""
        event = RiskEvent(
            event_type=RiskViolation.MAX_DRAWDOWN,
            severity="critical",
            message="Max drawdown exceeded",
            details={"drawdown": 0.16, "limit": 0.15},
        )

        assert event.event_type == RiskViolation.MAX_DRAWDOWN
        assert event.severity == "critical"
        assert event.details["drawdown"] == 0.16


class TestRiskManager:
    """Tests for RiskManager."""

    def test_initialization_defaults(self):
        """Test risk manager initialization with defaults."""
        rm = RiskManager()

        assert rm.daily_loss_limit_pct == 0.05
        assert rm.max_position_pct == 0.10
        assert rm.max_exposure_pct == 0.50
        assert rm.max_drawdown_pct == 0.15

    def test_initialization_custom(self):
        """Test risk manager initialization with custom params."""
        rm = RiskManager(
            daily_loss_limit_pct=0.03,
            max_position_pct=0.05,
            max_exposure_pct=0.40,
            max_drawdown_pct=0.10,
            initial_bankroll=5000.0,
        )

        assert rm.daily_loss_limit_pct == 0.03
        assert rm.max_position_pct == 0.05
        assert rm.max_exposure_pct == 0.40
        assert rm.max_drawdown_pct == 0.10
        assert rm.initial_bankroll == 5000.0

    def test_get_bankroll_override(self):
        """Test bankroll with override."""
        rm = RiskManager(initial_bankroll=2000.0)

        assert rm.get_bankroll() == 2000.0

    def test_get_bankroll_from_db(self):
        """Test bankroll from database."""
        rm = RiskManager()

        with patch("src.execution.risk_manager.get_db_session") as mock_session_gen:
            mock_session = MagicMock()
            mock_context = MagicMock()
            mock_context.__enter__ = MagicMock(return_value=mock_session)
            mock_context.__exit__ = MagicMock(return_value=False)

            mock_snapshot = MagicMock()
            mock_snapshot.balance = 3000.0

            mock_query = MagicMock()
            mock_query.order_by.return_value.first.return_value = mock_snapshot
            mock_session.query.return_value = mock_query

            mock_session_gen.return_value = iter([mock_context])

            assert rm.get_bankroll() == 3000.0

    def test_check_trade_approved(self):
        """Test trade check that passes all limits."""
        rm = RiskManager(initial_bankroll=1000.0)

        with patch.object(rm, "get_daily_pnl", return_value=0.0):
            with patch.object(rm, "get_total_exposure", return_value=0.0):
                with patch.object(rm, "get_position_exposure", return_value=0.0):
                    with patch.object(rm, "get_current_drawdown", return_value=0.0):
                        result = rm.check_trade(
                            ticker="TEST",
                            side="yes",
                            quantity=10,
                            price=50,  # $5 trade
                        )

                        assert result.approved is True
                        assert result.status == RiskStatus.APPROVED
                        assert len(result.violations) == 0

    def test_check_trade_daily_loss_exceeded(self):
        """Test trade rejected due to daily loss limit."""
        rm = RiskManager(
            initial_bankroll=1000.0,
            daily_loss_limit_pct=0.05,  # $50 limit
        )

        with patch.object(rm, "get_daily_pnl", return_value=-60.0):  # Lost $60
            with patch.object(rm, "get_total_exposure", return_value=0.0):
                with patch.object(rm, "get_position_exposure", return_value=0.0):
                    with patch.object(rm, "get_current_drawdown", return_value=0.0):
                        result = rm.check_trade(
                            ticker="TEST",
                            side="yes",
                            quantity=10,
                            price=50,
                        )

                        assert result.approved is False
                        assert RiskViolation.DAILY_LOSS_LIMIT in result.violations

    def test_check_trade_max_position_exceeded(self):
        """Test trade rejected due to max position size."""
        rm = RiskManager(
            initial_bankroll=1000.0,
            max_position_pct=0.10,  # $100 max per position
        )

        with patch.object(rm, "get_daily_pnl", return_value=0.0):
            with patch.object(rm, "get_total_exposure", return_value=0.0):
                # Already have $80 in this position
                with patch.object(rm, "get_position_exposure", return_value=80.0):
                    with patch.object(rm, "get_current_drawdown", return_value=0.0):
                        # Try to add $50 more (total would be $130)
                        result = rm.check_trade(
                            ticker="TEST",
                            side="yes",
                            quantity=100,
                            price=50,  # $50 trade
                        )

                        assert result.approved is False
                        assert RiskViolation.MAX_POSITION_SIZE in result.violations
                        # Should suggest allowed amount
                        assert result.allowed_amount == 20.0  # 100 - 80

    def test_check_trade_max_exposure_exceeded(self):
        """Test trade rejected due to max total exposure."""
        rm = RiskManager(
            initial_bankroll=1000.0,
            max_exposure_pct=0.50,  # $500 max total
        )

        with patch.object(rm, "get_daily_pnl", return_value=0.0):
            # Already at $480 exposure
            with patch.object(rm, "get_total_exposure", return_value=480.0):
                with patch.object(rm, "get_position_exposure", return_value=0.0):
                    with patch.object(rm, "get_current_drawdown", return_value=0.0):
                        # Try to add $50 more (would be $530)
                        result = rm.check_trade(
                            ticker="NEW",
                            side="yes",
                            quantity=100,
                            price=50,
                        )

                        assert result.approved is False
                        assert RiskViolation.MAX_TOTAL_EXPOSURE in result.violations
                        assert result.allowed_amount == 20.0  # 500 - 480

    def test_check_trade_max_drawdown_exceeded(self):
        """Test trade rejected due to max drawdown."""
        rm = RiskManager(
            initial_bankroll=1000.0,
            max_drawdown_pct=0.15,
        )

        with patch.object(rm, "get_daily_pnl", return_value=0.0):
            with patch.object(rm, "get_total_exposure", return_value=0.0):
                with patch.object(rm, "get_position_exposure", return_value=0.0):
                    # 16% drawdown (exceeds 15% limit)
                    with patch.object(rm, "get_current_drawdown", return_value=0.16):
                        result = rm.check_trade(
                            ticker="TEST",
                            side="yes",
                            quantity=10,
                            price=50,
                        )

                        assert result.approved is False
                        assert RiskViolation.MAX_DRAWDOWN in result.violations
                        # Should also pause trading
                        assert rm.is_trading_paused() is True

    def test_check_trade_multiple_violations(self):
        """Test trade with multiple violations."""
        rm = RiskManager(
            initial_bankroll=1000.0,
            daily_loss_limit_pct=0.05,
            max_exposure_pct=0.50,
        )

        with patch.object(rm, "get_daily_pnl", return_value=-60.0):
            with patch.object(rm, "get_total_exposure", return_value=490.0):
                with patch.object(rm, "get_position_exposure", return_value=0.0):
                    with patch.object(rm, "get_current_drawdown", return_value=0.0):
                        result = rm.check_trade(
                            ticker="TEST",
                            side="yes",
                            quantity=100,
                            price=50,
                        )

                        assert result.approved is False
                        assert len(result.violations) >= 2
                        assert RiskViolation.DAILY_LOSS_LIMIT in result.violations
                        assert RiskViolation.MAX_TOTAL_EXPOSURE in result.violations

    def test_check_trade_when_paused(self):
        """Test trade rejected when trading is paused."""
        rm = RiskManager(initial_bankroll=1000.0)
        rm.pause_trading("Manual pause")

        with patch.object(rm, "get_daily_pnl", return_value=0.0):
            with patch.object(rm, "get_total_exposure", return_value=0.0):
                with patch.object(rm, "get_position_exposure", return_value=0.0):
                    with patch.object(rm, "get_current_drawdown", return_value=0.0):
                        result = rm.check_trade(
                            ticker="TEST",
                            side="yes",
                            quantity=10,
                            price=50,
                        )

                        assert result.approved is False
                        assert RiskViolation.TRADING_PAUSED in result.violations

    def test_check_daily_limits_all_ok(self):
        """Test daily limits check when all OK."""
        rm = RiskManager(initial_bankroll=1000.0)

        with patch.object(rm, "get_daily_pnl", return_value=10.0):
            with patch.object(rm, "get_total_exposure", return_value=200.0):
                with patch.object(rm, "get_current_drawdown", return_value=0.05):
                    result = rm.check_daily_limits()

                    assert result.approved is True
                    assert len(result.violations) == 0

    def test_check_daily_limits_at_exposure_limit(self):
        """Test daily limits at max exposure."""
        rm = RiskManager(
            initial_bankroll=1000.0,
            max_exposure_pct=0.50,
        )

        with patch.object(rm, "get_daily_pnl", return_value=0.0):
            # Exactly at limit
            with patch.object(rm, "get_total_exposure", return_value=500.0):
                with patch.object(rm, "get_current_drawdown", return_value=0.0):
                    result = rm.check_daily_limits()

                    assert result.approved is False
                    assert RiskViolation.MAX_TOTAL_EXPOSURE in result.violations

    def test_pause_and_resume_trading(self):
        """Test pausing and resuming trading."""
        rm = RiskManager()

        assert rm.is_trading_paused() is False

        rm.pause_trading("Test pause")
        assert rm.is_trading_paused() is True
        assert rm._pause_reason == "Test pause"

        rm.resume_trading()
        assert rm.is_trading_paused() is False
        assert rm._pause_reason is None

    def test_get_risk_status(self):
        """Test getting risk status summary."""
        rm = RiskManager(initial_bankroll=1000.0)

        with patch.object(rm, "get_daily_pnl", return_value=-20.0):
            with patch.object(rm, "get_total_exposure", return_value=300.0):
                with patch.object(rm, "get_current_drawdown", return_value=0.08):
                    status = rm.get_risk_status()

                    assert status["bankroll"] == 1000.0
                    assert status["daily_pnl"] == -20.0
                    assert status["current_exposure"] == 300.0
                    assert status["current_drawdown"] == 0.08
                    assert status["trading_paused"] is False

    def test_get_risk_events(self):
        """Test getting risk events."""
        rm = RiskManager(initial_bankroll=1000.0)

        # Trigger a risk event
        with patch.object(rm, "get_daily_pnl", return_value=-60.0):
            with patch.object(rm, "get_total_exposure", return_value=0.0):
                with patch.object(rm, "get_position_exposure", return_value=0.0):
                    with patch.object(rm, "get_current_drawdown", return_value=0.0):
                        rm.check_trade("TEST", "yes", 10, 50)

        events = rm.get_risk_events()
        assert len(events) >= 1
        assert events[0].event_type == RiskViolation.DAILY_LOSS_LIMIT

    def test_get_risk_events_filtered(self):
        """Test getting filtered risk events."""
        rm = RiskManager(initial_bankroll=1000.0)

        # Trigger multiple events
        rm._log_risk_event(
            RiskViolation.DAILY_LOSS_LIMIT,
            "violation",
            "Test 1",
            {},
        )
        rm._log_risk_event(
            RiskViolation.MAX_POSITION_SIZE,
            "warning",
            "Test 2",
            {},
        )
        rm._log_risk_event(
            RiskViolation.DAILY_LOSS_LIMIT,
            "violation",
            "Test 3",
            {},
        )

        # Filter by type
        loss_events = rm.get_risk_events(event_type=RiskViolation.DAILY_LOSS_LIMIT)
        assert len(loss_events) == 2

        position_events = rm.get_risk_events(event_type=RiskViolation.MAX_POSITION_SIZE)
        assert len(position_events) == 1

    def test_clear_risk_events(self):
        """Test clearing risk events."""
        rm = RiskManager()

        rm._log_risk_event(
            RiskViolation.DAILY_LOSS_LIMIT,
            "violation",
            "Test",
            {},
        )

        assert len(rm.get_risk_events()) >= 1

        rm.clear_risk_events()
        assert len(rm.get_risk_events()) == 0

    def test_get_current_drawdown(self):
        """Test drawdown calculation."""
        rm = RiskManager()

        with patch.object(rm, "get_peak_equity", return_value=1000.0):
            with patch.object(rm, "get_current_equity", return_value=850.0):
                drawdown = rm.get_current_drawdown()
                assert abs(drawdown - 0.15) < 0.001  # 15% drawdown

    def test_get_current_drawdown_no_peak(self):
        """Test drawdown with zero peak."""
        rm = RiskManager()

        with patch.object(rm, "get_peak_equity", return_value=0.0):
            with patch.object(rm, "get_current_equity", return_value=100.0):
                drawdown = rm.get_current_drawdown()
                assert drawdown == 0.0


class TestConvenienceFunctions:
    """Tests for module-level convenience functions."""

    def test_get_risk_manager_singleton(self):
        """Test get_risk_manager returns singleton."""
        import src.execution.risk_manager as rm_module

        rm_module._risk_manager = None

        rm1 = get_risk_manager()
        rm2 = get_risk_manager()

        assert rm1 is rm2

    def test_check_trade_risk_convenience(self):
        """Test check_trade_risk convenience function."""
        import src.execution.risk_manager as rm_module

        rm_module._risk_manager = None

        with patch.object(RiskManager, "get_bankroll", return_value=1000.0):
            with patch.object(RiskManager, "get_daily_pnl", return_value=0.0):
                with patch.object(RiskManager, "get_total_exposure", return_value=0.0):
                    with patch.object(RiskManager, "get_position_exposure", return_value=0.0):
                        with patch.object(RiskManager, "get_current_drawdown", return_value=0.0):
                            result = check_trade_risk("TEST", "yes", 10, 50)
                            assert isinstance(result, RiskCheckResult)

    def test_is_trading_allowed_convenience(self):
        """Test is_trading_allowed convenience function."""
        import src.execution.risk_manager as rm_module

        rm_module._risk_manager = None

        with patch.object(RiskManager, "get_bankroll", return_value=1000.0):
            with patch.object(RiskManager, "get_daily_pnl", return_value=0.0):
                with patch.object(RiskManager, "get_total_exposure", return_value=0.0):
                    with patch.object(RiskManager, "get_current_drawdown", return_value=0.0):
                        assert is_trading_allowed() is True


class TestRiskEnums:
    """Tests for risk enums."""

    def test_risk_status_values(self):
        """Test RiskStatus enum values."""
        assert RiskStatus.APPROVED.value == "approved"
        assert RiskStatus.REJECTED.value == "rejected"
        assert RiskStatus.WARNING.value == "warning"

    def test_risk_violation_values(self):
        """Test RiskViolation enum values."""
        assert RiskViolation.DAILY_LOSS_LIMIT.value == "daily_loss_limit"
        assert RiskViolation.MAX_POSITION_SIZE.value == "max_position_size"
        assert RiskViolation.MAX_TOTAL_EXPOSURE.value == "max_total_exposure"
        assert RiskViolation.MAX_DRAWDOWN.value == "max_drawdown"
        assert RiskViolation.TRADING_PAUSED.value == "trading_paused"
