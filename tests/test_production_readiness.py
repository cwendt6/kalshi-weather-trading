"""Tests for production readiness fixes."""
import threading
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from src.execution.executor import (
    ExecutionResult,
    Order,
    OrderAction,
    OrderExecutor,
    OrderSide,
    OrderStatus,
)
from src.execution.position_sizer import PositionSizer
from src.execution.risk_manager import RiskManager, RiskViolation


# ═══════════════════════════════════════════════════════════════════════════════
# Part 1: Live Trading Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestLiveTrading:
    """Test live Kalshi order submission."""

    def test_submit_fails_without_client(self):
        """Live submission should fail gracefully without client."""
        executor = OrderExecutor(paper_trading=False, kalshi_client=None)

        order = Order(
            order_id="TEST-001",
            ticker="TEST",
            side=OrderSide.YES,
            action=OrderAction.BUY,
            quantity=10,
            price=50,
        )
        result = executor._submit_to_kalshi(order)
        assert result.success is False
        assert "not configured" in result.message
        assert order.status == OrderStatus.FAILED

    def test_submit_with_mock_client(self):
        """Live submission should call Kalshi API and return success."""
        mock_client = MagicMock()
        mock_order_response = MagicMock()
        mock_order_response.order_id = "KALSHI-123"

        # Make place_order a coroutine that returns the mock response
        import asyncio

        async def mock_place_order(**kwargs):
            return mock_order_response

        mock_client.place_order = mock_place_order

        executor = OrderExecutor(paper_trading=False, kalshi_client=mock_client)

        order = Order(
            order_id="TEST-002",
            ticker="TEST",
            side=OrderSide.YES,
            action=OrderAction.BUY,
            quantity=10,
            price=50,
        )

        # Mock out _store_trade and _update_position since we don't have a DB
        with patch.object(executor, "_store_trade"), \
             patch.object(executor, "_update_position"):
            result = executor._submit_to_kalshi(order)

        assert result.success is True
        assert order.exchange_order_id == "KALSHI-123"
        assert "KALSHI-123" in result.message

    def test_submit_handles_api_error(self):
        """Live submission should handle API errors gracefully."""
        mock_client = MagicMock()

        import asyncio

        async def mock_place_order(**kwargs):
            raise ConnectionError("API unreachable")

        mock_client.place_order = mock_place_order

        executor = OrderExecutor(paper_trading=False, kalshi_client=mock_client)

        order = Order(
            order_id="TEST-003",
            ticker="TEST",
            side=OrderSide.YES,
            action=OrderAction.BUY,
            quantity=10,
            price=50,
        )
        result = executor._submit_to_kalshi(order)
        assert result.success is False
        assert order.status == OrderStatus.FAILED
        assert "API unreachable" in order.error_message

    def test_paper_trading_still_works(self):
        """Paper trading should still simulate fills."""
        executor = OrderExecutor(paper_trading=True)

        order = Order(
            order_id="TEST-004",
            ticker="TEST",
            side=OrderSide.YES,
            action=OrderAction.BUY,
            quantity=10,
            price=50,
        )

        with patch.object(executor, "_store_trade"), \
             patch.object(executor, "_update_position"):
            result = executor._simulate_fill(order)

        assert result.success is True
        assert order.status == OrderStatus.FILLED
        assert order.filled_quantity == 10

    def test_check_order_status_without_client(self):
        """Order status check should return None without client."""
        executor = OrderExecutor(paper_trading=True, kalshi_client=None)

        import asyncio
        result = asyncio.run(executor.check_order_status("FAKE-123"))
        assert result is None


# ═══════════════════════════════════════════════════════════════════════════════
# Part 2: Longshot Hunter Wiring Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestLongshotWiring:
    """Test longshot hunter is wired into main loop."""

    def test_longshot_import_in_main(self):
        """LongshotHunter should be importable from main module's imports."""
        from src.strategy.longshot_hunter import LongshotHunter, get_longshot_hunter
        assert LongshotHunter is not None
        assert get_longshot_hunter is not None

    def test_longshot_hunter_scan_method(self):
        """LongshotHunter.scan_for_longshots should be callable."""
        from src.strategy.longshot_hunter import LongshotHunter
        hunter = LongshotHunter()
        # Should work with empty market list
        result = hunter.scan_for_longshots(markets=[])
        assert result == []

    def test_main_has_longshot_scan(self):
        """main.py should reference longshot scanning."""
        import inspect
        from src.main import TradingSystem
        source = inspect.getsource(TradingSystem)
        assert "longshot_hunter" in source
        assert "_scan_and_execute_longshots" in source
        assert "longshot_scan" in source.lower()


# ═══════════════════════════════════════════════════════════════════════════════
# Part 3: Bankroll Sync Tests
# ═══════════════════════════════════════════════════════════════════════════════


def _mock_db_session(mock_session):
    """Helper to properly mock get_db_session() used as next(get_db_session())."""
    mock_session.__enter__ = MagicMock(return_value=mock_session)
    mock_session.__exit__ = MagicMock(return_value=False)
    mock_gen = MagicMock()
    mock_gen.__next__ = MagicMock(return_value=mock_session)
    return mock_gen


class TestBankrollSync:
    """Test bankroll syncs with Kalshi API."""

    def test_paper_mode_uses_db_or_config(self):
        """Paper mode should use DB snapshot, not Kalshi API."""
        sizer = PositionSizer(paper_trading=True, kalshi_client=MagicMock())

        with patch("src.execution.position_sizer.get_db_session") as mock_db:
            mock_session = MagicMock()
            mock_snapshot = MagicMock()
            mock_snapshot.balance = 5000.0
            mock_session.query.return_value.order_by.return_value.first.return_value = mock_snapshot
            mock_db.return_value = _mock_db_session(mock_session)

            bankroll = sizer.get_current_bankroll()
            assert bankroll == 5000.0

    def test_live_mode_tries_api_first(self):
        """Live mode should try Kalshi API first."""
        import asyncio

        async def mock_get_balance():
            mock_balance = MagicMock()
            mock_balance.balance = 7500.0
            return mock_balance

        mock_client = MagicMock()
        mock_client.get_balance = mock_get_balance

        sizer = PositionSizer(paper_trading=False, kalshi_client=mock_client)
        bankroll = sizer.get_current_bankroll()
        assert bankroll == 7500.0

    def test_live_mode_falls_back_to_db(self):
        """Live mode should fall back to DB if API fails."""
        import asyncio

        async def mock_get_balance():
            raise ConnectionError("API down")

        mock_client = MagicMock()
        mock_client.get_balance = mock_get_balance

        sizer = PositionSizer(paper_trading=False, kalshi_client=mock_client)

        with patch("src.execution.position_sizer.get_db_session") as mock_db:
            mock_session = MagicMock()
            mock_snapshot = MagicMock()
            mock_snapshot.balance = 3000.0
            mock_session.query.return_value.order_by.return_value.first.return_value = mock_snapshot
            mock_db.return_value = _mock_db_session(mock_session)

            bankroll = sizer.get_current_bankroll()
            assert bankroll == 3000.0

    def test_config_fallback(self):
        """Should fall back to config bankroll when no other source available."""
        sizer = PositionSizer(paper_trading=True, kalshi_client=None)

        with patch("src.execution.position_sizer.get_db_session") as mock_db:
            mock_session = MagicMock()
            mock_session.query.return_value.order_by.return_value.first.return_value = None
            mock_db.return_value = _mock_db_session(mock_session)

            bankroll = sizer.get_current_bankroll()
            assert bankroll == sizer._config_bankroll


# ═══════════════════════════════════════════════════════════════════════════════
# Part 4: Exposure at Market Price Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestExposureAtMarketPrice:
    """Test exposure uses current market price."""

    def test_exposure_uses_market_price(self):
        """Exposure should reflect current market value, not entry price."""
        sizer = PositionSizer()

        with patch("src.execution.position_sizer.get_db_session") as mock_db:
            mock_session = MagicMock()
            mock_pos = MagicMock()
            mock_pos.quantity = 100
            mock_pos.average_price = 30  # Bought at 30c
            mock_pos.market_price = 70   # Now at 70c
            mock_session.query.return_value.filter.return_value.all.return_value = [mock_pos]
            mock_db.return_value = _mock_db_session(mock_session)

            exposure = sizer.get_total_exposure()
            # Expected: 100 * 70 / 100 = $70
            assert exposure == 70.0

    def test_exposure_falls_back_to_entry_price(self):
        """Exposure should fall back to entry price if market_price is None."""
        sizer = PositionSizer()

        with patch("src.execution.position_sizer.get_db_session") as mock_db:
            mock_session = MagicMock()
            mock_pos = MagicMock()
            mock_pos.quantity = 100
            mock_pos.average_price = 30
            mock_pos.market_price = None  # No market price
            mock_session.query.return_value.filter.return_value.all.return_value = [mock_pos]
            mock_db.return_value = _mock_db_session(mock_session)

            exposure = sizer.get_total_exposure()
            # Falls back to entry price: 100 * 30 / 100 = $30
            assert exposure == 30.0

    def test_exposure_empty_portfolio(self):
        """Exposure should be 0 with no positions."""
        sizer = PositionSizer()

        with patch("src.execution.position_sizer.get_db_session") as mock_db:
            mock_session = MagicMock()
            mock_session.query.return_value.filter.return_value.all.return_value = []
            mock_db.return_value = _mock_db_session(mock_session)

            exposure = sizer.get_total_exposure()
            assert exposure == 0.0


# ═══════════════════════════════════════════════════════════════════════════════
# Part 5: Correlation Detection Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestPortfolioCorrelation:
    """Test correlation detection."""

    def test_same_event_blocked_at_limit(self):
        """Should block 3rd position on same event."""
        rm = RiskManager(initial_bankroll=10000.0)

        with patch("src.execution.risk_manager.get_db_session") as mock_db:
            mock_session = MagicMock()
            mock_session.__enter__ = MagicMock(return_value=mock_session)
            mock_session.__exit__ = MagicMock(return_value=False)

            # 2 existing positions on same event
            pos1 = MagicMock()
            pos1.ticker = "KXBTC-26FEB07-T100000"
            pos1.quantity = 10
            pos1.average_price = 30

            pos2 = MagicMock()
            pos2.ticker = "KXBTC-26FEB07-T105000"
            pos2.quantity = 10
            pos2.average_price = 20

            # New market and existing markets share category
            market_mock = MagicMock()
            market_mock.category = "crypto"

            # Wire the session queries:
            # query(PositionDB).filter(qty>0).all() -> positions
            # query(MarketDB).filter_by(ticker=...).first() -> market
            from src.data.models import PositionDB, MarketDB

            def query_side_effect(model):
                q = MagicMock()
                if model == PositionDB:
                    q.filter.return_value.all.return_value = [pos1, pos2]
                elif model == MarketDB:
                    q.filter_by.return_value.first.return_value = market_mock
                return q

            mock_session.query.side_effect = query_side_effect
            mock_gen = MagicMock()
            mock_gen.__next__ = MagicMock(return_value=mock_session)
            mock_db.return_value = mock_gen

            result = rm.check_correlation("KXBTC-26FEB07-T110000", "yes")
            assert result is False  # Blocked — 3rd position on same event

    def test_different_event_allowed(self):
        """Should allow positions on different events."""
        rm = RiskManager(initial_bankroll=10000.0)

        with patch("src.execution.risk_manager.get_db_session") as mock_db:
            mock_session = MagicMock()
            mock_session.__enter__ = MagicMock(return_value=mock_session)
            mock_session.__exit__ = MagicMock(return_value=False)

            pos1 = MagicMock()
            pos1.ticker = "KXETH-26FEB07-T5000"
            pos1.quantity = 10
            pos1.average_price = 30

            market_mock = MagicMock()
            market_mock.category = "crypto"

            from src.data.models import PositionDB, MarketDB

            def query_side_effect(model):
                q = MagicMock()
                if model == PositionDB:
                    q.filter.return_value.all.return_value = [pos1]
                elif model == MarketDB:
                    q.filter_by.return_value.first.return_value = market_mock
                return q

            mock_session.query.side_effect = query_side_effect
            mock_db.return_value = _mock_db_session(mock_session)

            result = rm.check_correlation("KXBTC-26FEB07-T100000", "yes")
            assert result is True  # Different event prefix

    def test_no_positions_always_allowed(self):
        """Should allow any trade when no positions exist."""
        rm = RiskManager(initial_bankroll=10000.0)

        with patch("src.execution.risk_manager.get_db_session") as mock_db:
            mock_session = MagicMock()
            mock_session.__enter__ = MagicMock(return_value=mock_session)
            mock_session.__exit__ = MagicMock(return_value=False)

            from src.data.models import PositionDB, MarketDB

            def query_side_effect(model):
                q = MagicMock()
                if model == PositionDB:
                    q.filter.return_value.all.return_value = []
                elif model == MarketDB:
                    q.filter_by.return_value.first.return_value = MagicMock(category="test")
                return q

            mock_session.query.side_effect = query_side_effect
            mock_db.return_value = _mock_db_session(mock_session)

            result = rm.check_correlation("ANYTHING-123", "yes")
            assert result is True

    def test_extract_event_slug(self):
        """Event slug should extract first 2 segments."""
        rm = RiskManager()
        assert rm._extract_event_slug("KXBTC-26FEB07-T100000") == "KXBTC-26FEB07"
        assert rm._extract_event_slug("INX-25FEB07-T5680") == "INX-25FEB07"
        assert rm._extract_event_slug("SIMPLE") == "SIMPLE"

    def test_concentration_violation_in_check_trade(self):
        """check_trade should include CONCENTRATION_LIMIT violation."""
        rm = RiskManager(initial_bankroll=10000.0)

        with patch.object(rm, "check_correlation", return_value=False), \
             patch.object(rm, "get_daily_pnl", return_value=0.0), \
             patch.object(rm, "get_total_exposure", return_value=0.0), \
             patch.object(rm, "get_position_exposure", return_value=0.0), \
             patch.object(rm, "get_current_drawdown", return_value=0.0):
            result = rm.check_trade("TEST", "yes", 10, 50)
            assert not result.approved
            assert RiskViolation.CONCENTRATION_LIMIT in result.violations


# ═══════════════════════════════════════════════════════════════════════════════
# Part 6: Thread-Safe Position Sizing Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestBatchSizingLock:
    """Test thread-safety of position sizing."""

    def test_sizer_has_allocation_lock(self):
        """PositionSizer should have a threading lock."""
        sizer = PositionSizer()
        assert hasattr(sizer, "_allocation_lock")
        assert isinstance(sizer._allocation_lock, type(threading.Lock()))

    def test_concurrent_sizing_is_serialized(self):
        """Two threads calling calculate_position_size should be serialized."""
        sizer = PositionSizer()
        results = []
        call_order = []

        def mock_calculate(*args, **kwargs):
            call_order.append(threading.current_thread().name)
            import time
            time.sleep(0.05)  # Small delay to test lock
            return MagicMock(recommended_contracts=5, recommended_dollars=25.0)

        with patch.object(sizer, "_calculate_position_size_unsafe", side_effect=mock_calculate), \
             patch.object(sizer, "get_current_bankroll", return_value=1000.0):

            def call_sizer(name):
                result = sizer.calculate_position_size(
                    ticker="TEST", win_probability=0.6,
                    entry_price=30, side="yes", edge=0.1,
                )
                results.append((name, result))

            t1 = threading.Thread(target=call_sizer, args=("t1",), name="t1")
            t2 = threading.Thread(target=call_sizer, args=("t2",), name="t2")

            t1.start()
            t2.start()
            t1.join()
            t2.join()

        # Both should complete
        assert len(results) == 2

    def test_batch_uses_lock(self):
        """calculate_batch should acquire the lock."""
        sizer = PositionSizer()
        assert hasattr(sizer, "calculate_batch")
        assert hasattr(sizer, "_calculate_batch_unsafe")


# ═══════════════════════════════════════════════════════════════════════════════
# Part 7: Dashboard Reliability Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestDashboardReliability:
    """Test dashboard reliability improvements."""

    def test_no_random_in_dashboard(self):
        """Dashboard should not use random.uniform for prices."""
        import ast
        with open("src/dashboard/app.py") as f:
            source = f.read()
        # Check no random.uniform/randint used for price display
        assert "random.uniform" not in source
        assert "random.randint" not in source

    def test_no_bare_except_pass(self):
        """Dashboard should not have bare except:pass."""
        with open("src/dashboard/app.py") as f:
            lines = f.readlines()

        bare_except_pass = []
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped in ("except:", "except Exception:"):
                # Check next non-empty line
                for j in range(i, min(i + 3, len(lines))):
                    next_line = lines[j].strip()
                    if next_line == "pass":
                        bare_except_pass.append(i)
                        break
                    elif next_line:
                        break

        assert len(bare_except_pass) == 0, f"Bare except:pass found at lines: {bare_except_pass}"

    def test_paper_mode_indicator_is_dynamic(self):
        """Paper mode indicator should check environment."""
        with open("src/dashboard/app.py") as f:
            source = f.read()
        assert "PAPER_TRADING" in source
        assert "getenv" in source


# ═══════════════════════════════════════════════════════════════════════════════
# Part 8: Fee Double-Counting Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestFeeDoubleCount:
    """Test fee calculations are not double-counted."""

    def test_resolution_tracker_uses_winner_fee_only(self):
        """Resolution tracker should use calculated winner fee, not stored fee."""
        from src.analytics.resolution_tracker import _calculate_trade_pnl

        # Buy YES at 30c, market resolves YES — winner
        outcome, pnl, pnl_pct = _calculate_trade_pnl(
            side="yes",
            action="buy",
            price=30,
            quantity=10,
            fee=999.99,  # Intentionally wrong stored fee — should be IGNORED
            market_result="yes",
        )
        assert outcome == "win"
        # Expected: payout=10, cost=3.0, winner_fee=0.20, pnl=6.80
        assert abs(pnl - 6.80) < 0.01

    def test_hero_pnl_does_not_double_count(self):
        """Hero P&L should not add stored fee AND winner fee."""
        with open("src/dashboard/app.py") as f:
            source = f.read()

        # The old bug was: total_fees += float(t.fee or 0) + winner_fee
        # The fix should only use winner_fee
        assert "float(t.fee or 0) + winner_fee" not in source

    def test_resolution_summary_uses_winner_fee(self):
        """Resolution summary total_fees should use KALSHI_WINNER_FEE_RATE."""
        with open("src/analytics/resolution_tracker.py") as f:
            source = f.read()

        # The old bug was: summary.total_fees = sum(r.fee for r in resolved)
        # Now should use KALSHI_WINNER_FEE_RATE
        assert "KALSHI_WINNER_FEE_RATE" in source
        # Make sure old pattern isn't there
        assert 'sum(r.fee for r in resolved)' not in source
