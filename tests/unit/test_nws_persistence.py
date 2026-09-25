"""Tests for NWS daily extreme persistence (Phase 6)."""
import json
import pytest
from datetime import date
from pathlib import Path
from unittest.mock import patch, MagicMock

from src.data_sources.nws_weather import NWSClient, KALSHI_STATIONS


@pytest.fixture
def tmp_extremes_file(tmp_path):
    return tmp_path / "nws_daily_extremes.json"


@pytest.fixture
def nws_client(tmp_extremes_file):
    with patch.object(NWSClient, "EXTREMES_FILE", tmp_extremes_file):
        client = NWSClient()
        yield client


class TestExtremesPersistence:
    def test_extremes_saved_to_file(self, nws_client, tmp_extremes_file):
        """Update a daily extreme -> file is written."""
        nws_client._daily_extremes["NYC"] = {
            "date": date.today().isoformat(), "high": 45.0, "low": 30.0
        }
        nws_client._save_extremes()
        assert tmp_extremes_file.exists()
        data = json.loads(tmp_extremes_file.read_text())
        assert data["NYC"]["high"] == 45.0

    def test_extremes_loaded_on_init(self, tmp_extremes_file):
        """Write extremes file, create new client -> loaded."""
        today = date.today().isoformat()
        tmp_extremes_file.write_text(json.dumps({
            "NYC": {"date": today, "high": 50.0, "low": 25.0}
        }))
        with patch.object(NWSClient, "EXTREMES_FILE", tmp_extremes_file):
            client = NWSClient()
        assert "NYC" in client._daily_extremes
        assert client._daily_extremes["NYC"]["high"] == 50.0

    def test_stale_extremes_discarded(self, tmp_extremes_file):
        """Yesterday's data is ignored on load."""
        tmp_extremes_file.write_text(json.dumps({
            "NYC": {"date": "2020-01-01", "high": 50.0, "low": 25.0}
        }))
        with patch.object(NWSClient, "EXTREMES_FILE", tmp_extremes_file):
            client = NWSClient()
        assert "NYC" not in client._daily_extremes

    @patch("src.data_sources.nws_weather.NWSClient._load_extremes", return_value={})
    def test_bootstrap_fills_gaps(self, mock_load):
        """Bootstrap fetches observations and populates extremes."""
        client = NWSClient()
        mock_response = MagicMock()
        mock_response.json.return_value = {"features": []}
        mock_response.raise_for_status = MagicMock()
        client.session.get = MagicMock(return_value=mock_response)
        client._save_extremes = MagicMock()
        # Should not crash
        client.bootstrap_daily_extremes()
        client._save_extremes.assert_called_once()

    def test_save_survives_restart(self, tmp_extremes_file):
        """Save, simulate restart, verify data persisted."""
        today = date.today().isoformat()
        with patch.object(NWSClient, "EXTREMES_FILE", tmp_extremes_file):
            client1 = NWSClient()
            client1._daily_extremes["MIAMI"] = {
                "date": today, "high": 85.0, "low": 72.0
            }
            client1._save_extremes()
        with patch.object(NWSClient, "EXTREMES_FILE", tmp_extremes_file):
            client2 = NWSClient()
        assert client2._daily_extremes["MIAMI"]["high"] == 85.0
