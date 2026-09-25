"""Quick dashboard smoke test."""

import pytest
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_dashboard_imports():
    """Verify dashboard can be imported."""
    from src.dashboard import app
    assert hasattr(app, 'main')


def test_dashboard_pages_exist():
    """Verify key dashboard functions exist (v3.0 — 5 tabs)."""
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


def test_dashboard_sidebar():
    """Verify sidebar render function exists."""
    from src.dashboard.app import render_sidebar
    assert callable(render_sidebar)


def test_dashboard_helpers():
    """Verify helper functions exist."""
    from src.dashboard.app import (
        create_terminal_chart,
        terminal_header,
        get_market_titles,
    )
    assert callable(create_terminal_chart)
    assert callable(terminal_header)
    assert callable(get_market_titles)


def test_dashboard_colors_defined():
    """Verify theme colors are defined."""
    from src.dashboard.app import COLORS

    assert 'bg_primary' in COLORS
    assert 'accent_green' in COLORS
    assert 'accent_red' in COLORS


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
