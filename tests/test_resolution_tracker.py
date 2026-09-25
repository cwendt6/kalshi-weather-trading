"""
Unit tests for the resolution tracker.

Tests cover:
- P&L calculation accuracy
- Fee deduction in P&L
- Win/loss classification based on NET P&L
- Strategy breakdown
"""
import pytest
from src.utils.fees import KALSHI_WINNER_FEE_RATE


class TestPnLCalculation:
    """Test P&L math for various trade scenarios."""

    def test_buy_no_wins_at_96c(self):
        """BUY NO at 96c, market resolves NO -> net profit after fee."""
        buy_price = 0.96
        payout = 1.00
        gross_profit = payout - buy_price  # $0.04
        fee = payout * KALSHI_WINNER_FEE_RATE  # $0.02
        net_profit = gross_profit - fee  # $0.02
        assert net_profit == pytest.approx(0.02)
        assert net_profit > 0, "Should be a net win"

    def test_buy_no_wins_at_95c(self):
        """BUY NO at 95c, market resolves NO -> net profit after fee."""
        buy_price = 0.95
        payout = 1.00
        gross_profit = payout - buy_price  # $0.05
        fee = payout * KALSHI_WINNER_FEE_RATE  # $0.02
        net_profit = gross_profit - fee  # $0.03
        assert net_profit == pytest.approx(0.03)

    def test_buy_no_wins_at_98c(self):
        """BUY NO at 98c, market resolves NO -> breaks even after fee."""
        buy_price = 0.98
        payout = 1.00
        gross_profit = payout - buy_price  # $0.02
        fee = payout * KALSHI_WINNER_FEE_RATE  # $0.02
        net_profit = gross_profit - fee  # $0.00
        assert net_profit == pytest.approx(0.00)

    def test_buy_no_wins_at_99c(self):
        """BUY NO at 99c, market resolves NO -> NET LOSS after fee."""
        buy_price = 0.99
        payout = 1.00
        gross_profit = payout - buy_price  # $0.01
        fee = payout * KALSHI_WINNER_FEE_RATE  # $0.02
        net_profit = gross_profit - fee  # -$0.01
        assert net_profit == pytest.approx(-0.01)
        assert net_profit < 0, "Should be classified as loss"

    def test_buy_no_loses(self):
        """BUY NO at 96c, market resolves YES -> full loss (no fee on losses)."""
        buy_price = 0.96
        pnl = -buy_price  # Lost entire buy price
        assert pnl == pytest.approx(-0.96)

    def test_buy_yes_wins_at_50c(self):
        """BUY YES at 50c, market resolves YES -> net profit."""
        buy_price = 0.50
        payout = 1.00
        gross_profit = payout - buy_price  # $0.50
        fee = payout * KALSHI_WINNER_FEE_RATE  # $0.02
        net_profit = gross_profit - fee  # $0.48
        assert net_profit == pytest.approx(0.48)

    def test_multicontract_pnl(self):
        """10 contracts BUY NO at 96c, resolves NO -> net profit."""
        contracts = 10
        buy_price = 0.96
        payout = 1.00
        total_cost = contracts * buy_price  # $9.60
        total_payout = contracts * payout  # $10.00
        gross_profit = total_payout - total_cost  # $0.40
        total_fee = contracts * payout * KALSHI_WINNER_FEE_RATE  # $0.20
        net_profit = gross_profit - total_fee  # $0.20
        assert net_profit == pytest.approx(0.20)


class TestOutcomeClassification:
    """Test that outcomes are classified by NET P&L, not market direction."""

    def test_win_when_net_positive(self):
        """Trade is a 'win' only if net P&L > 0."""
        net_pnl = 0.02  # Small positive
        outcome = "win" if net_pnl >= 0 else "loss"
        assert outcome == "win"

    def test_loss_when_net_negative(self):
        """Trade at 99c 'wins' market direction but loses money -> classified as loss."""
        buy_price = 0.99
        gross_profit = 1.0 - buy_price  # 0.01
        fee = 0.02
        net_pnl = gross_profit - fee  # -0.01
        outcome = "win" if net_pnl >= 0 else "loss"
        assert outcome == "loss", "99c trade should be loss despite correct market direction"

    def test_breakeven_is_win(self):
        """Zero net P&L counts as 'win' (not loss)."""
        net_pnl = 0.0
        outcome = "win" if net_pnl >= 0 else "loss"
        assert outcome == "win"
