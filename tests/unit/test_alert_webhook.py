"""Tests for alert webhook integration (Phase 5)."""
import time
import pytest
from unittest.mock import patch, MagicMock

from src.utils.alerts import (
    AlertManager,
    AlertType,
    AlertPriority,
    Alert,
    get_alert_manager,
)


@pytest.fixture(autouse=True)
def reset_alert_manager():
    """Reset global alert manager between tests."""
    import src.utils.alerts as alerts_mod
    alerts_mod._alert_manager = None
    yield
    alerts_mod._alert_manager = None


class TestWebhookInit:
    def test_discord_webhook_init(self):
        """Setting DISCORD_WEBHOOK_URL creates AlertManager with discord format."""
        with patch.dict("os.environ", {"DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/test"}):
            mgr = get_alert_manager()
        assert mgr.webhook_url == "https://discord.com/api/webhooks/test"
        assert mgr.webhook_format == "discord"

    def test_slack_webhook_init(self):
        """Setting SLACK_WEBHOOK_URL (no Discord) creates slack format."""
        with patch.dict("os.environ", {
            "SLACK_WEBHOOK_URL": "https://hooks.slack.com/test",
        }, clear=False):
            import os
            os.environ.pop("DISCORD_WEBHOOK_URL", None)
            import src.utils.alerts as alerts_mod
            alerts_mod._alert_manager = None
            mgr = get_alert_manager()
        assert mgr.webhook_url == "https://hooks.slack.com/test"
        assert mgr.webhook_format == "slack"

    def test_no_webhook_console_only(self):
        """No webhook URLs -> console only."""
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("DISCORD_WEBHOOK_URL", None)
            os.environ.pop("SLACK_WEBHOOK_URL", None)
            import src.utils.alerts as alerts_mod
            alerts_mod._alert_manager = None
            mgr = get_alert_manager()
        assert mgr.webhook_url is None


class TestAlertMethods:
    def test_settlement_alert_fired(self):
        """Settlement triggers alert_settlement() with correct PnL."""
        mgr = AlertManager(console_enabled=False)
        result = mgr.alert_settlement(
            ticker="KXHIGHNY-26FEB13-B36",
            side="no",
            result="no",
            pnl=2.94,
            strategy="obs_settled",
            quantity=10,
        )
        assert result is not None
        assert result.alert_type == AlertType.SETTLEMENT
        assert "2.94" in result.message

    def test_daily_summary_format(self):
        """Daily summary alert contains all required fields."""
        mgr = AlertManager(console_enabled=False)
        result = mgr.alert_daily_summary(
            date_str="2026-02-13",
            total_pnl=5.42,
            trades=12,
            wins=8,
            losses=4,
            settlements=6,
            obs_pool_balance=14.30,
        )
        assert result is not None
        assert "5.42" in result.message
        assert "12" in result.message
        assert "14.30" in result.message

    def test_obs_settled_alert(self):
        """Obs-settled alert includes guaranteed profit."""
        mgr = AlertManager(console_enabled=False)
        result = mgr.alert_obs_settled(
            ticker="KXHIGHNY-26FEB13-B36",
            no_price=95,
            contracts=10,
            guaranteed_profit=0.49,
        )
        assert result is not None
        assert result.alert_type == AlertType.OBS_SETTLED

    def test_rate_limiting(self):
        """Two identical alerts within 60s: first sends, second is suppressed."""
        mgr = AlertManager(console_enabled=False, rate_limit_seconds=60)

        alert1 = Alert(
            alert_type=AlertType.SETTLEMENT,
            priority=AlertPriority.MEDIUM,
            title="Test Settlement: TICKER1",
            message="Test",
            data={},
        )
        assert mgr.send(alert1) is True

        # Same alert type + title within rate limit
        alert2 = Alert(
            alert_type=AlertType.SETTLEMENT,
            priority=AlertPriority.MEDIUM,
            title="Test Settlement: TICKER1",
            message="Test again",
            data={},
        )
        assert mgr.send(alert2) is False  # Rate limited
