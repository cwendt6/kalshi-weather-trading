"""
Tests for position manager: exit rules, take-profit, trailing stops,
stop losses, time-decay, price tier classification, and fee awareness.

Run: pytest tests/test_position_manager.py -v
"""
import pytest
import sys
import os
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.execution.position_manager import (
    PositionManager,
    PositionState,
    ExitRule,
    ExitSignal,
    ExitReason,
    PriceTier,
    DEFAULT_EXIT_RULES,
    get_position_manager,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Price Tier Classification
# ═══════════════════════════════════════════════════════════════════════════════

class TestPriceTierClassification:
    """Test that positions are assigned to the correct price tier."""

    def test_longshot_tier(self):
        assert PositionManager.classify_tier(5) == PriceTier.LONGSHOT
        assert PositionManager.classify_tier(10) == PriceTier.LONGSHOT
        assert PositionManager.classify_tier(15) == PriceTier.LONGSHOT

    def test_low_tier(self):
        assert PositionManager.classify_tier(16) == PriceTier.LOW
        assert PositionManager.classify_tier(25) == PriceTier.LOW
        assert PositionManager.classify_tier(35) == PriceTier.LOW

    def test_mid_tier(self):
        assert PositionManager.classify_tier(36) == PriceTier.MID
        assert PositionManager.classify_tier(50) == PriceTier.MID
        assert PositionManager.classify_tier(65) == PriceTier.MID

    def test_high_tier(self):
        assert PositionManager.classify_tier(66) == PriceTier.HIGH
        assert PositionManager.classify_tier(85) == PriceTier.HIGH
        assert PositionManager.classify_tier(97) == PriceTier.HIGH

    def test_boundary_values(self):
        """Boundary: 15 is LONGSHOT, 16 is LOW, etc."""
        assert PositionManager.classify_tier(15) == PriceTier.LONGSHOT
        assert PositionManager.classify_tier(16) == PriceTier.LOW
        assert PositionManager.classify_tier(35) == PriceTier.LOW
        assert PositionManager.classify_tier(36) == PriceTier.MID
        assert PositionManager.classify_tier(65) == PriceTier.MID
        assert PositionManager.classify_tier(66) == PriceTier.HIGH

    def test_extreme_values(self):
        assert PositionManager.classify_tier(1) == PriceTier.LONGSHOT
        assert PositionManager.classify_tier(99) == PriceTier.HIGH


# ═══════════════════════════════════════════════════════════════════════════════
# Default Exit Rules
# ═══════════════════════════════════════════════════════════════════════════════

class TestDefaultExitRules:
    """Test that default exit rules are correctly configured."""

    def test_all_tiers_have_rules(self):
        for tier in PriceTier:
            assert tier in DEFAULT_EXIT_RULES

    def test_longshot_has_3_tp_levels(self):
        rule = DEFAULT_EXIT_RULES[PriceTier.LONGSHOT]
        assert len(rule.take_profit_ladder) == 3

    def test_high_tier_tighter_stops(self):
        """HIGH tier should have tighter stop loss than LONGSHOT."""
        high = DEFAULT_EXIT_RULES[PriceTier.HIGH]
        longshot = DEFAULT_EXIT_RULES[PriceTier.LONGSHOT]
        assert high.stop_loss_pct < longshot.stop_loss_pct

    def test_high_tier_shorter_hold(self):
        """HIGH tier should have shorter max hold than LONGSHOT."""
        high = DEFAULT_EXIT_RULES[PriceTier.HIGH]
        longshot = DEFAULT_EXIT_RULES[PriceTier.LONGSHOT]
        assert high.max_hold_hours < longshot.max_hold_hours

    def test_trailing_stop_distances_decrease_with_tier(self):
        """Higher tiers should have tighter trailing stops."""
        longshot_dist = DEFAULT_EXIT_RULES[PriceTier.LONGSHOT].trailing_stop_distance
        high_dist = DEFAULT_EXIT_RULES[PriceTier.HIGH].trailing_stop_distance
        assert high_dist < longshot_dist

    def test_all_rules_have_positive_stop_loss(self):
        for tier, rule in DEFAULT_EXIT_RULES.items():
            assert rule.stop_loss_pct > 0, f"{tier.value} has no stop loss"


# ═══════════════════════════════════════════════════════════════════════════════
# P&L Calculation
# ═══════════════════════════════════════════════════════════════════════════════

class TestPnLCalculation:
    """Test unrealized P&L calculations."""

    def setup_method(self):
        self.pm = PositionManager(paper_trading=True)

    def test_yes_position_profit(self):
        """YES bought at 40c, now at 60c -> profit."""
        pnl, pnl_pct = self.pm._calculate_unrealized_pnl("yes", 10, 40, 60)
        assert pnl == 2.0  # 10 * (60-40) / 100 = $2.00
        assert abs(pnl_pct - 0.50) < 0.01  # 50% gain

    def test_yes_position_loss(self):
        """YES bought at 60c, now at 40c -> loss."""
        pnl, pnl_pct = self.pm._calculate_unrealized_pnl("yes", 10, 60, 40)
        assert pnl == -2.0
        assert abs(pnl_pct - (-0.333)) < 0.01

    def test_no_position_profit(self):
        """NO bought at 40c (YES at 60c), YES drops to 30c -> profit."""
        # NO entry at 40c means YES was 60c. Now YES is 30c (NO value = 70c).
        pnl, pnl_pct = self.pm._calculate_unrealized_pnl("no", 10, 60, 30)
        # NO profit = entry_price(60) - current_yes(30) per contract
        assert pnl == 3.0  # 10 * (60-30) / 100

    def test_no_position_loss(self):
        """NO bought when YES at 40c, YES rises to 70c -> loss."""
        pnl, pnl_pct = self.pm._calculate_unrealized_pnl("no", 10, 40, 70)
        assert pnl == -3.0  # 10 * (40-70) / 100

    def test_zero_quantity(self):
        """Zero quantity should return 0 P&L."""
        pnl, pnl_pct = self.pm._calculate_unrealized_pnl("yes", 0, 50, 60)
        assert pnl == 0.0

    def test_zero_entry_price(self):
        """Zero entry price should handle gracefully."""
        pnl, pnl_pct = self.pm._calculate_unrealized_pnl("yes", 10, 0, 50)
        assert pnl == 5.0
        assert pnl_pct == 0.0  # Division by zero handled


# ═══════════════════════════════════════════════════════════════════════════════
# Exit Profit After Fees
# ═══════════════════════════════════════════════════════════════════════════════

class TestExitProfitAfterFees:
    """Test fee-aware exit profit calculations."""

    def setup_method(self):
        self.pm = PositionManager(paper_trading=True)

    def test_profitable_exit(self):
        """YES at 40c sold at 70c: gross $3, fee ~$0.14, net ~$2.86."""
        net = self.pm._calculate_exit_profit_after_fees("yes", 10, 40, 70)
        assert net > 0
        # Gross = 10 * (70-40)/100 = $3.00
        # Fee = 10 * 0.02 * 70/100 = $0.14
        assert abs(net - 2.86) < 0.01

    def test_barely_profitable_squeezed_by_fees(self):
        """Small gain that barely survives fees."""
        # YES at 95c sold at 97c: gross = $0.20 for 10 contracts
        # Fee = 10 * 0.02 * 97/100 = $0.194
        net = self.pm._calculate_exit_profit_after_fees("yes", 10, 95, 97)
        assert net > 0  # Should still be tiny positive

    def test_loss_exit(self):
        """YES at 60c sold at 40c -> loss (no fee concern)."""
        net = self.pm._calculate_exit_profit_after_fees("yes", 10, 60, 40)
        assert net < 0

    def test_no_position_exit(self):
        """NO position exit should also compute correctly."""
        # NO entry when YES=60c, sold when YES=30c
        net = self.pm._calculate_exit_profit_after_fees("no", 10, 60, 30)
        assert net > 0


# ═══════════════════════════════════════════════════════════════════════════════
# Take-Profit Logic
# ═══════════════════════════════════════════════════════════════════════════════

class TestTakeProfit:
    """Test take-profit ladder logic."""

    def setup_method(self):
        self.pm = PositionManager(paper_trading=True)

    def _make_state(self, entry, current, qty=10, side="yes"):
        tier = PositionManager.classify_tier(entry)
        return PositionState(
            ticker="TEST-TICKER",
            side=side,
            quantity=qty,
            entry_price=entry,
            current_price=current,
            peak_price=current,
            tier=tier,
            entry_time=datetime.now(timezone.utc) - timedelta(hours=1),
            last_checked=datetime.now(timezone.utc),
        )

    def test_tp_not_triggered_below_target(self):
        """Price below first TP level -> no signal."""
        state = self._make_state(10, 15)  # LONGSHOT, first TP at 25c
        rule = DEFAULT_EXIT_RULES[PriceTier.LONGSHOT]
        signal = self.pm._check_take_profit(state, rule)
        assert signal is None

    def test_tp_triggered_at_first_level(self):
        """Price hits first TP level -> partial exit signal."""
        state = self._make_state(10, 30)  # LONGSHOT, 30c > 25c TP1
        rule = DEFAULT_EXIT_RULES[PriceTier.LONGSHOT]
        signal = self.pm._check_take_profit(state, rule)
        assert signal is not None
        assert signal.reason == ExitReason.TAKE_PROFIT
        assert signal.quantity_to_exit < state.quantity  # Partial exit (33%)

    def test_tp_marks_level_as_hit(self):
        """After TP triggers, that level should be marked as hit."""
        state = self._make_state(10, 30)
        rule = DEFAULT_EXIT_RULES[PriceTier.LONGSHOT]
        signal = self.pm._check_take_profit(state, rule)
        assert 0 in state.tp_levels_hit

    def test_tp_skips_already_hit_level(self):
        """Previously hit TP level should not trigger again."""
        state = self._make_state(10, 30)
        state.tp_levels_hit = [0]  # Level 0 already hit
        rule = DEFAULT_EXIT_RULES[PriceTier.LONGSHOT]
        signal = self.pm._check_take_profit(state, rule)
        # Should look for next level (50c), but current is 30c
        assert signal is None

    def test_tp_second_level(self):
        """Price reaches second TP level (skipping first as hit)."""
        state = self._make_state(10, 55)  # LONGSHOT, 55c > 50c TP2
        state.tp_levels_hit = [0]  # L1 already hit
        rule = DEFAULT_EXIT_RULES[PriceTier.LONGSHOT]
        signal = self.pm._check_take_profit(state, rule)
        assert signal is not None
        assert 1 in state.tp_levels_hit

    def test_tp_disabled(self):
        """With take_profit_enabled=False, no TP signals."""
        pm = PositionManager(take_profit_enabled=False, paper_trading=True)
        state = self._make_state(10, 80)
        rule = DEFAULT_EXIT_RULES[PriceTier.LONGSHOT]
        signal = pm._check_take_profit(state, rule)
        assert signal is None


# ═══════════════════════════════════════════════════════════════════════════════
# Trailing Stop Logic
# ═══════════════════════════════════════════════════════════════════════════════

class TestTrailingStop:
    """Test trailing stop logic."""

    def setup_method(self):
        self.pm = PositionManager(paper_trading=True)

    def _make_state(self, entry, current, peak, side="yes"):
        tier = PositionManager.classify_tier(entry)
        return PositionState(
            ticker="TEST-TICKER",
            side=side,
            quantity=10,
            entry_price=entry,
            current_price=current,
            peak_price=peak,
            tier=tier,
            entry_time=datetime.now(timezone.utc) - timedelta(hours=1),
            last_checked=datetime.now(timezone.utc),
        )

    def test_not_activated_insufficient_gain(self):
        """Trailing stop doesn't activate if peak gain < activation threshold."""
        # LONGSHOT entry 10c, peak 15c (50% gain, need 100% for activation)
        state = self._make_state(10, 12, peak=15)
        rule = DEFAULT_EXIT_RULES[PriceTier.LONGSHOT]
        signal = self.pm._check_trailing_stop(state, rule)
        assert signal is None

    def test_activated_and_triggered(self):
        """Trailing stop activates after big gain, triggers on pullback."""
        # LONGSHOT entry 10c, peak 25c (150% gain > 100% activation)
        # Current 17c, drop from peak = (25-17)/25 = 32% > 30% distance
        state = self._make_state(10, 17, peak=25)
        rule = DEFAULT_EXIT_RULES[PriceTier.LONGSHOT]
        signal = self.pm._check_trailing_stop(state, rule)
        assert signal is not None
        assert signal.reason == ExitReason.TRAILING_STOP

    def test_activated_but_not_triggered(self):
        """Trailing stop active but price hasn't dropped enough."""
        # LONGSHOT entry 10c, peak 25c (150% gain > 100% activation)
        # Current 22c, drop from peak = (25-22)/25 = 12% < 30% distance
        state = self._make_state(10, 22, peak=25)
        rule = DEFAULT_EXIT_RULES[PriceTier.LONGSHOT]
        signal = self.pm._check_trailing_stop(state, rule)
        assert signal is None

    def test_disabled(self):
        """With trailing_stop_enabled=False, no trailing stop signals."""
        pm = PositionManager(trailing_stop_enabled=False, paper_trading=True)
        state = self._make_state(10, 17, peak=25)
        rule = DEFAULT_EXIT_RULES[PriceTier.LONGSHOT]
        signal = pm._check_trailing_stop(state, rule)
        assert signal is None


# ═══════════════════════════════════════════════════════════════════════════════
# Stop Loss Logic
# ═══════════════════════════════════════════════════════════════════════════════

class TestStopLoss:
    """Test hard stop loss logic."""

    def setup_method(self):
        self.pm = PositionManager(paper_trading=True)

    def _make_state(self, entry, current, side="yes"):
        tier = PositionManager.classify_tier(entry)
        return PositionState(
            ticker="TEST-TICKER",
            side=side,
            quantity=10,
            entry_price=entry,
            current_price=current,
            peak_price=entry,  # Never went up
            tier=tier,
            entry_time=datetime.now(timezone.utc) - timedelta(hours=1),
            last_checked=datetime.now(timezone.utc),
        )

    def test_stop_loss_triggered(self):
        """YES at 50c, drops to 25c -> 50% loss triggers MID stop (30%)."""
        state = self._make_state(50, 25)
        rule = DEFAULT_EXIT_RULES[PriceTier.MID]
        signal = self.pm._check_stop_loss(state, rule)
        assert signal is not None
        assert signal.reason == ExitReason.STOP_LOSS

    def test_stop_loss_not_triggered(self):
        """YES at 50c, drops to 40c -> 20% loss < 30% MID stop."""
        state = self._make_state(50, 40)
        rule = DEFAULT_EXIT_RULES[PriceTier.MID]
        signal = self.pm._check_stop_loss(state, rule)
        assert signal is None

    def test_stop_loss_longshot_wide(self):
        """LONGSHOT has 80% stop — only triggers on near-total loss."""
        state = self._make_state(10, 3)  # 70% loss
        rule = DEFAULT_EXIT_RULES[PriceTier.LONGSHOT]
        signal = self.pm._check_stop_loss(state, rule)
        assert signal is None  # 70% < 80%

        state2 = self._make_state(10, 1)  # 90% loss
        signal2 = self.pm._check_stop_loss(state2, rule)
        assert signal2 is not None  # 90% >= 80%

    def test_stop_loss_high_tight(self):
        """HIGH tier has 20% stop — triggers early."""
        state = self._make_state(80, 60)  # 25% loss
        rule = DEFAULT_EXIT_RULES[PriceTier.HIGH]
        signal = self.pm._check_stop_loss(state, rule)
        assert signal is not None

    def test_no_stop_if_profitable(self):
        """Stop loss should not trigger when position is profitable."""
        state = self._make_state(50, 60)  # Profitable
        rule = DEFAULT_EXIT_RULES[PriceTier.MID]
        signal = self.pm._check_stop_loss(state, rule)
        assert signal is None


# ═══════════════════════════════════════════════════════════════════════════════
# Time Decay Logic
# ═══════════════════════════════════════════════════════════════════════════════

class TestTimeDecay:
    """Test time-decay exit logic."""

    def setup_method(self):
        self.pm = PositionManager(paper_trading=True)

    def _make_state(self, entry, hours_held):
        tier = PositionManager.classify_tier(entry)
        return PositionState(
            ticker="TEST-TICKER",
            side="yes",
            quantity=10,
            entry_price=entry,
            current_price=entry,  # Flat
            peak_price=entry,
            tier=tier,
            entry_time=datetime.now(timezone.utc) - timedelta(hours=hours_held),
            last_checked=datetime.now(timezone.utc),
        )

    def test_time_decay_triggered(self):
        """Position held past max hold hours -> exit."""
        # HIGH tier: max 48h, held for 50h
        state = self._make_state(80, 50)
        rule = DEFAULT_EXIT_RULES[PriceTier.HIGH]
        signal = self.pm._check_time_decay(state, rule)
        assert signal is not None
        assert signal.reason == ExitReason.TIME_DECAY

    def test_time_decay_not_triggered(self):
        """Position within hold limit -> no exit."""
        # HIGH tier: max 48h, held for 24h
        state = self._make_state(80, 24)
        rule = DEFAULT_EXIT_RULES[PriceTier.HIGH]
        signal = self.pm._check_time_decay(state, rule)
        assert signal is None

    def test_longshot_long_hold_ok(self):
        """LONGSHOT can hold for 7 days (168h)."""
        state = self._make_state(10, 160)  # 160h < 168h
        rule = DEFAULT_EXIT_RULES[PriceTier.LONGSHOT]
        signal = self.pm._check_time_decay(state, rule)
        assert signal is None

    def test_longshot_too_old(self):
        """LONGSHOT held past 168h -> exit."""
        state = self._make_state(10, 170)  # 170h > 168h
        rule = DEFAULT_EXIT_RULES[PriceTier.LONGSHOT]
        signal = self.pm._check_time_decay(state, rule)
        assert signal is not None

    def test_disabled(self):
        """With time_decay_enabled=False, no time decay signals."""
        pm = PositionManager(time_decay_enabled=False, paper_trading=True)
        state = self._make_state(80, 100)
        rule = DEFAULT_EXIT_RULES[PriceTier.HIGH]
        signal = pm._check_time_decay(state, rule)
        assert signal is None


# ═══════════════════════════════════════════════════════════════════════════════
# Position Manager Integration
# ═══════════════════════════════════════════════════════════════════════════════

class TestPositionManagerIntegration:
    """Test PositionManager initialization and configuration."""

    def test_initializes(self):
        pm = PositionManager(paper_trading=True)
        assert pm.paper_trading is True
        assert pm.trailing_stop_enabled is True
        assert pm.take_profit_enabled is True
        assert pm.time_decay_enabled is True

    def test_custom_exit_rules(self):
        custom = {
            PriceTier.MID: ExitRule(
                tier=PriceTier.MID,
                stop_loss_pct=0.10,  # Very tight
                max_hold_hours=12,
            )
        }
        pm = PositionManager(exit_rules=custom, paper_trading=True)
        assert pm.exit_rules[PriceTier.MID].stop_loss_pct == 0.10

    def test_get_position_manager_singleton(self):
        """Factory function returns same instance."""
        # Reset singleton for test isolation
        import src.execution.position_manager as mod
        mod._position_manager = None
        pm1 = get_position_manager(paper_trading=True)
        pm2 = get_position_manager(paper_trading=True)
        assert pm1 is pm2
        mod._position_manager = None  # Clean up

    def test_get_stats_empty(self):
        pm = PositionManager(paper_trading=True)
        stats = pm.get_stats()
        assert stats["total_exits"] == 0
        assert stats["total_pnl"] == 0.0

    def test_get_exit_history_empty(self):
        pm = PositionManager(paper_trading=True)
        assert pm.get_exit_history() == []

    def test_get_position_states_empty(self):
        pm = PositionManager(paper_trading=True)
        assert pm.get_position_states() == []


# ═══════════════════════════════════════════════════════════════════════════════
# Settings Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestPositionManagerSettings:
    """Test that settings include position management fields."""

    def test_position_check_interval_exists(self):
        from config.settings import Settings
        assert "position_check_interval" in Settings.model_fields

    def test_trailing_stop_enabled_exists(self):
        from config.settings import Settings
        assert "trailing_stop_enabled" in Settings.model_fields

    def test_take_profit_enabled_exists(self):
        from config.settings import Settings
        assert "take_profit_enabled" in Settings.model_fields

    def test_time_decay_exit_enabled_exists(self):
        from config.settings import Settings
        assert "time_decay_exit_enabled" in Settings.model_fields

    def test_max_position_age_hours_exists(self):
        from config.settings import Settings
        assert "max_position_age_hours" in Settings.model_fields


# ═══════════════════════════════════════════════════════════════════════════════
# Exit Signal Dataclass
# ═══════════════════════════════════════════════════════════════════════════════

class TestExitSignal:
    """Test ExitSignal dataclass."""

    def test_creates_correctly(self):
        signal = ExitSignal(
            ticker="TEST",
            side="yes",
            reason=ExitReason.TAKE_PROFIT,
            exit_price=70,
            entry_price=40,
            quantity_to_exit=5,
            total_quantity=10,
            unrealized_pnl=1.50,
            unrealized_pnl_pct=0.375,
            net_profit_after_fees=1.30,
            tier=PriceTier.MID,
            hold_hours=2.5,
            peak_price=75,
            reasoning="Take-profit L1",
        )
        assert signal.ticker == "TEST"
        assert signal.reason == ExitReason.TAKE_PROFIT
        assert signal.quantity_to_exit == 5


# ═══════════════════════════════════════════════════════════════════════════════
# Main.py Wiring Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestMainWiring:
    """Test that position manager is wired into main.py."""

    def test_import_exists(self):
        """PositionManager should be importable from execution module."""
        from src.execution.position_manager import PositionManager, get_position_manager
        assert PositionManager is not None
        assert get_position_manager is not None

    def test_main_has_monitor_method(self):
        """TradingSystem should have _monitor_and_manage_positions method."""
        import inspect
        from src.main import TradingSystem
        assert hasattr(TradingSystem, "_monitor_and_manage_positions")
        assert inspect.iscoroutinefunction(TradingSystem._monitor_and_manage_positions)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
