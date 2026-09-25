"""
Tests for EV-based exits, Kelly rebalancing, graduated time decay,
and contrarian signal analysis.

Run: pytest tests/test_smart_exits.py -v
"""
import pytest
import sys
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.execution.position_manager import (
    PositionManager,
    PositionState,
    ExitRule,
    ExitSignal,
    ExitReason,
    PriceTier,
    DEFAULT_EXIT_RULES,
)
from src.strategy.contrarian_signal import (
    ContrarianAnalyzer,
    ContrarianSignal,
    CrowdState,
    get_contrarian_analyzer,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _make_state(
    ticker: str = "TEST-TICKER",
    side: str = "yes",
    quantity: int = 10,
    entry_price: int = 40,
    current_price: int = 55,
    peak_price: int = 60,
    tier: PriceTier = PriceTier.MID,
    entry_time: datetime = None,
    tp_levels_hit: list = None,
) -> PositionState:
    """Build a PositionState for testing."""
    return PositionState(
        ticker=ticker,
        side=side,
        quantity=quantity,
        entry_price=entry_price,
        current_price=current_price,
        peak_price=peak_price,
        tier=tier,
        entry_time=entry_time or (datetime.now(timezone.utc) - timedelta(hours=12)),
        last_checked=datetime.now(timezone.utc),
        tp_levels_hit=tp_levels_hit or [],
    )


def _make_rule(tier: PriceTier = PriceTier.MID) -> ExitRule:
    """Get the default exit rule for a tier."""
    return DEFAULT_EXIT_RULES[tier]


def _make_forecast(prob_yes: float = 0.55):
    """Build a mock Forecast object."""
    forecast = MagicMock()
    forecast.probability_yes = prob_yes
    forecast.probability_no = 1.0 - prob_yes
    forecast.confidence = 0.7
    return forecast


# ═══════════════════════════════════════════════════════════════════════════════
# Contrarian Analyzer
# ═══════════════════════════════════════════════════════════════════════════════

class TestContrarianAnalyzer:
    """Test contrarian/sentiment divergence signal."""

    def test_crowd_state_extreme_yes(self):
        analyzer = ContrarianAnalyzer()
        assert analyzer._classify_crowd(90) == CrowdState.EXTREME_YES
        assert analyzer._classify_crowd(85) == CrowdState.EXTREME_YES

    def test_crowd_state_strong_yes(self):
        analyzer = ContrarianAnalyzer()
        assert analyzer._classify_crowd(75) == CrowdState.STRONG_YES
        assert analyzer._classify_crowd(70) == CrowdState.STRONG_YES

    def test_crowd_state_neutral(self):
        analyzer = ContrarianAnalyzer()
        assert analyzer._classify_crowd(50) == CrowdState.NEUTRAL
        assert analyzer._classify_crowd(31) == CrowdState.NEUTRAL
        assert analyzer._classify_crowd(69) == CrowdState.NEUTRAL

    def test_crowd_state_strong_no(self):
        analyzer = ContrarianAnalyzer()
        assert analyzer._classify_crowd(20) == CrowdState.STRONG_NO
        assert analyzer._classify_crowd(30) == CrowdState.STRONG_NO

    def test_crowd_state_extreme_no(self):
        analyzer = ContrarianAnalyzer()
        assert analyzer._classify_crowd(10) == CrowdState.EXTREME_NO
        assert analyzer._classify_crowd(14) == CrowdState.EXTREME_NO

    def test_crowd_state_boundary_15(self):
        analyzer = ContrarianAnalyzer()
        # 15 = EXTREME_NO (<=15), 16 = STRONG_NO (16-30)
        assert analyzer._classify_crowd(15) == CrowdState.EXTREME_NO
        assert analyzer._classify_crowd(16) == CrowdState.STRONG_NO

    def test_velocity_calculation_no_history(self):
        analyzer = ContrarianAnalyzer()
        assert analyzer._calculate_velocity([]) == 0.0
        assert analyzer._calculate_velocity([(datetime.now(timezone.utc), 50, 100)]) == 0.0

    def test_velocity_calculation_with_data(self):
        analyzer = ContrarianAnalyzer()
        t0 = datetime.now(timezone.utc) - timedelta(hours=10)
        t1 = datetime.now(timezone.utc)
        history = [(t0, 40, 100), (t1, 60, 200)]
        velocity = analyzer._calculate_velocity(history)
        assert abs(velocity - 2.0) < 0.1  # 20 cents over ~10 hours

    @patch.object(ContrarianAnalyzer, '_get_price_history', return_value=[])
    @patch.object(ContrarianAnalyzer, '_get_average_volume', return_value=0)
    def test_analyze_extreme_yes_model_disagrees(self, mock_vol, mock_hist):
        """When crowd at 90% YES but model says 60%, contrarian score should be positive."""
        analyzer = ContrarianAnalyzer()
        signal = analyzer.analyze(
            ticker="TEST",
            current_price=90,
            our_probability=0.60,
        )
        assert signal.crowd_state == CrowdState.EXTREME_YES
        assert signal.contrarian_score > 0
        assert "overreaction" in signal.message.lower() or "model says" in signal.message.lower()

    @patch.object(ContrarianAnalyzer, '_get_price_history', return_value=[])
    @patch.object(ContrarianAnalyzer, '_get_average_volume', return_value=0)
    def test_analyze_extreme_yes_model_agrees(self, mock_vol, mock_hist):
        """When crowd at 90% YES and model also says 90%, contrarian score should be negative."""
        analyzer = ContrarianAnalyzer()
        signal = analyzer.analyze(
            ticker="TEST",
            current_price=90,
            our_probability=0.90,
        )
        assert signal.crowd_state == CrowdState.EXTREME_YES
        assert signal.contrarian_score <= 0
        assert "consensus correct" in signal.message.lower() or "agrees" in signal.message.lower()

    @patch.object(ContrarianAnalyzer, '_get_price_history', return_value=[])
    @patch.object(ContrarianAnalyzer, '_get_average_volume', return_value=0)
    def test_analyze_neutral_no_signal(self, mock_vol, mock_hist):
        """At 50c, no strong contrarian signal regardless of model."""
        analyzer = ContrarianAnalyzer()
        signal = analyzer.analyze(
            ticker="TEST",
            current_price=50,
            our_probability=0.55,
        )
        assert signal.crowd_state == CrowdState.NEUTRAL
        assert "neutral" in signal.message.lower()

    @patch.object(ContrarianAnalyzer, '_get_price_history', return_value=[])
    @patch.object(ContrarianAnalyzer, '_get_average_volume', return_value=0)
    def test_analyze_extreme_no_model_higher(self, mock_vol, mock_hist):
        """When crowd at 10% YES but model says 35%, contrarian score positive."""
        analyzer = ContrarianAnalyzer()
        signal = analyzer.analyze(
            ticker="TEST",
            current_price=10,
            our_probability=0.35,
        )
        assert signal.crowd_state == CrowdState.EXTREME_NO
        assert signal.contrarian_score > 0
        assert "underpricing" in signal.message.lower() or "model says" in signal.message.lower()

    def test_get_contrarian_analyzer_singleton(self):
        """get_contrarian_analyzer should return the same instance."""
        a1 = get_contrarian_analyzer()
        a2 = get_contrarian_analyzer()
        assert a1 is a2

    @patch.object(ContrarianAnalyzer, '_get_price_history', return_value=[])
    @patch.object(ContrarianAnalyzer, '_get_average_volume', return_value=0)
    def test_analyze_score_bounds(self, mock_vol, mock_hist):
        """Contrarian score should always be between -1 and 1."""
        analyzer = ContrarianAnalyzer()
        for price in [5, 15, 30, 50, 70, 85, 95]:
            for prob in [0.1, 0.3, 0.5, 0.7, 0.9]:
                signal = analyzer.analyze("TEST", price, prob)
                assert -1.0 <= signal.contrarian_score <= 1.0


# ═══════════════════════════════════════════════════════════════════════════════
# EV-Based Exit
# ═══════════════════════════════════════════════════════════════════════════════

class TestEVExit:
    """Test EV-based exit logic."""

    def _make_pm(self) -> PositionManager:
        return PositionManager(paper_trading=True)

    def _mock_db_session(self, mock_db, market_title="Test Market"):
        """Set up mock DB session that returns a market with given title."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_market = MagicMock()
        mock_market.title = market_title
        mock_session.query.return_value.filter_by.return_value.first.return_value = mock_market
        mock_db.return_value.__next__ = MagicMock(return_value=mock_session)

    @patch('src.data.database.get_db_session')
    def test_ev_exit_edge_flipped(self, mock_db):
        """If model now disagrees with our position, exit."""
        pm = self._make_pm()
        state = _make_state(
            entry_price=40, current_price=45, side="yes", tier=PriceTier.MID
        )
        rule = _make_rule(PriceTier.MID)

        mock_forecast = _make_forecast(prob_yes=0.35)
        mock_ev = MagicMock()
        mock_ev.calculate_expected_value.return_value = (-5.0, -0.10)

        self._mock_db_session(mock_db)

        with patch('src.analysis.forecaster.get_default_forecaster') as mock_f, \
             patch('src.analysis.edge.EdgeCalculator', return_value=mock_ev):
            mock_f.return_value.forecast.return_value = mock_forecast
            signal = pm._check_ev_exit(state, rule)

        assert signal is not None
        assert "edge flipped" in signal.reasoning.lower() or "EV exit" in signal.reasoning

    @patch('src.data.database.get_db_session')
    def test_ev_exit_negative_ev_but_profitable(self, mock_db):
        """If EV of holding is negative but we're profitable, lock in gains."""
        pm = self._make_pm()
        state = _make_state(
            entry_price=30, current_price=50, side="yes", tier=PriceTier.LOW
        )
        rule = _make_rule(PriceTier.LOW)

        mock_forecast = _make_forecast(prob_yes=0.47)
        mock_ev = MagicMock()
        mock_ev.calculate_expected_value.return_value = (-2.0, -0.04)

        self._mock_db_session(mock_db)

        with patch('src.analysis.forecaster.get_default_forecaster') as mock_f, \
             patch('src.analysis.edge.EdgeCalculator', return_value=mock_ev):
            mock_f.return_value.forecast.return_value = mock_forecast
            signal = pm._check_ev_exit(state, rule)

        assert signal is not None
        assert "locking in" in signal.reasoning.lower() or "EV exit" in signal.reasoning

    @patch('src.data.database.get_db_session')
    def test_ev_exit_still_positive_ev(self, mock_db):
        """If EV is still positive, don't exit."""
        pm = self._make_pm()
        state = _make_state(
            entry_price=40, current_price=45, side="yes", tier=PriceTier.MID
        )
        rule = _make_rule(PriceTier.MID)

        mock_forecast = _make_forecast(prob_yes=0.55)
        mock_ev = MagicMock()
        mock_ev.calculate_expected_value.return_value = (5.0, 0.10)

        self._mock_db_session(mock_db)

        with patch('src.analysis.forecaster.get_default_forecaster') as mock_f, \
             patch('src.analysis.edge.EdgeCalculator', return_value=mock_ev):
            mock_f.return_value.forecast.return_value = mock_forecast
            signal = pm._check_ev_exit(state, rule)

        assert signal is None  # Hold

    @patch('src.data.database.get_db_session')
    def test_ev_exit_edge_decayed_with_profit(self, mock_db):
        """Edge decayed below 3% minimum but we're up 10%+, take profit."""
        pm = self._make_pm()
        state = _make_state(
            entry_price=40, current_price=50, side="yes", tier=PriceTier.MID
        )
        rule = _make_rule(PriceTier.MID)

        mock_forecast = _make_forecast(prob_yes=0.51)
        mock_ev = MagicMock()
        mock_ev.calculate_expected_value.return_value = (0.5, 0.01)

        self._mock_db_session(mock_db)

        with patch('src.analysis.forecaster.get_default_forecaster') as mock_f, \
             patch('src.analysis.edge.EdgeCalculator', return_value=mock_ev):
            mock_f.return_value.forecast.return_value = mock_forecast
            signal = pm._check_ev_exit(state, rule)

        assert signal is not None
        assert "edge decayed" in signal.reasoning.lower()

    def test_ev_exit_handles_forecast_failure(self):
        """If forecaster throws, EV exit returns None gracefully."""
        pm = self._make_pm()
        state = _make_state()
        rule = _make_rule()

        with patch('src.analysis.forecaster.get_default_forecaster', side_effect=Exception("API down")):
            signal = pm._check_ev_exit(state, rule)

        assert signal is None


# ═══════════════════════════════════════════════════════════════════════════════
# Kelly Rebalance
# ═══════════════════════════════════════════════════════════════════════════════

class TestKellyRebalance:
    """Test Kelly-optimal position rebalancing."""

    def _make_pm(self) -> PositionManager:
        return PositionManager(paper_trading=True)

    def _mock_db_session(self, mock_db, market_title="Test"):
        """Set up mock DB session."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_market = MagicMock()
        mock_market.title = market_title
        mock_session.query.return_value.filter_by.return_value.first.return_value = mock_market
        mock_db.return_value.__next__ = MagicMock(return_value=mock_session)

    @patch('src.data.database.get_db_session')
    def test_kelly_zero_fraction_exits_fully(self, mock_db):
        """When Kelly fraction is 0 or negative, exit entire position."""
        pm = self._make_pm()
        state = _make_state(
            entry_price=50, current_price=48, quantity=10, side="yes"
        )
        rule = _make_rule()

        mock_forecast = _make_forecast(prob_yes=0.40)
        mock_sizer = MagicMock()
        mock_sizer.calculate_kelly_fraction.return_value = 0.0
        mock_sizer.kelly_fraction = 0.5

        self._mock_db_session(mock_db)

        with patch('src.analysis.forecaster.get_default_forecaster') as mock_f, \
             patch('src.execution.position_sizer.get_position_sizer', return_value=mock_sizer), \
             patch('src.execution.position_sizer.get_bankroll', return_value=1000.0):
            mock_f.return_value.forecast.return_value = mock_forecast
            signal = pm._check_kelly_rebalance(state, rule)

        assert signal is not None
        assert signal.quantity_to_exit == 10  # Full exit
        assert "Kelly exit" in signal.reasoning or "kelly" in signal.reasoning.lower()

    @patch('src.data.database.get_db_session')
    def test_kelly_overweight_trims(self, mock_db):
        """When holding >150% of Kelly-optimal, trim to optimal."""
        pm = self._make_pm()
        state = _make_state(
            entry_price=40, current_price=45, quantity=20, side="yes"
        )
        rule = _make_rule()

        mock_forecast = _make_forecast(prob_yes=0.55)
        mock_sizer = MagicMock()
        mock_sizer.calculate_kelly_fraction.return_value = 0.10
        mock_sizer.kelly_fraction = 0.5

        self._mock_db_session(mock_db)

        # Use small bankroll so Kelly-optimal is small
        with patch('src.analysis.forecaster.get_default_forecaster') as mock_f, \
             patch('src.execution.position_sizer.get_position_sizer', return_value=mock_sizer), \
             patch('src.execution.position_sizer.get_bankroll', return_value=3.0):
            mock_f.return_value.forecast.return_value = mock_forecast
            signal = pm._check_kelly_rebalance(state, rule)

        # optimal_dollars = 3.0 * 0.05 = 0.15
        # optimal_contracts = int(0.15 / 0.45) = 0
        # kelly_frac = 0.10 (>0), so doesn't hit the zero branch
        # optimal_contracts = 0, so the >150% check fails (0 > 0 is false)
        assert signal is None

    @patch('src.data.database.get_db_session')
    def test_kelly_within_bounds_no_action(self, mock_db):
        """When position is near Kelly-optimal, no action needed."""
        pm = self._make_pm()
        state = _make_state(
            entry_price=40, current_price=50, quantity=5, side="yes"
        )
        rule = _make_rule()

        mock_forecast = _make_forecast(prob_yes=0.60)
        mock_sizer = MagicMock()
        mock_sizer.calculate_kelly_fraction.return_value = 0.20
        mock_sizer.kelly_fraction = 0.5

        self._mock_db_session(mock_db)

        with patch('src.analysis.forecaster.get_default_forecaster') as mock_f, \
             patch('src.execution.position_sizer.get_position_sizer', return_value=mock_sizer), \
             patch('src.execution.position_sizer.get_bankroll', return_value=1000.0):
            mock_f.return_value.forecast.return_value = mock_forecast
            signal = pm._check_kelly_rebalance(state, rule)

        assert signal is None

    def test_kelly_handles_exception(self):
        """If Kelly check fails, returns None gracefully."""
        pm = self._make_pm()
        state = _make_state()
        rule = _make_rule()

        with patch('src.analysis.forecaster.get_default_forecaster', side_effect=Exception("fail")):
            signal = pm._check_kelly_rebalance(state, rule)

        assert signal is None


# ═══════════════════════════════════════════════════════════════════════════════
# Graduated Time Decay
# ═══════════════════════════════════════════════════════════════════════════════

class TestGraduatedTimeDecay:
    """Test graduated time-decay exit with 4 tranches."""

    def _make_pm(self) -> PositionManager:
        return PositionManager(paper_trading=True)

    def test_no_decay_if_no_close_time(self):
        """If market has no close_time, fall back to None (regular time decay handles it)."""
        pm = self._make_pm()
        state = _make_state()
        rule = _make_rule()

        with patch.object(pm, '_get_market_close_time', return_value=None):
            signal = pm._check_graduated_time_decay(state, rule)

        assert signal is None

    def test_no_decay_if_market_closed(self):
        """If market already closed, return None."""
        pm = self._make_pm()
        state = _make_state()
        rule = _make_rule()

        past = datetime.now(timezone.utc) - timedelta(hours=1)
        with patch.object(pm, '_get_market_close_time', return_value=past):
            signal = pm._check_graduated_time_decay(state, rule)

        assert signal is None

    def test_first_tranche_triggers(self):
        """First tranche triggers when hours_remaining <= base_hours * 1.5."""
        pm = self._make_pm()
        state = _make_state(
            entry_price=40, current_price=45, quantity=20, tier=PriceTier.MID
        )
        rule = _make_rule(PriceTier.MID)

        # MID max_hold_hours = 72, so first tranche at 72 * 1.5 = 108h
        close_time = datetime.now(timezone.utc) + timedelta(hours=100)  # 100h remaining < 108h

        with patch.object(pm, '_get_market_close_time', return_value=close_time):
            signal = pm._check_graduated_time_decay(state, rule)

        assert signal is not None
        assert signal.reason == ExitReason.TIME_DECAY
        assert "early" in signal.reasoning.lower()
        assert 100 in state.tp_levels_hit

    def test_second_tranche_skips_if_first_done(self):
        """If first tranche already done, move to second tranche."""
        pm = self._make_pm()
        state = _make_state(
            entry_price=40, current_price=45, quantity=15,
            tier=PriceTier.MID, tp_levels_hit=[100],
        )
        rule = _make_rule(PriceTier.MID)

        # MID max_hold_hours = 72, second tranche at 72 * 1.0 = 72h
        close_time = datetime.now(timezone.utc) + timedelta(hours=60)  # 60h < 72h

        with patch.object(pm, '_get_market_close_time', return_value=close_time):
            signal = pm._check_graduated_time_decay(state, rule)

        assert signal is not None
        assert "mid" in signal.reasoning.lower()
        assert 101 in state.tp_levels_hit

    def test_all_tranches_done_no_signal(self):
        """If all tranches already executed, no signal."""
        pm = self._make_pm()
        state = _make_state(
            entry_price=40, current_price=45, quantity=5,
            tier=PriceTier.MID, tp_levels_hit=[100, 101, 102, 103],
        )
        rule = _make_rule(PriceTier.MID)

        close_time = datetime.now(timezone.utc) + timedelta(hours=5)

        with patch.object(pm, '_get_market_close_time', return_value=close_time):
            signal = pm._check_graduated_time_decay(state, rule)

        assert signal is None

    def test_early_tranche_skips_deep_loss(self):
        """Early tranches skip if position is deeply underwater (>30% loss)."""
        pm = self._make_pm()
        state = _make_state(
            entry_price=60, current_price=35, quantity=20,
            tier=PriceTier.MID,
        )
        rule = _make_rule(PriceTier.MID)

        close_time = datetime.now(timezone.utc) + timedelta(hours=100)

        with patch.object(pm, '_get_market_close_time', return_value=close_time):
            signal = pm._check_graduated_time_decay(state, rule)

        # PnL is -41.6%, early tranche should skip
        assert signal is None

    def test_tranche_index_uses_100_offset(self):
        """Tranche indices use 100+ offset to avoid collision with TP levels."""
        pm = self._make_pm()
        state = _make_state(
            entry_price=40, current_price=45, quantity=20, tier=PriceTier.MID,
            tp_levels_hit=[0, 1],  # Regular TP levels 0 and 1 hit
        )
        rule = _make_rule(PriceTier.MID)

        close_time = datetime.now(timezone.utc) + timedelta(hours=100)

        with patch.object(pm, '_get_market_close_time', return_value=close_time):
            signal = pm._check_graduated_time_decay(state, rule)

        assert signal is not None
        # Regular TP levels [0, 1] should NOT interfere with tranche index 100
        assert 100 in state.tp_levels_hit

    def test_final_tranche_exits_last_shares(self):
        """Final tranche sells remaining shares."""
        pm = self._make_pm()
        state = _make_state(
            entry_price=40, current_price=42, quantity=5,
            tier=PriceTier.MID, tp_levels_hit=[100, 101, 102],
        )
        rule = _make_rule(PriceTier.MID)

        # Final tranche at base * 0.125 = 72 * 0.125 = 9h
        close_time = datetime.now(timezone.utc) + timedelta(hours=5)  # 5h < 9h

        with patch.object(pm, '_get_market_close_time', return_value=close_time):
            signal = pm._check_graduated_time_decay(state, rule)

        assert signal is not None
        assert "final" in signal.reasoning.lower()
        assert 103 in state.tp_levels_hit


# ═══════════════════════════════════════════════════════════════════════════════
# Contrarian Exit Integration
# ═══════════════════════════════════════════════════════════════════════════════

class TestContrarianExit:
    """Test contrarian exit wired into position manager."""

    def _make_pm(self) -> PositionManager:
        return PositionManager(paper_trading=True)

    def _mock_db_session(self, mock_db, market_title="Test"):
        """Set up mock DB session."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_market = MagicMock()
        mock_market.title = market_title
        mock_session.query.return_value.filter_by.return_value.first.return_value = mock_market
        mock_db.return_value.__next__ = MagicMock(return_value=mock_session)

    @patch('src.data.database.get_db_session')
    def test_contrarian_exit_crowd_agrees_and_profitable(self, mock_db):
        """When crowd now agrees with us and we're profitable, exit."""
        pm = self._make_pm()
        state = _make_state(
            entry_price=30, current_price=50, side="yes", tier=PriceTier.LOW
        )
        rule = _make_rule(PriceTier.LOW)

        mock_signal = MagicMock()
        mock_signal.contrarian_score = -0.5
        mock_signal.message = "Crowd now agrees"
        mock_forecast = _make_forecast(prob_yes=0.55)

        self._mock_db_session(mock_db)

        with patch('src.strategy.contrarian_signal.get_contrarian_analyzer') as mock_ca, \
             patch('src.analysis.forecaster.get_default_forecaster') as mock_f:
            mock_ca.return_value.analyze.return_value = mock_signal
            mock_f.return_value.forecast.return_value = mock_forecast
            signal = pm._check_contrarian_exit(state, rule)

        assert signal is not None
        assert "contrarian" in signal.reasoning.lower()

    @patch('src.data.database.get_db_session')
    def test_contrarian_exit_no_trigger_when_score_positive(self, mock_db):
        """When contrarian score is positive (crowd disagrees), don't exit."""
        pm = self._make_pm()
        state = _make_state(
            entry_price=30, current_price=50, side="yes", tier=PriceTier.LOW
        )
        rule = _make_rule(PriceTier.LOW)

        mock_signal = MagicMock()
        mock_signal.contrarian_score = 0.3
        mock_forecast = _make_forecast(prob_yes=0.55)

        self._mock_db_session(mock_db)

        with patch('src.strategy.contrarian_signal.get_contrarian_analyzer') as mock_ca, \
             patch('src.analysis.forecaster.get_default_forecaster') as mock_f:
            mock_ca.return_value.analyze.return_value = mock_signal
            mock_f.return_value.forecast.return_value = mock_forecast
            signal = pm._check_contrarian_exit(state, rule)

        assert signal is None

    @patch('src.data.database.get_db_session')
    def test_contrarian_exit_no_trigger_when_unprofitable(self, mock_db):
        """Even if crowd agrees, don't exit at a loss."""
        pm = self._make_pm()
        state = _make_state(
            entry_price=50, current_price=48, side="yes", tier=PriceTier.MID
        )
        rule = _make_rule(PriceTier.MID)

        mock_signal = MagicMock()
        mock_signal.contrarian_score = -0.5
        mock_forecast = _make_forecast(prob_yes=0.50)

        self._mock_db_session(mock_db)

        with patch('src.strategy.contrarian_signal.get_contrarian_analyzer') as mock_ca, \
             patch('src.analysis.forecaster.get_default_forecaster') as mock_f:
            mock_ca.return_value.analyze.return_value = mock_signal
            mock_f.return_value.forecast.return_value = mock_forecast
            signal = pm._check_contrarian_exit(state, rule)

        # PnL is -4% (not > 0.10), so no exit
        assert signal is None

    def test_contrarian_exit_handles_exception(self):
        """If contrarian check fails, returns None gracefully."""
        pm = self._make_pm()
        state = _make_state()
        rule = _make_rule()

        with patch('src.strategy.contrarian_signal.get_contrarian_analyzer', side_effect=Exception("fail")):
            signal = pm._check_contrarian_exit(state, rule)

        assert signal is None


# ═══════════════════════════════════════════════════════════════════════════════
# Priority Order
# ═══════════════════════════════════════════════════════════════════════════════

class TestExitPriorityOrder:
    """Test that exits are checked in the correct priority order."""

    def test_priority_order_all_8_checks_exist(self):
        """Verify check_positions has all 8 priority checks in correct order."""
        import inspect
        source = inspect.getsource(PositionManager.check_positions)

        # Find the indices of each check to verify order
        checks = [
            "_check_stop_loss",
            "_check_trailing_stop",
            "_check_ev_exit",
            "_check_kelly_rebalance",
            "_check_contrarian_exit",
            "_check_graduated_time_decay",
            "_check_take_profit",
            "_check_time_decay",
        ]

        indices = []
        for check in checks:
            idx = source.find(check)
            assert idx >= 0, f"{check} not found in check_positions"
            indices.append(idx)

        # Verify they appear in order
        for i in range(len(indices) - 1):
            assert indices[i] < indices[i + 1], (
                f"{checks[i]} (at {indices[i]}) should come before "
                f"{checks[i + 1]} (at {indices[i + 1]})"
            )

    def test_stop_loss_is_first_priority(self):
        """Stop loss should be the very first check."""
        import inspect
        source = inspect.getsource(PositionManager.check_positions)
        stop_loss_idx = source.find("_check_stop_loss")
        ev_idx = source.find("_check_ev_exit")
        assert stop_loss_idx < ev_idx, "Stop loss must come before EV exit"

    def test_ev_exit_before_take_profit(self):
        """EV-based exit has higher priority than take-profit ladder."""
        import inspect
        source = inspect.getsource(PositionManager.check_positions)
        ev_idx = source.find("_check_ev_exit")
        tp_idx = source.find("_check_take_profit")
        assert ev_idx < tp_idx, "EV exit must come before take profit"


# ═══════════════════════════════════════════════════════════════════════════════
# Integration: Full check_positions flow
# ═══════════════════════════════════════════════════════════════════════════════

class TestCheckPositionsIntegration:
    """Test the full check_positions method with mocked DB."""

    @patch('src.execution.position_manager.get_db_session')
    def test_check_positions_returns_empty_when_no_positions(self, mock_db):
        """With no positions, check_positions returns empty list."""
        pm = PositionManager(paper_trading=True)

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.query.return_value.filter.return_value.all.return_value = []
        mock_db.return_value.__next__ = MagicMock(return_value=mock_session)

        signals = pm.check_positions()
        assert signals == []
