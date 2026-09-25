"""Tests for trade strategy tagging (Phase 5).

Verifies that every executor.execute() call in main.py passes
a strategy= tag so the settlement manager can route PnL correctly.
"""
import ast
import os
import pytest


# Path to main.py source
MAIN_PY = os.path.join(
    os.path.dirname(__file__), os.pardir, os.pardir, "src", "main.py"
)


def _get_main_source() -> str:
    with open(MAIN_PY) as f:
        return f.read()


def _find_execute_calls(source: str):
    """Find all .execute( calls in the source and return line numbers."""
    calls = []
    for i, line in enumerate(source.splitlines(), 1):
        stripped = line.strip()
        # Match executor.execute( or executor.execute_trade(
        if "executor.execute" in stripped and "(" in stripped:
            calls.append((i, stripped))
    return calls


class TestTradeTagging:
    def test_weather_live_execute_tagged(self):
        """The live-mode weather executor.execute() passes strategy=db_strategy."""
        src = _get_main_source()
        calls = _find_execute_calls(src)
        # Find the live path executor.execute call
        live_calls = [
            (ln, line) for ln, line in calls
            if "executor.execute(" in line and "execute_trade" not in line
        ]
        assert len(live_calls) >= 1, "Expected at least one executor.execute() call"
        # All must pass strategy=
        for ln, line in live_calls:
            # Check a window around the call for strategy= param
            src_lines = src.splitlines()
            window = "\n".join(src_lines[max(0, ln-2):ln+10])
            assert "strategy=" in window, (
                f"executor.execute() at line {ln} missing strategy= parameter"
            )

    def test_obs_settled_tagged(self):
        """The obs-settled scanner passes strategy='obs_settled'."""
        src = _get_main_source()
        calls = _find_execute_calls(src)
        obs_calls = [
            (ln, line) for ln, line in calls
            if "execute_trade" in line
        ]
        assert len(obs_calls) >= 1, "Expected at least one execute_trade() call"
        for ln, line in obs_calls:
            src_lines = src.splitlines()
            window = "\n".join(src_lines[max(0, ln-2):ln+10])
            assert "obs_settled" in window, (
                f"execute_trade() at line {ln} missing obs_settled strategy"
            )

    def test_paper_weather_trade_tagged(self):
        """Paper-mode weather trades (direct TradeDB insert) have strategy=db_strategy."""
        src = _get_main_source()
        # Find TradeDB(...) construction blocks in weather scan
        in_weather = False
        found_trade_db = False
        for i, line in enumerate(src.splitlines(), 1):
            if "_scan_and_execute_weather" in line:
                in_weather = True
            if in_weather and "TradeDB(" in line:
                found_trade_db = True
                # Check nearby lines for strategy=
                window = "\n".join(src.splitlines()[max(0, i-2):i+15])
                assert "strategy=" in window, (
                    f"TradeDB() at line {i} missing strategy= parameter"
                )
        assert found_trade_db, "Expected TradeDB() in _scan_and_execute_weather"

    def test_no_untagged_executor_calls(self):
        """Every executor.execute() call in main.py has a strategy= parameter."""
        src = _get_main_source()
        calls = _find_execute_calls(src)
        for ln, line in calls:
            src_lines = src.splitlines()
            # Look at the call and the next 10 lines to find strategy=
            window = "\n".join(src_lines[max(0, ln-2):ln+10])
            assert "strategy=" in window or "strategy" in line, (
                f"Untagged executor call at line {ln}: {line}"
            )
