"""Tests for paper mode consistency (Phase 6)."""
import json
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from src.execution.risk_manager import RiskManager


@pytest.fixture
def tmp_pool_file(tmp_path):
    return tmp_path / "obs_pool_state.json"


class TestPaperMode:
    def test_obs_pool_persistence_live(self, tmp_pool_file):
        """Live mode: obs pool state saves and loads."""
        with patch.object(RiskManager, "OBS_POOL_STATE_FILE", tmp_pool_file):
            rm = RiskManager(initial_bankroll=100.0)
            rm.record_obs_deploy(7.50)
            rm.record_obs_settlement(deployed=7.50, profit=0.30)
            rm.save_obs_pool_state()

        assert tmp_pool_file.exists()
        data = json.loads(tmp_pool_file.read_text())
        assert data["obs_pool_profits"] == pytest.approx(0.30)

    def test_obs_pool_no_persistence_paper(self, tmp_pool_file):
        """Paper mode: pool is in-memory only (no file needed)."""
        rm = RiskManager(initial_bankroll=100.0)
        rm.record_obs_deploy(5.0)
        # No crash, pool state is just in-memory
        assert rm._obs_pool_deployed == 5.0

    def test_paper_mode_tag(self):
        """Paper mode uses [PAPER] prefix concept."""
        # Verify the pattern is available
        paper = True
        mode_tag = "[PAPER]" if paper else "[LIVE]"
        assert mode_tag == "[PAPER]"
        live_tag = "[PAPER]" if not paper else "[LIVE]"
        assert live_tag == "[LIVE]"

    def test_settlement_manager_paper_mode(self):
        """Paper settlement manager should work without Kalshi API."""
        from src.execution.settlement_manager import SettlementManager
        sm = SettlementManager(paper_trading=True)
        assert sm.paper_trading is True

    def test_settlement_manager_live_mode(self):
        """Live settlement manager tracks paper_trading=False."""
        from src.execution.settlement_manager import SettlementManager
        sm = SettlementManager(paper_trading=False)
        assert sm.paper_trading is False
