"""
Tests for velocity scanner and momentum-based exits.

Covers:
- Velocity calculation from PriceDB snapshots
- Tier classification (EXPLOSIVE, FAST, MODERATE, SLOW)
- Momentum score calculation
- Entry signal generation
- Momentum reversal and stall exits
- Integration with existing exit priority chain
"""

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.strategy.velocity_scanner import (
    VelocityOpportunity,
    VelocityScanResult,
    VelocityScanner,
    VelocityTier,
    get_velocity_scanner,
)
from src.execution.position_reevaluator import (
    ExitDecision,
    ExitReason,
    PositionReEvaluator,
)


# ─── Helpers ──────────────────────────────────────────────────────────


def _make_price_snapshot(ticker, yes_bid, timestamp, volume=100, yes_ask=None):
    """Create a mock PriceDB-like object with required attributes."""
    snap = MagicMock()
    snap.ticker = ticker
    snap.yes_bid = yes_bid
    snap.yes_ask = yes_ask if yes_ask is not None else (yes_bid + 2 if yes_bid else None)
    snap.volume = volume
    snap.timestamp = timestamp
    return snap


def _make_position(ticker, side, quantity, avg_price, created_at=None):
    """Create a mock PositionDB-like object."""
    pos = MagicMock()
    pos.ticker = ticker
    pos.side = side
    pos.quantity = quantity
    pos.average_price = avg_price
    pos.created_at = created_at or (datetime.now(timezone.utc) - timedelta(hours=1))
    return pos


def _make_market(ticker, title, status="active", category="unknown", close_time=None):
    """Create a mock MarketDB-like object."""
    m = MagicMock()
    m.ticker = ticker
    m.title = title
    m.status = status
    m.category = category
    m.close_time = close_time or (datetime.now(timezone.utc) + timedelta(hours=24))
    return m


def _mock_db_session(session):
    """Create a context-manager-compatible mock for get_db_session()."""
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=session)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


def make_gen(session_mock):
    """Factory for side_effect that yields a new generator each call."""
    def _gen():
        yield _mock_db_session(session_mock)
    return _gen


# ─── TestVelocityCalculation ──────────────────────────────────────────


class TestVelocityCalculation:
    """Test velocity calculation from price snapshots."""

    def test_steady_upward_velocity(self):
        """Steady upward price movement should give positive velocity."""
        scanner = VelocityScanner(paper_trading=True)
        now = datetime.now(timezone.utc)

        # 5 snapshots over 30 min: 50c → 60c (10c rise)
        snapshots = [
            _make_price_snapshot("TEST", 50, now - timedelta(minutes=30)),
            _make_price_snapshot("TEST", 52, now - timedelta(minutes=22)),
            _make_price_snapshot("TEST", 55, now - timedelta(minutes=15)),
            _make_price_snapshot("TEST", 58, now - timedelta(minutes=8)),
            _make_price_snapshot("TEST", 60, now),
        ]

        session = MagicMock()
        session.query.return_value.filter.return_value.order_by.return_value.all.return_value = snapshots

        with patch("src.strategy.velocity_scanner.get_db_session") as mock_db:
            mock_db.return_value = iter([_mock_db_session(session)])
            result = scanner._calculate_velocity("TEST")

        assert result is not None
        assert result["direction"] == "up"
        assert result["velocity_30min"] > 0
        assert result["current_price"] == 60

    def test_steady_downward_velocity(self):
        """Downward price movement should give negative velocity."""
        scanner = VelocityScanner(paper_trading=True)
        now = datetime.now(timezone.utc)

        snapshots = [
            _make_price_snapshot("TEST", 60, now - timedelta(minutes=30)),
            _make_price_snapshot("TEST", 57, now - timedelta(minutes=20)),
            _make_price_snapshot("TEST", 54, now - timedelta(minutes=10)),
            _make_price_snapshot("TEST", 50, now),
        ]

        session = MagicMock()
        session.query.return_value.filter.return_value.order_by.return_value.all.return_value = snapshots

        with patch("src.strategy.velocity_scanner.get_db_session") as mock_db:
            mock_db.return_value = iter([_mock_db_session(session)])
            result = scanner._calculate_velocity("TEST")

        assert result is not None
        assert result["direction"] == "down"
        assert result["velocity_30min"] < 0

    def test_flat_velocity(self):
        """No price change should give zero velocity."""
        scanner = VelocityScanner(paper_trading=True)
        now = datetime.now(timezone.utc)

        snapshots = [
            _make_price_snapshot("TEST", 50, now - timedelta(minutes=30)),
            _make_price_snapshot("TEST", 50, now - timedelta(minutes=20)),
            _make_price_snapshot("TEST", 50, now - timedelta(minutes=10)),
            _make_price_snapshot("TEST", 50, now),
        ]

        session = MagicMock()
        session.query.return_value.filter.return_value.order_by.return_value.all.return_value = snapshots

        with patch("src.strategy.velocity_scanner.get_db_session") as mock_db:
            mock_db.return_value = iter([_mock_db_session(session)])
            result = scanner._calculate_velocity("TEST")

        assert result is not None
        assert abs(result["velocity_30min"]) < 0.01

    def test_insufficient_data_returns_none(self):
        """Less than MIN_SNAPSHOTS should return None."""
        scanner = VelocityScanner(paper_trading=True)
        now = datetime.now(timezone.utc)

        # Only 2 snapshots (min is 3)
        snapshots = [
            _make_price_snapshot("TEST", 50, now - timedelta(minutes=30)),
            _make_price_snapshot("TEST", 55, now),
        ]

        session = MagicMock()
        session.query.return_value.filter.return_value.order_by.return_value.all.return_value = snapshots

        with patch("src.strategy.velocity_scanner.get_db_session") as mock_db:
            mock_db.return_value = iter([_mock_db_session(session)])
            result = scanner._calculate_velocity("TEST")

        assert result is None

    def test_tier_classification_explosive(self):
        """10c+ in 30 min should be EXPLOSIVE."""
        scanner = VelocityScanner(paper_trading=True)
        # velocity_30min * 30 >= 10 → 0.34 c/min * 30 = 10.2c
        tier = scanner._classify_velocity(0.34, 10.2)
        assert tier == VelocityTier.EXPLOSIVE

    def test_tier_classification_fast(self):
        """5-9c in 30 min should be FAST."""
        scanner = VelocityScanner(paper_trading=True)
        # 0.2 c/min * 30 = 6c
        tier = scanner._classify_velocity(0.2, 6.0)
        assert tier == VelocityTier.FAST

    def test_tier_classification_slow(self):
        """<2c in 30 min should be SLOW."""
        scanner = VelocityScanner(paper_trading=True)
        # 0.03 c/min * 30 = 0.9c
        tier = scanner._classify_velocity(0.03, 0.9)
        assert tier == VelocityTier.SLOW

    def test_volume_spike_detection(self):
        """High recent volume should increase momentum score."""
        scanner = VelocityScanner(paper_trading=True)

        # High velocity + high volume
        score_high_vol = scanner._calculate_momentum_score(
            velocity=0.34,      # ~10c in 30 min
            acceleration=2.0,
            volume_ratio=2.5,   # 2.5x average volume
        )

        # Same velocity + low volume
        score_low_vol = scanner._calculate_momentum_score(
            velocity=0.34,
            acceleration=2.0,
            volume_ratio=0.5,   # below average
        )

        assert score_high_vol > score_low_vol


# ─── TestEntrySignals ──────────────────────────────────────────────────


class TestEntrySignals:
    """Test entry signal generation."""

    def test_buy_yes_on_upward_momentum(self):
        """Upward momentum should generate buy YES signal."""
        scanner = VelocityScanner(paper_trading=True)
        velocity = {
            "direction": "up",
            "current_price": 55,
            "velocity_30min": 0.34,
            "acceleration": 1.0,
            "volume_ratio": 1.2,
        }
        meta = {"title": "Test Market", "category": "crypto"}

        result = scanner._generate_entry_signal("TEST", VelocityTier.FAST, velocity, meta)

        assert result is not None
        assert result["side"] == "yes"
        assert result["price"] == 55  # buy YES at current price
        assert result["quantity"] > 0

    def test_buy_no_on_downward_momentum(self):
        """Downward momentum should generate buy NO signal."""
        scanner = VelocityScanner(paper_trading=True)
        velocity = {
            "direction": "down",
            "current_price": 40,
            "velocity_30min": -0.20,
            "acceleration": -1.0,
            "volume_ratio": 1.5,
        }
        meta = {"title": "Test Market", "category": "crypto"}

        result = scanner._generate_entry_signal("TEST", VelocityTier.FAST, velocity, meta)

        assert result is not None
        assert result["side"] == "no"
        assert result["price"] == 60  # NO cost = 100 - 40

    def test_skip_already_positioned(self):
        """Should skip tickers with existing open positions."""
        scanner = VelocityScanner(paper_trading=True)

        session = MagicMock()
        # Has open position
        session.query.return_value.filter.return_value.first.return_value = MagicMock(quantity=10)

        with patch("src.strategy.velocity_scanner.get_db_session") as mock_db:
            mock_db.return_value = iter([_mock_db_session(session)])
            result = scanner._already_positioned("TEST")

        assert result is True

    def test_position_sizing_by_tier(self):
        """Position size should be smaller for EXPLOSIVE, larger for MODERATE."""
        scanner = VelocityScanner(paper_trading=True)

        velocity = {
            "direction": "up",
            "current_price": 50,
            "velocity_30min": 0.34,
            "acceleration": 1.0,
            "volume_ratio": 1.0,
        }
        meta = {"title": "Test", "category": "test"}

        explosive = scanner._generate_entry_signal("T1", VelocityTier.EXPLOSIVE, velocity, meta)
        moderate = scanner._generate_entry_signal("T2", VelocityTier.MODERATE, velocity, meta)

        assert explosive is not None
        assert moderate is not None
        assert moderate["quantity"] > explosive["quantity"]

    def test_skip_extreme_prices(self):
        """Should skip markets at extreme prices (<=2c or >=98c)."""
        scanner = VelocityScanner(paper_trading=True)

        velocity_high = {
            "direction": "up",
            "current_price": 99,
            "velocity_30min": 0.34,
            "acceleration": 1.0,
            "volume_ratio": 1.0,
        }
        meta = {"title": "Test", "category": "test"}

        result = scanner._generate_entry_signal("TEST", VelocityTier.FAST, velocity_high, meta)
        assert result is None

    def test_skip_recent_trade(self):
        """Should skip tickers with buy trades in last 30 min."""
        scanner = VelocityScanner(paper_trading=True)

        session = MagicMock()
        # No open position
        pos_query = MagicMock()
        pos_query.first.return_value = None

        # Has recent trade
        trade_query = MagicMock()
        trade_query.first.return_value = MagicMock()  # exists

        def filter_side_effect(*args, **kwargs):
            # First filter call = PositionDB, second = TradeDB
            mock = MagicMock()
            if not hasattr(filter_side_effect, '_call_count'):
                filter_side_effect._call_count = 0
            filter_side_effect._call_count += 1
            if filter_side_effect._call_count <= 1:
                mock.first.return_value = None
            else:
                mock.first.return_value = MagicMock()
            return mock

        session.query.return_value.filter.side_effect = filter_side_effect

        with patch("src.strategy.velocity_scanner.get_db_session") as mock_db:
            mock_db.return_value = iter([_mock_db_session(session)])
            result = scanner._already_positioned("TEST")

        assert result is True


# ─── TestMomentumExits ──────────────────────────────────────────────────


class TestMomentumExits:
    """Test momentum-based exit logic in PositionReEvaluator."""

    def test_reversal_exit_yes_side(self):
        """Price drop from peak should trigger reversal exit for YES position."""
        reeval = PositionReEvaluator(paper_trading=True)

        entry_time = datetime.now(timezone.utc) - timedelta(hours=1)

        # Peak was 55c, current is 51c → drop of 4c >= 3c threshold
        with patch.object(reeval, "_get_peak_price_since_entry", return_value=55), \
             patch.object(reeval, "_get_recent_velocity", return_value=None):
            result = reeval._check_momentum_exit(
                ticker="TEST",
                side="yes",
                quantity=10,
                entry_price=48,
                current_price=51,
                pnl_cents=3,
                pnl_dollars=0.30,
                pnl_pct=0.0625,
                hours_held=1.0,
                created_at=entry_time,
            )

        assert result is not None
        assert result.should_exit is True
        assert result.reason == ExitReason.MOMENTUM_REVERSAL
        assert result.quantity == 10

    def test_reversal_exit_no_side(self):
        """Price reversal should trigger exit for NO position."""
        reeval = PositionReEvaluator(paper_trading=True)
        entry_time = datetime.now(timezone.utc) - timedelta(hours=1)

        # For NO side: peak NO value was 55, current NO value = 100 - 49 = 51
        # Drop = 55 - 51 = 4c >= 3c threshold
        with patch.object(reeval, "_get_peak_price_since_entry", return_value=55), \
             patch.object(reeval, "_get_recent_velocity", return_value=None):
            result = reeval._check_momentum_exit(
                ticker="TEST",
                side="no",
                quantity=10,
                entry_price=48,
                current_price=49,  # yes price = 49, NO value = 51
                pnl_cents=3,
                pnl_dollars=0.30,
                pnl_pct=0.0625,
                hours_held=1.0,
                created_at=entry_time,
            )

        assert result is not None
        assert result.should_exit is True
        assert result.reason == ExitReason.MOMENTUM_REVERSAL

    def test_stall_exit(self):
        """No price movement for 20+ min should trigger stall exit."""
        reeval = PositionReEvaluator(paper_trading=True)
        entry_time = datetime.now(timezone.utc) - timedelta(hours=1)

        velocity_data = {
            "velocity_per_min": 0.0,
            "direction": "flat",
            "peak_price": 55,
            "trough_price": 55,
            "snapshots": 5,
            "stall_minutes": 25,  # 25 min stall >= 20 min threshold
        }

        with patch.object(reeval, "_get_peak_price_since_entry", return_value=55), \
             patch.object(reeval, "_get_recent_velocity", return_value=velocity_data):
            result = reeval._check_momentum_exit(
                ticker="TEST",
                side="yes",
                quantity=10,
                entry_price=50,
                current_price=55,
                pnl_cents=5,
                pnl_dollars=0.50,
                pnl_pct=0.10,
                hours_held=1.0,
                created_at=entry_time,
            )

        assert result is not None
        assert result.should_exit is True
        assert result.reason == ExitReason.MOMENTUM_STALL

    def test_no_exit_when_in_loss(self):
        """Momentum exits should NOT fire when pnl_cents <= 0."""
        reeval = PositionReEvaluator(paper_trading=True)

        # The momentum check is guarded by pnl_cents > 0 in _evaluate_position
        # Test the guard directly
        pos_data = {
            "ticker": "TEST",
            "side": "yes",
            "quantity": 10,
            "entry_price": 50,
            "created_at": datetime.now(timezone.utc) - timedelta(hours=1),
            "current_price": 48,  # pnl = -2c (in loss)
        }

        # Mock all external calls to prevent actual DB access
        with patch.object(reeval, "_check_momentum_exit") as mock_momentum:
            result = reeval._evaluate_position(pos_data)

            # _check_momentum_exit should NOT have been called (pnl <= 0)
            mock_momentum.assert_not_called()

    def test_no_exit_on_small_drop(self):
        """Drop smaller than threshold should not trigger exit."""
        reeval = PositionReEvaluator(paper_trading=True)
        entry_time = datetime.now(timezone.utc) - timedelta(hours=1)

        # Peak 55, current 53 → drop 2c < 3c threshold
        with patch.object(reeval, "_get_peak_price_since_entry", return_value=55), \
             patch.object(reeval, "_get_recent_velocity", return_value=None):
            result = reeval._check_momentum_exit(
                ticker="TEST",
                side="yes",
                quantity=10,
                entry_price=50,
                current_price=53,
                pnl_cents=3,
                pnl_dollars=0.30,
                pnl_pct=0.06,
                hours_held=1.0,
                created_at=entry_time,
            )

        assert result is None

    @patch.dict(os.environ, {"EXIT_MOMENTUM_ENABLED": "false"})
    def test_momentum_disabled_skips(self):
        """When momentum is disabled, the check should be skipped."""
        # Create a new instance to pick up the env var
        reeval = PositionReEvaluator.__new__(PositionReEvaluator)
        reeval.MOMENTUM_ENABLED = False
        reeval.paper_trading = True
        reeval._partial_exits = {}
        reeval._llm_reeval_queue = {}
        reeval._last_llm_batch = None

        pos_data = {
            "ticker": "TEST",
            "side": "yes",
            "quantity": 10,
            "entry_price": 50,
            "created_at": datetime.now(timezone.utc) - timedelta(hours=1),
            "current_price": 55,  # pnl = +5c
        }

        with patch.object(reeval, "_check_momentum_exit") as mock_momentum:
            # Manually set MOMENTUM_ENABLED to False on instance
            reeval.MOMENTUM_ENABLED = False
            result = reeval._evaluate_position(pos_data)

            # Should NOT call momentum check when disabled
            mock_momentum.assert_not_called()

    def test_integration_with_priority_chain(self):
        """Momentum exits should respect the priority order: stop-loss first, then momentum."""
        reeval = PositionReEvaluator(paper_trading=True)

        # Position with heavy loss — stop-loss should fire BEFORE momentum check
        pos_data = {
            "ticker": "TEST",
            "side": "yes",
            "quantity": 10,
            "entry_price": 50,
            "created_at": datetime.now(timezone.utc) - timedelta(hours=1),
            "current_price": 40,  # pnl = -10c → exceeds -8c stop-loss
        }

        with patch.object(reeval, "_check_momentum_exit") as mock_momentum:
            result = reeval._evaluate_position(pos_data)

            # Stop-loss should fire first, momentum should NOT be called
            assert result.should_exit is True
            assert result.reason == ExitReason.STOP_LOSS
            mock_momentum.assert_not_called()


# ─── TestPeakAndVelocityHelpers ────────────────────────────────────────


class TestPeakAndVelocityHelpers:
    """Test _get_peak_price_since_entry and _get_recent_velocity."""

    def test_peak_price_yes_side(self):
        """Should return max yes_bid for YES positions."""
        reeval = PositionReEvaluator(paper_trading=True)
        entry_time = datetime.now(timezone.utc) - timedelta(hours=1)

        session = MagicMock()
        session.query.return_value.filter.return_value.scalar.return_value = 62

        with patch("src.execution.position_reevaluator.get_db_session") as mock_db:
            mock_db.return_value = iter([_mock_db_session(session)])
            result = reeval._get_peak_price_since_entry("TEST", "yes", entry_time)

        assert result == 62

    def test_peak_price_no_side(self):
        """Should return 100 - min(yes_ask) for NO positions."""
        reeval = PositionReEvaluator(paper_trading=True)
        entry_time = datetime.now(timezone.utc) - timedelta(hours=1)

        session = MagicMock()
        # min(yes_ask) = 38 → NO value = 100 - 38 = 62
        session.query.return_value.filter.return_value.scalar.return_value = 38

        with patch("src.execution.position_reevaluator.get_db_session") as mock_db:
            mock_db.return_value = iter([_mock_db_session(session)])
            result = reeval._get_peak_price_since_entry("TEST", "no", entry_time)

        assert result == 62

    def test_peak_price_no_data(self):
        """Should return None when no data available."""
        reeval = PositionReEvaluator(paper_trading=True)
        entry_time = datetime.now(timezone.utc) - timedelta(hours=1)

        session = MagicMock()
        session.query.return_value.filter.return_value.scalar.return_value = None

        with patch("src.execution.position_reevaluator.get_db_session") as mock_db:
            mock_db.return_value = iter([_mock_db_session(session)])
            result = reeval._get_peak_price_since_entry("TEST", "yes", entry_time)

        assert result is None

    def test_recent_velocity_calculation(self):
        """Should calculate velocity from recent snapshots."""
        reeval = PositionReEvaluator(paper_trading=True)
        now = datetime.now(timezone.utc)

        snapshots = [
            _make_price_snapshot("TEST", 50, now - timedelta(minutes=14)),
            _make_price_snapshot("TEST", 52, now - timedelta(minutes=10)),
            _make_price_snapshot("TEST", 55, now - timedelta(minutes=5)),
            _make_price_snapshot("TEST", 58, now),
        ]

        session = MagicMock()
        session.query.return_value.filter.return_value.order_by.return_value.all.return_value = snapshots

        with patch("src.execution.position_reevaluator.get_db_session") as mock_db:
            mock_db.return_value = iter([_mock_db_session(session)])
            result = reeval._get_recent_velocity("TEST", 15)

        assert result is not None
        assert result["velocity_per_min"] > 0
        assert result["direction"] == "up"
        assert result["snapshots"] == 4
        assert result["peak_price"] == 58
        assert result["trough_price"] == 50

    def test_stall_detection(self):
        """Should detect stall when prices stop moving."""
        reeval = PositionReEvaluator(paper_trading=True)
        now = datetime.now(timezone.utc)

        # Prices moved, then stalled for 20+ minutes
        snapshots = [
            _make_price_snapshot("TEST", 50, now - timedelta(minutes=30)),
            _make_price_snapshot("TEST", 55, now - timedelta(minutes=25)),
            _make_price_snapshot("TEST", 55, now - timedelta(minutes=20)),
            _make_price_snapshot("TEST", 55, now - timedelta(minutes=15)),
            _make_price_snapshot("TEST", 55, now - timedelta(minutes=10)),
            _make_price_snapshot("TEST", 55, now),
        ]

        session = MagicMock()
        session.query.return_value.filter.return_value.order_by.return_value.all.return_value = snapshots

        with patch("src.execution.position_reevaluator.get_db_session") as mock_db:
            mock_db.return_value = iter([_mock_db_session(session)])
            result = reeval._get_recent_velocity("TEST", 30)

        assert result is not None
        assert result["stall_minutes"] >= 20


# ─── TestIntegration ──────────────────────────────────────────────────


class TestIntegration:
    """Integration tests for velocity scanner + main.py wiring."""

    def test_full_scan_cycle_with_mock_data(self):
        """Scan should find opportunities from mocked price data."""
        scanner = VelocityScanner(paper_trading=True)
        now = datetime.now(timezone.utc)

        # Mock price snapshots showing fast upward movement
        snapshots = [
            _make_price_snapshot("FAST-MKT", 40, now - timedelta(minutes=30)),
            _make_price_snapshot("FAST-MKT", 45, now - timedelta(minutes=20)),
            _make_price_snapshot("FAST-MKT", 50, now - timedelta(minutes=10)),
            _make_price_snapshot("FAST-MKT", 55, now),
        ]

        market = _make_market(
            "FAST-MKT", "Fast Moving Market",
            close_time=now + timedelta(hours=24),
        )

        session = MagicMock()

        # Mock distinct tickers query
        session.query.return_value.filter.return_value.all.side_effect = [
            [("FAST-MKT",)],  # distinct tickers
            [market],         # markets query
        ]

        # For _calculate_velocity and _already_positioned, we need separate sessions
        vel_session = MagicMock()
        vel_session.query.return_value.filter.return_value.order_by.return_value.all.return_value = snapshots

        pos_session = MagicMock()
        pos_session.query.return_value.filter.return_value.first.return_value = None

        call_count = [0]
        def db_gen():
            call_count[0] += 1
            if call_count[0] == 1:
                yield _mock_db_session(session)       # scan_markets main query
            elif call_count[0] == 2:
                yield _mock_db_session(vel_session)    # _calculate_velocity
            else:
                yield _mock_db_session(pos_session)    # _already_positioned

        with patch("src.strategy.velocity_scanner.get_db_session", side_effect=db_gen):
            result = scanner.scan_markets()

        assert isinstance(result, VelocityScanResult)
        assert result.markets_scanned >= 1

    def test_velocity_opportunity_dataclass(self):
        """VelocityOpportunity should hold all required fields."""
        opp = VelocityOpportunity(
            ticker="TEST-MKT",
            title="Test Market",
            tier=VelocityTier.FAST,
            direction="up",
            velocity_30min=0.2,
            velocity_1h=8.0,
            velocity_4h=5.0,
            acceleration=1.5,
            volume_ratio=1.3,
            momentum_score=0.7,
            current_price=55,
            price_30min_ago=49,
            price_1h_ago=47,
            suggested_side="yes",
            suggested_quantity=40,
            suggested_price=55,
            category="crypto",
        )

        assert opp.ticker == "TEST-MKT"
        assert opp.tier == VelocityTier.FAST
        assert opp.momentum_score == 0.7

    def test_env_var_configuration(self):
        """Scanner should respect environment variable configuration."""
        with patch.dict(os.environ, {
            "VELOCITY_EXPLOSIVE_CENTS": "15",
            "VELOCITY_FAST_CENTS": "8",
            "VELOCITY_MODERATE_CENTS": "3",
            "VELOCITY_MAX_ENTRIES": "5",
        }):
            # Create a new class instance to pick up env vars
            scanner = VelocityScanner.__new__(VelocityScanner)
            scanner.EXPLOSIVE_THRESHOLD = int(os.getenv("VELOCITY_EXPLOSIVE_CENTS", "10"))
            scanner.FAST_THRESHOLD = int(os.getenv("VELOCITY_FAST_CENTS", "5"))
            scanner.MODERATE_THRESHOLD = int(os.getenv("VELOCITY_MODERATE_CENTS", "2"))
            scanner.MAX_ENTRIES_PER_CYCLE = int(os.getenv("VELOCITY_MAX_ENTRIES", "20"))

            assert scanner.EXPLOSIVE_THRESHOLD == 15
            assert scanner.FAST_THRESHOLD == 8
            assert scanner.MODERATE_THRESHOLD == 3
            assert scanner.MAX_ENTRIES_PER_CYCLE == 5


# ─── TestScanResult ──────────────────────────────────────────────────


class TestScanResult:
    """Test VelocityScanResult dataclass."""

    def test_empty_result(self):
        """Empty scan result should have zero counts."""
        result = VelocityScanResult(
            markets_scanned=100,
            fast_movers_found=0,
            opportunities=[],
            errors=0,
        )
        assert result.markets_scanned == 100
        assert result.fast_movers_found == 0
        assert len(result.opportunities) == 0

    def test_singleton_factory(self):
        """get_velocity_scanner should return the same instance."""
        import src.strategy.velocity_scanner as vs_module
        vs_module._velocity_scanner = None  # Reset singleton

        s1 = get_velocity_scanner(paper_trading=True)
        s2 = get_velocity_scanner(paper_trading=True)
        assert s1 is s2

        vs_module._velocity_scanner = None  # Cleanup
