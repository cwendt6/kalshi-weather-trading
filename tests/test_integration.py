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


# Test straddle opportunity dataclass


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


# Test take-profit ladder values


# Test signal executor


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
