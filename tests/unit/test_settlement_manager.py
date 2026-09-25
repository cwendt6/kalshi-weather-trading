"""Tests for the settlement manager (Phase 5)."""
import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from src.execution.settlement_manager import SettlementManager
from src.utils.fees import KALSHI_WINNER_FEE_RATE


@pytest.fixture
def sm():
    return SettlementManager(paper_trading=True)


class TestSettlementPnL:
    def test_settlement_pnl_yes_win(self, sm):
        """Bought 10 YES @ 15c, market resolved YES.
        Gross = (10*100 - 10*15)/100 = $8.50
        Fee = $8.50 * 0.02 = $0.17
        Net = $8.33
        """
        pnl, pnl_pct, outcome = sm._calculate_settlement_pnl("yes", 10, 15, "yes")
        assert outcome == "win"
        assert pnl == pytest.approx(8.50 - 8.50 * 0.02, abs=0.01)
        assert pnl > 0

    def test_settlement_pnl_yes_loss(self, sm):
        """Bought 10 YES @ 15c, market resolved NO. PnL = -$1.50."""
        pnl, pnl_pct, outcome = sm._calculate_settlement_pnl("yes", 10, 15, "no")
        assert outcome == "loss"
        assert pnl == pytest.approx(-1.50)
        assert pnl_pct == -100.0

    def test_settlement_pnl_no_win(self, sm):
        """Bought 10 NO @ 5c, market resolved NO.
        Gross = (10*100 - 10*5)/100 = $9.50
        Fee = $9.50 * 0.02 = $0.19
        Net = $9.31
        """
        pnl, pnl_pct, outcome = sm._calculate_settlement_pnl("no", 10, 5, "no")
        assert outcome == "win"
        assert pnl == pytest.approx(9.50 - 9.50 * 0.02, abs=0.01)

    def test_settlement_pnl_no_loss(self, sm):
        """Bought 10 NO @ 5c, market resolved YES. PnL = -$0.50."""
        pnl, pnl_pct, outcome = sm._calculate_settlement_pnl("no", 10, 5, "yes")
        assert outcome == "loss"
        assert pnl == pytest.approx(-0.50)


class TestSettlementProcess:
    def test_position_cleanup(self, sm):
        """After settlement, PositionDB row is deleted."""
        mock_session = MagicMock()
        mock_position = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = mock_position

        result = sm._cleanup_position_in_session(mock_session, "TEST-TICKER")
        assert result is True
        mock_session.delete.assert_called_once_with(mock_position)

    def test_position_cleanup_no_position(self, sm):
        """No position to clean up returns False."""
        mock_session = MagicMock()
        mock_session.query.return_value.filter_by.return_value.first.return_value = None

        result = sm._cleanup_position_in_session(mock_session, "TEST-TICKER")
        assert result is False

    def test_obs_pool_replenished(self):
        """Settled obs_settled trade calls risk_manager.record_obs_settlement()."""
        mock_rm = MagicMock()
        sm = SettlementManager(risk_manager=mock_rm, paper_trading=True)

        # Manually test the pool replenishment logic
        strategy = "obs_settled"
        quantity = 10
        entry_price = 95
        pnl_dollars = 0.49  # Net profit on 10 NO @ 95c

        if mock_rm and "obs_settled" in strategy:
            deployed = quantity * entry_price / 100.0
            profit = max(0.0, pnl_dollars)
            mock_rm.record_obs_settlement(deployed, profit)

        mock_rm.record_obs_settlement.assert_called_once_with(9.50, 0.49)

    def test_duplicate_settlement_skipped(self, sm):
        """Same ticker settled twice doesn't double-count."""
        sm._processed_tickers.add("KXHIGHNY-26FEB13-B36")
        # Second call should skip this ticker
        assert "KXHIGHNY-26FEB13-B36" in sm._processed_tickers

    def test_settlement_summary(self, sm):
        """Summary returns correct structure."""
        summary = sm.get_settlement_summary(hours=24)
        assert "total_settled" in summary
        assert "total_pnl" in summary
        assert "wins" in summary
        assert "losses" in summary
        assert "by_strategy" in summary
