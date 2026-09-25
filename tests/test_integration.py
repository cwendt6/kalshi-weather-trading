"""
Integration tests for Kalshi Trading System.
Run with: pytest tests/test_integration.py -v
"""

import pytest
from datetime import datetime, timedelta
from unittest.mock import Mock, patch
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# Test imports work
def test_imports():
    """Verify all modules can be imported."""
    from src.strategy.straddle_arbitrage import StraddleArbitrage
    from src.strategy.longshot_hunter import LongshotHunter
    from src.analytics.model_health import ModelHealthMonitor
    from src.execution.position_sizer import get_bankroll
    assert True


# Test bankroll config
def test_bankroll_config():
    """Verify bankroll configuration loads correctly."""
    from src.execution.position_sizer import load_bankroll_config, get_bankroll

    config = load_bankroll_config()
    assert config is not None
    assert 'bankroll' in config

    bankroll = get_bankroll()
    assert bankroll == 1000.0  # $1k as configured


# Test position sizing
def test_position_sizing_limits():
    """Verify position sizing respects limits."""
    from src.execution.position_sizer import PositionSizer

    sizer = PositionSizer()

    # Test that sizer initializes with correct constraints
    assert sizer.max_position_pct <= 0.10  # Max 10% of bankroll
    assert sizer.kelly_fraction <= 1.0  # Kelly fraction capped at 1


# Test straddle arbitrage detection
def test_straddle_scanner_initialization():
    """Verify straddle scanner initializes."""
    from src.strategy.straddle_arbitrage import StraddleArbitrage

    scanner = StraddleArbitrage()
    assert scanner.MIN_PROFIT_MARGIN >= 0.02  # 2% minimum (matches fee rate)
    assert hasattr(scanner, 'find_straddle_opportunities')
    assert hasattr(scanner, 'execute_straddle')


# Test straddle opportunity dataclass
def test_straddle_opportunity_dataclass():
    """Verify straddle opportunity structure."""
    from src.strategy.straddle_arbitrage import StraddleOpportunity

    opp = StraddleOpportunity(
        ticker="TEST-TICKER",
        title="Test Market",
        yes_price=0.48,
        no_price=0.48,
        total_cost=0.96,
        profit_margin=0.04,
        profit_pct=0.0417,
        expires_at=datetime.now() + timedelta(hours=1),
        volume=1000,
    )

    assert opp.total_cost == 0.96
    assert opp.profit_margin == 0.04
    assert opp.profit_pct > 0.04


# Test model health thresholds
def test_model_health_thresholds():
    """Verify model health thresholds are configured."""
    from src.analytics.model_health import ModelHealthMonitor, HealthStatus

    monitor = ModelHealthMonitor()

    # Check thresholds exist
    assert hasattr(monitor, 'thresholds')

    # Check status enum
    assert HealthStatus.EXCELLENT.value == "excellent"
    assert HealthStatus.CRITICAL.value == "critical"


# Test model health calculation
def test_model_health_status():
    """Verify model health status calculation."""
    from src.analytics.model_health import ModelHealthMonitor, HealthStatus

    monitor = ModelHealthMonitor()

    # Check that status methods exist
    assert hasattr(monitor, 'get_health_status')
    assert hasattr(monitor, 'should_pause_trading')
    assert hasattr(monitor, 'get_position_size_multiplier')


# Test longshot hunter criteria
def test_longshot_criteria():
    """Verify longshot criteria are set correctly."""
    from src.strategy.longshot_hunter import LongshotHunter, LongshotCriteria

    hunter = LongshotHunter()

    # Should have criteria attribute
    assert hasattr(hunter, 'criteria')

    # Default criteria should have reasonable values
    criteria = LongshotCriteria()
    assert criteria.MAX_ENTRY_PRICE <= 0.30  # Max 30 cents
    assert criteria.MIN_EDGE_PERCENT > 0


# Test dashboard v3.0 helper functions
def test_dashboard_helpers_v3():
    """Verify dashboard helper functions exist (v3.0)."""
    from src.dashboard.app import get_market_titles, create_terminal_chart, terminal_header

    assert callable(get_market_titles)
    assert callable(create_terminal_chart)
    assert callable(terminal_header)


# Test database models
def test_database_models():
    """Verify database models are defined."""
    from src.data.models import (
        MarketDB,
        TradeDB,
        ForecastDB,
        PositionDB,
    )
    assert MarketDB is not None
    assert TradeDB is not None
    assert ForecastDB is not None


# Test take-profit configuration
def test_take_profit_config():
    """Verify take-profit ladder is configured."""
    from src.execution.signal_executor import TakeProfitConfig

    config = TakeProfitConfig()
    assert hasattr(config, 'enabled')
    assert hasattr(config, 'default_ladder')

    # Default ladder should have entries
    assert len(config.default_ladder) >= 2


# Test take-profit ladder values
def test_take_profit_ladder_values():
    """Verify take-profit ladder has sensible values."""
    from src.execution.signal_executor import TakeProfitConfig

    config = TakeProfitConfig()

    # Check ladder sums to ~100%
    total_portion = sum(portion for _, portion in config.default_ladder)
    assert 0.99 <= total_portion <= 1.01, f"Ladder portions sum to {total_portion}, expected ~1.0"


# Test signal executor
def test_signal_executor_initialization():
    """Verify signal executor initializes."""
    from src.execution.signal_executor import SignalExecutor

    executor = SignalExecutor(paper_trading=True)
    assert executor.paper_trading is True
    assert hasattr(executor, 'execute_signal')
    assert hasattr(executor, 'place_take_profit_orders')


# Test market title helper
def test_market_title_helper():
    """Verify market title helper function exists."""
    from src.dashboard.app import get_market_titles

    assert callable(get_market_titles)


# Test dashboard v3.0 tabs exist
def test_dashboard_v3_tabs():
    """Verify all 5 dashboard tabs exist."""
    from src.dashboard.app import (
        render_dashboard,
        render_positions,
        render_trade_history,
        render_model_health,
        render_config,
    )
    assert callable(render_dashboard)
    assert callable(render_positions)
    assert callable(render_trade_history)
    assert callable(render_model_health)
    assert callable(render_config)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
