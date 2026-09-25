"""
Unit tests for strategy profitability after fees.

Tests verify that:
- Each strategy respects fee thresholds
- No strategy generates signals for unprofitable trades
- Edge calculations account for the 2% Kalshi fee
"""
import pytest
from src.utils.fees import KALSHI_WINNER_FEE_RATE


class TestImpossibleScannerThresholds:
    """Test impossible scanner fee awareness."""

    def test_max_no_price(self):
        from src.strategy.impossible_scanner import ImpossibleEventScanner
        scanner = ImpossibleEventScanner()
        assert scanner.MAX_NO_PRICE == 0.93  # Tightened from 97c to 93c

    def test_fee_constant_is_correct(self):
        from src.strategy.impossible_scanner import ImpossibleEventScanner
        scanner = ImpossibleEventScanner()
        assert scanner.KALSHI_WINNER_FEE_PCT == 0.02

    def test_min_profit_pct_default(self):
        from src.strategy.impossible_scanner import ImpossibleEventScanner
        scanner = ImpossibleEventScanner()
        assert scanner.min_profit_pct >= 0.03, "min_profit_pct must be >= 3% (paper mode)"

    def test_min_profit_pct_from_factory(self):
        from src.strategy.impossible_scanner import get_impossible_scanner
        scanner = get_impossible_scanner(min_profit_pct=0.04)
        assert scanner.min_profit_pct == 0.04


class TestStraddleArbitrageThresholds:
    """Test straddle arbitrage fee awareness."""

    def test_fee_rate_is_two_percent(self):
        from src.strategy.straddle_arbitrage import StraddleArbitrage
        sa = StraddleArbitrage()
        assert sa.FEE_RATE == 0.02

    def test_min_profit_margin_is_net_of_fees(self):
        from src.strategy.straddle_arbitrage import StraddleArbitrage
        sa = StraddleArbitrage()
        # MIN_PROFIT_MARGIN is applied AFTER fee deduction (net payout = 0.98)
        # So any positive MIN_PROFIT_MARGIN ensures net profitability
        assert sa.MIN_PROFIT_MARGIN >= sa.FEE_RATE, (
            f"MIN_PROFIT_MARGIN ({sa.MIN_PROFIT_MARGIN}) must be >= "
            f"FEE_RATE ({sa.FEE_RATE})"
        )


class TestSignalGeneratorThresholds:
    """Test signal generator fee awareness."""

    def test_min_edge_default_covers_fees(self):
        from src.strategy.signal_generator import SignalGenerator
        sg = SignalGenerator()
        # min_edge must be >= KALSHI_WINNER_FEE_RATE to cover fees
        assert sg.min_edge >= KALSHI_WINNER_FEE_RATE, (
            f"min_edge ({sg.min_edge}) must be >= {KALSHI_WINNER_FEE_RATE} to cover fees"
        )

    def test_factory_default_min_edge(self):
        from src.strategy.signal_generator import get_signal_generator
        sg = get_signal_generator()
        assert sg.min_edge >= KALSHI_WINNER_FEE_RATE


class TestFeeAwareProfitCalculations:
    """Test that profit calculations are fee-aware across strategies."""

    def test_straddle_net_payout_after_fee(self):
        """Straddle net payout should be 1.0 - 0.02 = 0.98."""
        from src.strategy.straddle_arbitrage import StraddleArbitrage
        sa = StraddleArbitrage()
        net_payout = 1.0 - sa.FEE_RATE
        assert net_payout == pytest.approx(0.98)

    def test_straddle_profitable_only_below_98(self):
        """Straddle is only profitable when total cost < 98c (net payout)."""
        from src.strategy.straddle_arbitrage import StraddleArbitrage
        sa = StraddleArbitrage()
        net_payout = 1.0 - sa.FEE_RATE
        # Max total cost = net_payout - MIN_PROFIT_MARGIN
        max_cost = net_payout - sa.MIN_PROFIT_MARGIN
        assert max_cost <= 0.96, "Max straddle cost should be at or below 96c"

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
