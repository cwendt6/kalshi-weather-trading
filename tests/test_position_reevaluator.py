"""Tests for position re-evaluator (quick scalp exit engine)."""
import pytest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch, PropertyMock

from src.execution.position_reevaluator import (
    ExitDecision,
    ExitReason,
    PositionReEvaluator,
    get_position_reevaluator,
)
from src.utils.fees import KALSHI_WINNER_FEE_RATE


# ─── Unit tests for P&L calculation ─────────────────────────────────────


class TestPnLCalculation:
    """Test core P&L math."""

    def test_yes_side_profit(self):
        """YES position: current > entry = profit."""
        assert PositionReEvaluator._calculate_pnl_cents("yes", 50, 55) == 5

    def test_yes_side_loss(self):
        """YES position: current < entry = loss."""
        assert PositionReEvaluator._calculate_pnl_cents("yes", 50, 42) == -8

    def test_yes_side_flat(self):
        """YES position: current == entry = no change."""
        assert PositionReEvaluator._calculate_pnl_cents("yes", 50, 50) == 0

    def test_no_side_profit(self):
        """NO position: current < entry = profit (price dropped = NO wins)."""
        assert PositionReEvaluator._calculate_pnl_cents("no", 50, 45) == 5

    def test_no_side_loss(self):
        """NO position: current > entry = loss."""
        assert PositionReEvaluator._calculate_pnl_cents("no", 50, 58) == -8

    def test_no_side_flat(self):
        """NO position: current == entry = no change."""
        assert PositionReEvaluator._calculate_pnl_cents("no", 50, 50) == 0


class TestNetProfitCalculation:
    """Test net profit after fees."""

    def test_profitable_yes_trade(self):
        """Net profit should subtract 2% fee on winning YES trades."""
        # 100 contracts, entry 50c, exit 55c
        # Gross = 100 * (55-50)/100 = $5.00
        # Fee = $5.00 * 0.02 = $0.10
        # Net = $4.90
        net = PositionReEvaluator._calculate_net_profit("yes", 100, 50, 55)
        assert abs(net - 4.90) < 0.01

    def test_profitable_no_trade(self):
        """Net profit on winning NO trade."""
        # 100 contracts, entry 50c, exit 45c (price dropped, NO wins)
        # Gross = 100 * (50-45)/100 = $5.00
        # Fee = $5.00 * 0.02 = $0.10
        # Net = $4.90
        net = PositionReEvaluator._calculate_net_profit("no", 100, 50, 45)
        assert abs(net - 4.90) < 0.01

    def test_losing_trade_no_fee(self):
        """No fee on losing trades."""
        # 100 contracts, entry 50c, exit 42c = -$8 loss
        # Fee = $0 (only winners pay)
        net = PositionReEvaluator._calculate_net_profit("yes", 100, 50, 42)
        assert abs(net - (-8.0)) < 0.01

    def test_breakeven_no_fee(self):
        """Exactly breakeven = no fee (gross 0, not positive)."""
        net = PositionReEvaluator._calculate_net_profit("yes", 100, 50, 50)
        assert net == 0.0

    def test_single_contract_profit(self):
        """Fee on a single contract profit."""
        # 1 contract, 50c -> 65c = $0.15 gross
        # Fee = $0.15 * 0.02 = $0.003
        # Net = $0.147
        net = PositionReEvaluator._calculate_net_profit("yes", 1, 50, 65)
        assert abs(net - 0.147) < 0.001

    def test_large_position_profit(self):
        """Fee calculation on large position."""
        # 1000 contracts, 30c -> 45c = $150 gross
        # Fee = $150 * 0.02 = $3.00
        # Net = $147.00
        net = PositionReEvaluator._calculate_net_profit("yes", 1000, 30, 45)
        assert abs(net - 147.0) < 0.01


# ─── Exit decision logic tests ──────────────────────────────────────────


class TestExitDecisions:
    """Test exit decision logic without DB access."""

    def setup_method(self):
        """Create a re-evaluator with mocked dependencies."""
        with patch("src.execution.position_reevaluator.get_db_session"):
            self.reevaluator = PositionReEvaluator(paper_trading=True)

    def _make_position_data(
        self,
        ticker="TEST-MARKET",
        side="yes",
        quantity=100,
        entry_price=50,
        current_price=55,
        hours_ago=2.0,
    ):
        """Create mock position data dict."""
        return {
            "ticker": ticker,
            "side": side,
            "quantity": quantity,
            "entry_price": entry_price,
            "current_price": current_price,
            "created_at": datetime.now(timezone.utc) - timedelta(hours=hours_ago),
        }

    def test_stop_loss_triggers(self):
        """Stop-loss should trigger at -8c."""
        pos = self._make_position_data(entry_price=50, current_price=42)
        decision = self.reevaluator._evaluate_position(pos)
        assert decision.should_exit is True
        assert decision.reason == ExitReason.STOP_LOSS

    def test_stop_loss_boundary(self):
        """Stop-loss should NOT trigger at -7c."""
        pos = self._make_position_data(entry_price=50, current_price=43)
        decision = self.reevaluator._evaluate_position(pos)
        assert decision.should_exit is False

    def test_tier1_profit_takes(self):
        """Tier 1: +5c gain should trigger partial exit."""
        pos = self._make_position_data(entry_price=50, current_price=55, quantity=100)
        decision = self.reevaluator._evaluate_position(pos)
        assert decision.should_exit is True
        assert decision.reason == ExitReason.PROFIT_TARGET_TIER1
        assert decision.quantity == 25  # 25% of 100

    def test_tier2_profit_takes(self):
        """Tier 2: +10c gain should trigger larger partial exit."""
        pos = self._make_position_data(entry_price=50, current_price=60, quantity=100)
        decision = self.reevaluator._evaluate_position(pos)
        assert decision.should_exit is True
        assert decision.reason == ExitReason.PROFIT_TARGET_TIER2
        assert decision.quantity == 50  # 50% of 100

    def test_tier3_profit_full_exit(self):
        """Tier 3: +15c gain should trigger full exit."""
        pos = self._make_position_data(entry_price=50, current_price=65, quantity=100)
        decision = self.reevaluator._evaluate_position(pos)
        assert decision.should_exit is True
        assert decision.reason == ExitReason.PROFIT_TARGET_TIER3
        assert decision.quantity == 100  # Full exit

    def test_no_side_stop_loss(self):
        """NO side stop-loss triggers when price goes UP."""
        # NO side: entry 50c, current 58c = -8c loss for NO
        pos = self._make_position_data(side="no", entry_price=50, current_price=58)
        decision = self.reevaluator._evaluate_position(pos)
        assert decision.should_exit is True
        assert decision.reason == ExitReason.STOP_LOSS

    def test_no_side_profit_taking(self):
        """NO side profit-taking triggers when price goes DOWN."""
        # NO side: entry 50c, current 45c = +5c profit for NO
        pos = self._make_position_data(side="no", entry_price=50, current_price=45, quantity=100)
        decision = self.reevaluator._evaluate_position(pos)
        assert decision.should_exit is True
        assert decision.reason == ExitReason.PROFIT_TARGET_TIER1

    def test_no_exit_when_flat(self):
        """No exit signal when position is flat (no gain/loss)."""
        pos = self._make_position_data(entry_price=50, current_price=50)
        decision = self.reevaluator._evaluate_position(pos)
        assert decision.should_exit is False

    def test_no_exit_small_gain(self):
        """No exit when gain is < Tier 1 threshold."""
        pos = self._make_position_data(entry_price=50, current_price=53)
        decision = self.reevaluator._evaluate_position(pos)
        assert decision.should_exit is False

    def test_no_price_available(self):
        """Should not exit when no current price is available."""
        pos = self._make_position_data(current_price=None)
        decision = self.reevaluator._evaluate_position(pos)
        assert decision.should_exit is False
        assert "No current price" in decision.reasoning

    def test_grace_period_prevents_exit(self):
        """New positions within grace period should not exit."""
        pos = self._make_position_data(
            entry_price=50,
            current_price=42,  # Would be stop-loss
            hours_ago=0.05,  # 3 minutes
        )
        decision = self.reevaluator._evaluate_position(pos)
        assert decision.should_exit is False
        assert "Grace period" in decision.reasoning

    def test_force_exit_after_48h(self):
        """Force exit positions older than 48h with weak edge."""
        pos = self._make_position_data(
            entry_price=50,
            current_price=51,  # Small gain, not enough for tier1
            hours_ago=49.0,
        )
        with patch.object(self.reevaluator, "_get_edge_estimate", return_value=0.02):
            decision = self.reevaluator._evaluate_position(pos)
        assert decision.should_exit is True
        assert decision.reason == ExitReason.TIME_DECAY_48H

    def test_no_force_exit_with_strong_edge(self):
        """Don't force exit 48h+ positions if edge is strong (>10%)."""
        pos = self._make_position_data(
            entry_price=50,
            current_price=51,
            hours_ago=49.0,
        )
        with patch.object(self.reevaluator, "_get_edge_estimate", return_value=0.15):
            decision = self.reevaluator._evaluate_position(pos)
        # With strong edge, won't force exit — but still below tier1 profit, so no exit
        assert decision.should_exit is False

    def test_edge_erosion_at_24h(self):
        """Edge erosion check triggers after 24h when edge < 3%."""
        pos = self._make_position_data(
            entry_price=50,
            current_price=51,
            hours_ago=25.0,
        )
        with patch.object(self.reevaluator, "_get_edge_estimate", return_value=0.01):
            decision = self.reevaluator._evaluate_position(pos)
        assert decision.should_exit is True
        assert decision.reason == ExitReason.EDGE_ERODED

    def test_no_edge_erosion_with_good_edge(self):
        """No edge erosion exit when edge is still healthy."""
        pos = self._make_position_data(
            entry_price=50,
            current_price=51,
            hours_ago=25.0,
        )
        with patch.object(self.reevaluator, "_get_edge_estimate", return_value=0.08):
            decision = self.reevaluator._evaluate_position(pos)
        assert decision.should_exit is False

    def test_stop_loss_takes_priority_over_time_decay(self):
        """Stop-loss should fire before time decay even on old positions."""
        pos = self._make_position_data(
            entry_price=50,
            current_price=42,
            hours_ago=49.0,
        )
        decision = self.reevaluator._evaluate_position(pos)
        assert decision.should_exit is True
        assert decision.reason == ExitReason.STOP_LOSS  # Not TIME_DECAY_48H

    def test_profit_tier3_takes_priority_over_time_decay(self):
        """Large profit should fire before time-based exits."""
        pos = self._make_position_data(
            entry_price=50,
            current_price=65,
            hours_ago=49.0,
        )
        # Force exit check runs first (before profit tiers), but profit tier3 should win
        # because force-exit only fires when edge < 10%
        with patch.object(self.reevaluator, "_get_edge_estimate", return_value=0.20):
            decision = self.reevaluator._evaluate_position(pos)
        assert decision.should_exit is True
        assert decision.reason == ExitReason.PROFIT_TARGET_TIER3

    def test_partial_exit_quantity_minimum(self):
        """Partial exit should always exit at least 1 contract."""
        pos = self._make_position_data(
            entry_price=50,
            current_price=55,
            quantity=2,  # Small position
        )
        decision = self.reevaluator._evaluate_position(pos)
        assert decision.should_exit is True
        assert decision.quantity >= 1

    def test_decision_includes_financial_details(self):
        """Exit decision should include all financial fields."""
        pos = self._make_position_data(entry_price=50, current_price=60, quantity=10)
        decision = self.reevaluator._evaluate_position(pos)
        assert decision.should_exit is True
        assert decision.entry_price_cents == 50
        assert decision.current_price_cents == 60
        assert decision.unrealized_pnl_dollars is not None
        assert decision.unrealized_pnl_pct is not None
        assert decision.net_profit_after_fees is not None
        assert decision.hours_held is not None


# ─── Get current price tests ────────────────────────────────────────────


class TestGetCurrentPrice:
    """Test price retrieval helper."""

    def test_mid_price_from_bid_ask(self):
        """Should return mid-price from bid/ask."""
        session = MagicMock()
        mock_price = MagicMock()
        mock_price.yes_bid = 48
        mock_price.yes_ask = 52
        session.query.return_value.filter.return_value.order_by.return_value.first.return_value = mock_price

        price = PositionReEvaluator._get_current_price(session, "TEST")
        assert price == 50

    def test_bid_only(self):
        """Should return bid if no ask."""
        session = MagicMock()
        mock_price = MagicMock()
        mock_price.yes_bid = 48
        mock_price.yes_ask = 0
        session.query.return_value.filter.return_value.order_by.return_value.first.return_value = mock_price

        price = PositionReEvaluator._get_current_price(session, "TEST")
        assert price == 48

    def test_ask_only(self):
        """Should return ask if no bid."""
        session = MagicMock()
        mock_price = MagicMock()
        mock_price.yes_bid = 0
        mock_price.yes_ask = 52
        session.query.return_value.filter.return_value.order_by.return_value.first.return_value = mock_price

        price = PositionReEvaluator._get_current_price(session, "TEST")
        assert price == 52

    def test_no_price_data(self):
        """Should return None when no price data exists."""
        session = MagicMock()
        session.query.return_value.filter.return_value.order_by.return_value.first.return_value = None

        price = PositionReEvaluator._get_current_price(session, "TEST")
        assert price is None


# ─── Evaluate all positions (integration-style with mocked DB) ───────────


def _mock_db_session(mock_session):
    """Helper to properly mock get_db_session() used as next(get_db_session())."""
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_gen = MagicMock()
    mock_gen.__next__ = MagicMock(return_value=mock_session)
    return mock_gen


class TestEvaluateAllPositions:
    """Test evaluate_all_positions with mocked DB access."""

    @patch("src.execution.position_reevaluator.get_db_session")
    def test_evaluates_open_positions(self, mock_get_session):
        """Should evaluate all positions with quantity > 0."""
        mock_pos = MagicMock()
        mock_pos.ticker = "TEST-1"
        mock_pos.side = "yes"
        mock_pos.quantity = 100
        mock_pos.average_price = 50
        mock_pos.created_at = datetime.now(timezone.utc) - timedelta(hours=2)

        mock_price = MagicMock()
        mock_price.yes_bid = 54
        mock_price.yes_ask = 56

        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.all.return_value = [mock_pos]
        mock_session.query.return_value.filter.return_value.order_by.return_value.first.return_value = mock_price

        mock_get_session.return_value = _mock_db_session(mock_session)

        reevaluator = PositionReEvaluator(paper_trading=True)
        decisions = reevaluator.evaluate_all_positions()

        # +5c gain should trigger tier 1
        assert len(decisions) == 1
        assert decisions[0].ticker == "TEST-1"
        assert decisions[0].reason == ExitReason.PROFIT_TARGET_TIER1

    @patch("src.execution.position_reevaluator.get_db_session")
    def test_no_positions_returns_empty(self, mock_get_session):
        """Should return empty list when no positions exist."""
        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.all.return_value = []

        mock_get_session.return_value = _mock_db_session(mock_session)

        reevaluator = PositionReEvaluator(paper_trading=True)
        decisions = reevaluator.evaluate_all_positions()
        assert decisions == []


# ─── Execute exit tests ──────────────────────────────────────────────────


class TestExecuteExit:
    """Test exit execution with mocked DB."""

    @patch("src.execution.position_reevaluator.get_db_session")
    def test_execute_exit_creates_trade_and_updates_position(self, mock_get_session):
        """Exit should create TradeDB and reduce PositionDB quantity."""
        mock_session = MagicMock()
        mock_position = MagicMock()
        mock_position.ticker = "TEST-1"
        mock_position.quantity = 100
        mock_position.average_price = 50
        mock_position.side = "yes"

        mock_price = MagicMock()
        mock_price.yes_bid = 54
        mock_price.yes_ask = 56

        def query_side_effect(model):
            mock_q = MagicMock()
            if hasattr(model, "__tablename__"):
                if model.__tablename__ == "positions":
                    mock_q.filter.return_value.first.return_value = mock_position
                elif model.__tablename__ == "prices":
                    mock_q.filter.return_value.order_by.return_value.first.return_value = mock_price
            return mock_q

        mock_session.query.side_effect = query_side_effect
        mock_get_session.return_value = _mock_db_session(mock_session)

        reevaluator = PositionReEvaluator(paper_trading=True)

        decision = ExitDecision(
            ticker="TEST-1",
            side="yes",
            quantity=25,
            should_exit=True,
            reason=ExitReason.PROFIT_TARGET_TIER1,
            entry_price_cents=50,
            current_price_cents=55,
            hours_held=2.0,
        )

        success = reevaluator.execute_exit(decision)
        assert success is True

        # Verify trade was added
        mock_session.add.assert_called_once()
        trade_arg = mock_session.add.call_args[0][0]
        assert trade_arg.action == "sell"
        assert trade_arg.quantity == 25
        assert "REEVAL" in trade_arg.order_id

        # Position quantity should be reduced (100 - 25 = 75)
        assert mock_position.quantity == 75

    @patch("src.execution.position_reevaluator.get_db_session")
    def test_execute_full_exit_deletes_position(self, mock_get_session):
        """Full exit should delete the position from DB."""
        mock_session = MagicMock()
        mock_position = MagicMock()
        mock_position.ticker = "TEST-1"
        mock_position.quantity = 100
        mock_position.average_price = 50

        mock_price = MagicMock()
        mock_price.yes_bid = 64
        mock_price.yes_ask = 66

        def query_side_effect(model):
            mock_q = MagicMock()
            if hasattr(model, "__tablename__"):
                if model.__tablename__ == "positions":
                    mock_q.filter.return_value.first.return_value = mock_position
                elif model.__tablename__ == "prices":
                    mock_q.filter.return_value.order_by.return_value.first.return_value = mock_price
            return mock_q

        mock_session.query.side_effect = query_side_effect
        mock_get_session.return_value = _mock_db_session(mock_session)

        reevaluator = PositionReEvaluator(paper_trading=True)

        decision = ExitDecision(
            ticker="TEST-1",
            side="yes",
            quantity=100,
            should_exit=True,
            reason=ExitReason.PROFIT_TARGET_TIER3,
            entry_price_cents=50,
            current_price_cents=65,
            hours_held=5.0,
        )

        success = reevaluator.execute_exit(decision)
        assert success is True
        mock_session.delete.assert_called_once_with(mock_position)

    @patch("src.execution.position_reevaluator.get_db_session")
    def test_execute_exit_no_position_found(self, mock_get_session):
        """Should return False when position not found."""
        mock_session = MagicMock()
        mock_session.query.return_value.filter.return_value.first.return_value = None

        mock_get_session.return_value = _mock_db_session(mock_session)

        reevaluator = PositionReEvaluator(paper_trading=True)
        decision = ExitDecision(
            ticker="NONEXISTENT",
            side="yes",
            quantity=10,
            should_exit=True,
            reason=ExitReason.STOP_LOSS,
            current_price_cents=42,
        )

        success = reevaluator.execute_exit(decision)
        assert success is False


# ─── LLM re-evaluation tests ────────────────────────────────────────────


class TestLLMReEvaluation:
    """Test LLM re-evaluation integration."""

    def _make_forecaster(self):
        """Create LLMForecaster with test key (real anthropic import)."""
        from src.analysis.forecaster import LLMForecaster
        return LLMForecaster(api_key="test-key")

    def test_re_evaluate_position_returns_dict(self):
        """re_evaluate_position should return dict with expected keys."""
        f = self._make_forecaster()

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="Analysis here.\n\nPROBABILITY: 0.55\nRECOMMENDATION: HOLD")]

        with patch.object(f.client.messages, "create", return_value=mock_response):
            result = f.re_evaluate_position(
                market_ticker="TEST-1",
                market_title="Will it rain tomorrow?",
                current_price=55,
                entry_price=50,
                side="yes",
                hours_held=12.0,
            )

        assert "recommendation" in result
        assert "new_probability" in result
        assert "confidence" in result
        assert "reasoning" in result
        assert "edge_pct" in result
        assert result["recommendation"] == "HOLD"

    def test_re_evaluate_position_exit(self):
        """LLM recommending EXIT should be parsed correctly."""
        f = self._make_forecaster()

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="Edge is gone.\n\nPROBABILITY: 0.52\nRECOMMENDATION: EXIT")]

        with patch.object(f.client.messages, "create", return_value=mock_response):
            result = f.re_evaluate_position(
                market_ticker="TEST-1",
                market_title="Will it rain?",
                current_price=55,
                entry_price=50,
                side="yes",
                hours_held=25.0,
            )

        assert result["recommendation"] == "EXIT"
        assert abs(result["new_probability"] - 0.52) < 0.01

    def test_re_evaluate_position_take_profit(self):
        """LLM recommending TAKE_PROFIT should be parsed correctly."""
        f = self._make_forecaster()

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="Lock in gains.\n\nPROBABILITY: 0.58\nRECOMMENDATION: TAKE_PROFIT")]

        with patch.object(f.client.messages, "create", return_value=mock_response):
            result = f.re_evaluate_position(
                market_ticker="TEST-1",
                market_title="Will it rain?",
                current_price=60,
                entry_price=50,
                side="yes",
                hours_held=6.0,
            )

        assert result["recommendation"] == "TAKE_PROFIT"

    def test_re_evaluate_no_client(self):
        """Should return default HOLD when client unavailable."""
        f = self._make_forecaster()
        f.client = None  # Simulate no client

        result = f.re_evaluate_position(
            market_ticker="TEST-1",
            market_title="Test",
            current_price=55,
            entry_price=50,
            side="yes",
            hours_held=2.0,
        )

        assert result["recommendation"] == "HOLD"

    def test_re_evaluate_api_error(self):
        """Should return default HOLD on API error."""
        f = self._make_forecaster()

        with patch.object(f.client.messages, "create", side_effect=Exception("API error")):
            result = f.re_evaluate_position(
                market_ticker="TEST-1",
                market_title="Test",
                current_price=55,
                entry_price=50,
                side="yes",
                hours_held=2.0,
            )

        assert result["recommendation"] == "HOLD"
        assert "Error" in result["reasoning"]


# ─── Global instance tests ───────────────────────────────────────────────


class TestGlobalInstance:
    """Test the get_position_reevaluator factory."""

    def test_creates_instance(self):
        """Should create a PositionReEvaluator instance."""
        # Reset global
        import src.execution.position_reevaluator as mod
        mod._reevaluator = None

        reeval = get_position_reevaluator(paper_trading=True)
        assert isinstance(reeval, PositionReEvaluator)
        assert reeval.paper_trading is True

        # Cleanup
        mod._reevaluator = None

    def test_returns_same_instance(self):
        """Should return the same global instance on repeated calls."""
        import src.execution.position_reevaluator as mod
        mod._reevaluator = None

        reeval1 = get_position_reevaluator()
        reeval2 = get_position_reevaluator()
        assert reeval1 is reeval2

        # Cleanup
        mod._reevaluator = None


# ─── Configuration tests ────────────────────────────────────────────────


class TestConfiguration:
    """Test that configuration defaults are sensible."""

    def test_default_thresholds(self):
        """Default thresholds should match spec."""
        r = PositionReEvaluator.__new__(PositionReEvaluator)
        assert r.TIER1_GAIN_CENTS == 5
        assert r.TIER2_GAIN_CENTS == 10
        assert r.TIER3_GAIN_CENTS == 15
        assert r.STOP_LOSS_CENTS == 8
        assert r.FORCE_EXIT_HOURS == 48
        assert r.AGGRESSIVE_RE_EVAL_HOURS == 24

    def test_tier_ordering(self):
        """Tier thresholds should be in ascending order."""
        r = PositionReEvaluator.__new__(PositionReEvaluator)
        assert r.TIER1_GAIN_CENTS < r.TIER2_GAIN_CENTS < r.TIER3_GAIN_CENTS

    def test_fraction_ordering(self):
        """Exit fractions should increase with tier."""
        r = PositionReEvaluator.__new__(PositionReEvaluator)
        assert r.TIER1_EXIT_FRACTION <= r.TIER2_EXIT_FRACTION <= r.TIER3_EXIT_FRACTION

    def test_tier3_is_full_exit(self):
        """Tier 3 should be 100% exit."""
        r = PositionReEvaluator.__new__(PositionReEvaluator)
        assert r.TIER3_EXIT_FRACTION == 1.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
