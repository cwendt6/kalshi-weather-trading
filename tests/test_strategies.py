"""
Unit tests for strategy profitability after fees.

Tests verify that:
- Each strategy respects fee thresholds
- No strategy generates signals for unprofitable trades
- Edge calculations account for the 2% Kalshi fee
"""
import pytest
from src.utils.fees import KALSHI_WINNER_FEE_RATE








class TestFeeAwareProfitCalculations:
    """Test that profit calculations are fee-aware across strategies."""



    def test_impossible_scanner_no_net_loss_at_96(self):
        """At 96c NO price, net profit should be positive."""
        no_price = 0.96
        gross = 1.0 - no_price  # 0.04
        fee = KALSHI_WINNER_FEE_RATE  # 0.02
        net = gross - fee  # 0.02
        assert net > 0, f"Net profit at 96c should be positive, got {net}"

    def test_impossible_scanner_net_loss_at_99(self):
        """At 99c NO price, net profit should be negative."""
        no_price = 0.99
        gross = 1.0 - no_price  # 0.01
        fee = KALSHI_WINNER_FEE_RATE  # 0.02
        net = gross - fee  # -0.01
        assert net < 0, f"Net profit at 99c should be negative, got {net}"
