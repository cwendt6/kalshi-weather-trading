"""
Unit tests for alert system.
"""
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

from src.utils.alerts import (
    Alert,
    AlertManager,
    AlertPriority,
    AlertType,
    alert_opportunity,
    alert_risk,
    alert_trade,
    get_alert_manager,
)


class TestAlert:
    """Tests for Alert dataclass."""

    def test_alert_creation(self):
        """Test Alert creation."""
        alert = Alert(
            alert_type=AlertType.OPPORTUNITY,
            priority=AlertPriority.HIGH,
            title="Test Alert",
            message="This is a test",
            data={"key": "value"},
        )

        assert alert.alert_type == AlertType.OPPORTUNITY
        assert alert.priority == AlertPriority.HIGH
        assert alert.title == "Test Alert"
        assert alert.sent is False

    def test_alert_to_dict(self):
        """Test alert to_dict conversion."""
        alert = Alert(
            alert_type=AlertType.TRADE_OPENED,
            priority=AlertPriority.MEDIUM,
            title="Trade Opened",
            message="BUY 10 YES @ 50",
            data={"ticker": "TEST"},
            alert_id="ALT-001",
        )

        d = alert.to_dict()

        assert d["type"] == "trade_opened"
        assert d["priority"] == "medium"
        assert d["title"] == "Trade Opened"
        assert d["alert_id"] == "ALT-001"

    def test_alert_to_console_string(self):
        """Test alert console formatting."""
        alert = Alert(
            alert_type=AlertType.RISK_VIOLATION,
            priority=AlertPriority.CRITICAL,
            title="Risk Alert",
            message="Daily loss limit exceeded",
            data={},
        )

        console_str = alert.to_console_string()

        assert "RISK_VIOLATION" in console_str
        assert "Risk Alert" in console_str
        assert "Daily loss limit exceeded" in console_str

    def test_alert_to_webhook_slack(self):
        """Test alert Slack webhook formatting."""
        alert = Alert(
            alert_type=AlertType.OPPORTUNITY,
            priority=AlertPriority.HIGH,
            title="Trading Opportunity",
            message="Edge: +15%",
            data={},
        )

        payload = alert.to_webhook_payload(format="slack")

        assert "text" in payload
        assert "blocks" in payload
        assert payload["text"] == "*Trading Opportunity*"

    def test_alert_to_webhook_discord(self):
        """Test alert Discord webhook formatting."""
        alert = Alert(
            alert_type=AlertType.OPPORTUNITY,
            priority=AlertPriority.HIGH,
            title="Trading Opportunity",
            message="Edge: +15%",
            data={},
        )

        payload = alert.to_webhook_payload(format="discord")

        assert "embeds" in payload
        assert len(payload["embeds"]) == 1
        assert payload["embeds"][0]["title"] == "Trading Opportunity"


class TestAlertManager:
    """Tests for AlertManager."""

    def test_initialization(self):
        """Test alert manager initialization."""
        manager = AlertManager(
            edge_threshold=0.15,
            console_enabled=True,
            webhook_url="https://example.com/webhook",
        )

        assert manager.edge_threshold == 0.15
        assert manager.console_enabled is True
        assert manager.webhook_url == "https://example.com/webhook"

    def test_send_alert_console(self):
        """Test sending alert to console."""
        manager = AlertManager(console_enabled=True)

        alert = Alert(
            alert_type=AlertType.SYSTEM_INFO,
            priority=AlertPriority.LOW,
            title="Test",
            message="Test message",
            data={},
        )

        result = manager.send(alert)

        assert result is True
        assert alert.sent is True

    def test_send_alert_rate_limited(self):
        """Test alert rate limiting."""
        manager = AlertManager(rate_limit_seconds=60)

        alert1 = Alert(
            alert_type=AlertType.OPPORTUNITY,
            priority=AlertPriority.MEDIUM,
            title="Same Alert",
            message="First",
            data={},
        )

        alert2 = Alert(
            alert_type=AlertType.OPPORTUNITY,
            priority=AlertPriority.MEDIUM,
            title="Same Alert",
            message="Second",
            data={},
        )

        with patch("builtins.print"):
            result1 = manager.send(alert1)
            result2 = manager.send(alert2)

            assert result1 is True
            assert result2 is False  # Rate limited

    def test_alert_opportunity_above_threshold(self):
        """Test opportunity alert above threshold."""
        manager = AlertManager(edge_threshold=0.10, console_enabled=False)

        with patch.object(manager, "send", return_value=True) as mock_send:
            alert = manager.alert_opportunity(
                ticker="TEST",
                edge=0.15,  # Above 10% threshold
                side="yes",
                price=50,
                ev=5.0,
            )

            assert alert is not None
            mock_send.assert_called_once()

    def test_alert_opportunity_below_threshold(self):
        """Test opportunity alert below threshold (filtered)."""
        manager = AlertManager(edge_threshold=0.10, console_enabled=False)

        alert = manager.alert_opportunity(
            ticker="TEST",
            edge=0.05,  # Below 10% threshold
            side="yes",
            price=50,
            ev=2.0,
        )

        assert alert is None

    def test_alert_trade_opened(self):
        """Test trade opened alert."""
        manager = AlertManager(console_enabled=False)

        with patch.object(manager, "send", return_value=True) as mock_send:
            alert = manager.alert_trade(
                ticker="TEST",
                action="buy",
                side="yes",
                quantity=10,
                price=50,
            )

            assert alert is not None
            assert alert.alert_type == AlertType.TRADE_OPENED
            mock_send.assert_called_once()

    def test_alert_trade_closed_with_pnl(self):
        """Test trade closed alert with P&L."""
        manager = AlertManager(console_enabled=False)

        with patch.object(manager, "send", return_value=True) as mock_send:
            alert = manager.alert_trade(
                ticker="TEST",
                action="sell",
                side="yes",
                quantity=10,
                price=60,
                pnl=10.0,
            )

            assert alert is not None
            assert alert.alert_type == AlertType.TRADE_CLOSED
            assert "P&L" in alert.message

    def test_alert_risk_warning(self):
        """Test risk warning alert."""
        manager = AlertManager(console_enabled=False)

        with patch.object(manager, "send", return_value=True) as mock_send:
            alert = manager.alert_risk(
                violation_type="daily_loss_limit",
                message="Approaching daily loss limit",
                is_critical=False,
            )

            assert alert is not None
            assert alert.alert_type == AlertType.RISK_WARNING
            assert alert.priority == AlertPriority.HIGH

    def test_alert_risk_violation(self):
        """Test risk violation alert."""
        manager = AlertManager(console_enabled=False)

        with patch.object(manager, "send", return_value=True) as mock_send:
            alert = manager.alert_risk(
                violation_type="max_drawdown",
                message="Max drawdown exceeded - trading paused",
                is_critical=True,
            )

            assert alert is not None
            assert alert.alert_type == AlertType.RISK_VIOLATION
            assert alert.priority == AlertPriority.CRITICAL

    def test_alert_system_info(self):
        """Test system info alert."""
        manager = AlertManager(console_enabled=False)

        with patch.object(manager, "send", return_value=True) as mock_send:
            alert = manager.alert_system(
                title="System Started",
                message="Trading system initialized",
                is_error=False,
            )

            assert alert is not None
            assert alert.alert_type == AlertType.SYSTEM_INFO
            assert alert.priority == AlertPriority.LOW

    def test_alert_system_error(self):
        """Test system error alert."""
        manager = AlertManager(console_enabled=False)

        with patch.object(manager, "send", return_value=True) as mock_send:
            alert = manager.alert_system(
                title="Database Error",
                message="Failed to connect to database",
                is_error=True,
            )

            assert alert is not None
            assert alert.alert_type == AlertType.SYSTEM_ERROR
            assert alert.priority == AlertPriority.HIGH

    def test_add_custom_handler(self):
        """Test adding custom alert handler."""
        manager = AlertManager(console_enabled=False)

        handler_calls = []

        def custom_handler(alert: Alert) -> None:
            handler_calls.append(alert)

        manager.add_handler(custom_handler)

        alert = Alert(
            alert_type=AlertType.SYSTEM_INFO,
            priority=AlertPriority.LOW,
            title="Test",
            message="Test",
            data={},
        )

        manager.send(alert)

        assert len(handler_calls) == 1
        assert handler_calls[0] is alert

    def test_get_recent_alerts(self):
        """Test getting recent alerts."""
        manager = AlertManager(console_enabled=False, rate_limit_seconds=0)

        # Send multiple alerts
        for i in range(5):
            alert = Alert(
                alert_type=AlertType.SYSTEM_INFO,
                priority=AlertPriority.LOW,
                title=f"Alert {i}",
                message=f"Message {i}",
                data={},
            )
            manager.send(alert)

        recent = manager.get_recent_alerts(limit=3)
        assert len(recent) == 3

    def test_get_recent_alerts_filtered(self):
        """Test getting filtered recent alerts."""
        manager = AlertManager(console_enabled=False, rate_limit_seconds=0)

        # Send different types
        manager._alert_history.append(
            Alert(
                alert_type=AlertType.OPPORTUNITY,
                priority=AlertPriority.HIGH,
                title="Opp 1",
                message="",
                data={},
            )
        )
        manager._alert_history.append(
            Alert(
                alert_type=AlertType.TRADE_OPENED,
                priority=AlertPriority.LOW,
                title="Trade 1",
                message="",
                data={},
            )
        )
        manager._alert_history.append(
            Alert(
                alert_type=AlertType.OPPORTUNITY,
                priority=AlertPriority.HIGH,
                title="Opp 2",
                message="",
                data={},
            )
        )

        opportunities = manager.get_recent_alerts(alert_type=AlertType.OPPORTUNITY)
        assert len(opportunities) == 2

    def test_clear_history(self):
        """Test clearing alert history."""
        manager = AlertManager(console_enabled=False)

        manager._alert_history.append(
            Alert(
                alert_type=AlertType.SYSTEM_INFO,
                priority=AlertPriority.LOW,
                title="Test",
                message="",
                data={},
            )
        )

        assert len(manager._alert_history) == 1

        manager.clear_history()

        assert len(manager._alert_history) == 0


class TestConvenienceFunctions:
    """Tests for module-level convenience functions."""

    def test_get_alert_manager_singleton(self):
        """Test get_alert_manager returns singleton."""
        import src.utils.alerts as alerts_module

        alerts_module._alert_manager = None

        manager1 = get_alert_manager()
        manager2 = get_alert_manager()

        assert manager1 is manager2

    def test_alert_opportunity_convenience(self):
        """Test alert_opportunity convenience function."""
        import src.utils.alerts as alerts_module

        alerts_module._alert_manager = None

        with patch.object(AlertManager, "alert_opportunity", return_value=None):
            result = alert_opportunity("TEST", 0.05, "yes", 50, 2.0)
            # Below threshold, should return None

    def test_alert_trade_convenience(self):
        """Test alert_trade convenience function."""
        import src.utils.alerts as alerts_module

        alerts_module._alert_manager = None

        manager = get_alert_manager()
        manager.console_enabled = False

        with patch.object(manager, "send", return_value=True):
            result = alert_trade("TEST", "buy", "yes", 10, 50)
            assert result is not None

    def test_alert_risk_convenience(self):
        """Test alert_risk convenience function."""
        import src.utils.alerts as alerts_module

        alerts_module._alert_manager = None

        manager = get_alert_manager()
        manager.console_enabled = False

        with patch.object(manager, "send", return_value=True):
            result = alert_risk("test_violation", "Test message", is_critical=False)
            assert result is not None


class TestAlertEnums:
    """Tests for alert enums."""

    def test_alert_type_values(self):
        """Test AlertType enum values."""
        assert AlertType.OPPORTUNITY.value == "opportunity"
        assert AlertType.TRADE_OPENED.value == "trade_opened"
        assert AlertType.RISK_VIOLATION.value == "risk_violation"

    def test_alert_priority_values(self):
        """Test AlertPriority enum values."""
        assert AlertPriority.LOW.value == "low"
        assert AlertPriority.MEDIUM.value == "medium"
        assert AlertPriority.HIGH.value == "high"
        assert AlertPriority.CRITICAL.value == "critical"
