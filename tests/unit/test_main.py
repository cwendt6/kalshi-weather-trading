"""
Unit tests for main trading loop.
"""
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.main import TradingSystem, run_trading_system


class TestTradingSystem:
    """Tests for TradingSystem."""

    def test_initialization(self):
        """Test trading system initialization."""
        system = TradingSystem(
            paper_trading=True,
            market_scan_interval=300,
            min_edge_threshold=0.05,
            max_positions=10,
        )

        assert system.paper_trading is True
        assert system.market_scan_interval == 300
        assert system.min_edge_threshold == 0.05
        assert system.max_positions == 10
        assert system._running is False

    def test_initialization_live_mode(self):
        """Test initialization in live mode."""
        system = TradingSystem(paper_trading=False)

        assert system.paper_trading is False

    def test_get_stats_initial(self):
        """Test getting initial stats."""
        system = TradingSystem()

        stats = system.get_stats()

        assert stats["running"] is False
        assert stats["paper_trading"] is True
        assert stats["cycles_completed"] == 0
        assert stats["trades_executed"] == 0

    def test_stop(self):
        """Test stop request."""
        system = TradingSystem()

        assert system._shutdown_requested is False

        system.stop()

        assert system._shutdown_requested is True

    @pytest.mark.asyncio
    async def test_scan_markets(self):
        """Test market scanning."""
        system = TradingSystem(min_edge_threshold=0.05)

        # Mock edge calculator
        mock_opp = MagicMock()
        mock_opp.edge = 0.10
        mock_opp.ticker = "TEST"
        mock_opp.side = "yes"
        mock_opp.price = 50
        mock_opp.expected_value = 5.0

        with patch.object(system.edge_calculator, "scan_markets", return_value=[mock_opp]):
            with patch.object(system.alert_manager, "alert_opportunity", return_value=None):
                opportunities = await system._scan_markets()

                assert len(opportunities) == 1
                assert opportunities[0].ticker == "TEST"

    @pytest.mark.asyncio
    async def test_scan_markets_filters_low_edge(self):
        """Test that low edge opportunities are filtered."""
        system = TradingSystem(min_edge_threshold=0.10)

        mock_opp = MagicMock()
        mock_opp.edge = 0.05  # Below threshold

        with patch.object(system.edge_calculator, "scan_markets", return_value=[mock_opp]):
            opportunities = await system._scan_markets()

            assert len(opportunities) == 0

    @pytest.mark.asyncio
    async def test_evaluate_opportunities_trading_paused(self):
        """Test evaluation when trading is paused."""
        system = TradingSystem()

        # Mock risk check - not approved
        mock_risk_result = MagicMock()
        mock_risk_result.approved = False
        mock_risk_result.violations = [MagicMock(value="trading_paused")]

        with patch.object(system.risk_manager, "check_daily_limits", return_value=mock_risk_result):
            recommendations = await system._evaluate_opportunities([MagicMock()])

            assert len(recommendations) == 0

    @pytest.mark.asyncio
    async def test_evaluate_opportunities_at_max_positions(self):
        """Test evaluation when at max positions."""
        system = TradingSystem(max_positions=2)

        mock_risk_result = MagicMock()
        mock_risk_result.approved = True
        mock_risk_result.violations = []

        # Mock 2 existing positions
        mock_positions = [MagicMock(), MagicMock()]

        with patch.object(system.risk_manager, "check_daily_limits", return_value=mock_risk_result):
            with patch.object(system.portfolio, "get_positions", return_value=mock_positions):
                recommendations = await system._evaluate_opportunities([MagicMock()])

                assert len(recommendations) == 0

    @pytest.mark.asyncio
    async def test_execute_trades_success(self):
        """Test successful trade execution."""
        system = TradingSystem()

        mock_opp = MagicMock()
        mock_opp.ticker = "TEST"
        mock_opp.side = "yes"
        mock_opp.price = 50
        mock_opp.edge = 0.10

        mock_size = MagicMock()
        mock_size.recommended_contracts = 10

        recommendations = [{"opportunity": mock_opp, "position_size": mock_size}]

        mock_result = MagicMock()
        mock_result.success = True

        with patch.object(system.executor, "execute", return_value=mock_result):
            with patch.object(system.alert_manager, "alert_trade", return_value=None):
                executed = await system._execute_trades(recommendations)

                assert executed == 1
                assert system._stats["trades_executed"] == 1

    @pytest.mark.asyncio
    async def test_execute_trades_failure(self):
        """Test failed trade execution."""
        system = TradingSystem()

        mock_opp = MagicMock()
        mock_opp.ticker = "TEST"
        mock_opp.side = "yes"
        mock_opp.price = 50

        mock_size = MagicMock()
        mock_size.recommended_contracts = 10

        recommendations = [{"opportunity": mock_opp, "position_size": mock_size}]

        mock_result = MagicMock()
        mock_result.success = False
        mock_result.message = "Risk check failed"

        with patch.object(system.executor, "execute", return_value=mock_result):
            executed = await system._execute_trades(recommendations)

            assert executed == 0

    @pytest.mark.asyncio
    async def test_update_portfolio(self):
        """Test portfolio snapshot update."""
        system = TradingSystem()

        with patch.object(system.portfolio, "save_snapshot") as mock_save:
            await system._update_portfolio()

            mock_save.assert_called_once()
            assert system._last_snapshot is not None

    @pytest.mark.asyncio
    async def test_run_cycle_when_paused(self):
        """Test run cycle when trading is paused."""
        system = TradingSystem()

        with patch.object(system.risk_manager, "is_trading_paused", return_value=True):
            with patch.object(system, "_scan_markets") as mock_scan:
                await system._run_cycle()

                # Should not scan when paused
                mock_scan.assert_not_called()

    @pytest.mark.asyncio
    async def test_shutdown(self):
        """Test graceful shutdown."""
        system = TradingSystem()
        system._stats["start_time"] = datetime.now(timezone.utc)

        # Mock pending orders
        mock_order = MagicMock()
        mock_order.order_id = "ORD-123"

        with patch.object(system.executor, "get_pending_orders", return_value=[mock_order]):
            with patch.object(system.executor, "cancel_order") as mock_cancel:
                with patch.object(system, "_update_portfolio", new_callable=AsyncMock):
                    with patch.object(system.alert_manager, "alert_system"):
                        await system.shutdown()

                        assert system._running is False
                        mock_cancel.assert_called_once_with("ORD-123")


class TestRunTradingSystem:
    """Tests for run_trading_system function."""

    @pytest.mark.asyncio
    async def test_run_trading_system_creates_system(self):
        """Test that run_trading_system creates and runs a system."""
        with patch.object(TradingSystem, "run", new_callable=AsyncMock) as mock_run:
            # Stop immediately
            mock_run.return_value = None

            # This would normally run forever, so we patch to return immediately
            await run_trading_system(
                paper_trading=True,
                market_scan_interval=60,
                min_edge=0.10,
            )

            mock_run.assert_called_once()
