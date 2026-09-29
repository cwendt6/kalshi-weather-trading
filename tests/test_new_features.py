"""
Tests for new features:
- Impossible scanner economics (1A, 1B, 1C)
- Category inference (2A)
- Mid-range strategy (1D)
- System monitor (4A-4D)

Run: pytest tests/test_new_features.py -v
"""
import pytest
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ═══════════════════════════════════════════════════════════════════════════════
# 1A: Impossible Scanner Threshold Tests
# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
# 1B: Price Validation Tests
# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
# 1C: Deduplication Tests
# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
# 2A: Category Inference Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestCategoryInference:
    """Test ticker-based category inference."""

    def test_sports_nfl(self):
        from src.utils.category_inference import infer_category
        assert infer_category("KXNFL-SUPERBOWL-YES") == "Sports"

    def test_sports_nba(self):
        from src.utils.category_inference import infer_category
        assert infer_category("KXNBA-LAKERS-WIN") == "Sports"

    def test_sports_mves(self):
        """KXMVES prefix (288K sports parlays) should map to Sports."""
        from src.utils.category_inference import infer_category
        assert infer_category("KXMVES-NFL-KC-WIN") == "Sports"

    def test_crypto_btc(self):
        from src.utils.category_inference import infer_category
        assert infer_category("KXBTC-26FEB06-T100000") == "Crypto"

    def test_crypto_eth(self):
        from src.utils.category_inference import infer_category
        assert infer_category("KXETH-26FEB06") == "Crypto"

    def test_weather_high(self):
        from src.utils.category_inference import infer_category
        assert infer_category("KXHIGHNY-26FEB06-T45") == "Climate and Weather"

    def test_weather_low(self):
        from src.utils.category_inference import infer_category
        assert infer_category("KXLOWCHI-26FEB06") == "Climate and Weather"

    def test_weather_snow(self):
        from src.utils.category_inference import infer_category
        assert infer_category("SNOWNY-26FEB06") == "Climate and Weather"

    def test_economics_gdp(self):
        from src.utils.category_inference import infer_category
        assert infer_category("KXGDP-26JAN30-4.5") == "Economics"

    def test_economics_cpi(self):
        from src.utils.category_inference import infer_category
        assert infer_category("KXCPI-26FEB") == "Economics"

    def test_politics_pres(self):
        from src.utils.category_inference import infer_category
        assert infer_category("KXPRES-2028-HARRIS") == "Politics"

    def test_politics_pope(self):
        from src.utils.category_inference import infer_category
        assert infer_category("KXNEWPOPE-ITALIAN") == "Politics"

    def test_unknown_returns_none(self):
        from src.utils.category_inference import infer_category
        assert infer_category("RANDOM-UNKNOWN-TICKER") is None

    def test_get_category_preserves_existing(self):
        """Should keep existing non-unknown category."""
        from src.utils.category_inference import get_category
        assert get_category("KXNFL-TEST", "NFL Game", "Sports") == "Sports"

    def test_get_category_infers_from_unknown(self):
        """Should infer category when existing is 'unknown'."""
        from src.utils.category_inference import get_category
        assert get_category("KXNFL-TEST", "NFL Game", "unknown") == "Sports"

    def test_get_category_title_fallback(self):
        """Should use title keywords when ticker doesn't match."""
        from src.utils.category_inference import get_category
        result = get_category("RANDOM-TICKER", "NFL Super Bowl winner", "unknown")
        assert result == "Sports"

    def test_ncaamb_before_ncaa(self):
        """KXNCAAMB should match before KXNCAA (longest prefix first)."""
        from src.utils.category_inference import infer_category
        assert infer_category("KXNCAAMB-DUKE-WIN") == "Sports"

    def test_case_insensitive(self):
        """Should work with lowercase tickers."""
        from src.utils.category_inference import infer_category
        assert infer_category("kxnfl-test") == "Sports"


# ═══════════════════════════════════════════════════════════════════════════════
# 1D: Mid-Range Strategy Tests
# ═══════════════════════════════════════════════════════════════════════════════



# ═══════════════════════════════════════════════════════════════════════════════
# 4A-4D: System Monitor Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestSystemMonitor:
    """Test system monitoring and alerting."""

    def test_monitor_initializes(self):
        from src.monitoring.system_monitor import SystemMonitor
        monitor = SystemMonitor()
        assert monitor is not None

    def test_default_rules(self):
        from src.monitoring.system_monitor import SystemMonitor
        monitor = SystemMonitor()
        assert len(monitor.rules) > 0
        rule_names = [r.name for r in monitor.rules]
        assert "consecutive_losses" in rule_names
        assert "daily_drawdown" in rule_names
        assert "stale_prices" in rule_names

    def test_record_error(self):
        from src.monitoring.system_monitor import SystemMonitor
        monitor = SystemMonitor()
        monitor.record_error("test_source", "test error message")
        events = monitor.get_recent_events(limit=1)
        assert len(events) == 1
        assert events[0].source == "test_source"
        assert events[0].severity == "error"

    def test_get_recent_events(self):
        from src.monitoring.system_monitor import SystemMonitor
        monitor = SystemMonitor()
        monitor.record_error("src1", "error1")
        monitor.record_error("src2", "error2")
        events = monitor.get_recent_events(limit=5)
        assert len(events) == 2

    def test_get_recent_events_severity_filter(self):
        from src.monitoring.system_monitor import SystemMonitor
        monitor = SystemMonitor()
        monitor.record_error("src", "an error")
        monitor._record_event("info", "info", "src", "an info")
        errors = monitor.get_recent_events(severity="error")
        assert all(e.severity == "error" for e in errors)

    def test_monitoring_summary(self):
        from src.monitoring.system_monitor import SystemMonitor
        monitor = SystemMonitor()
        summary = monitor.get_monitoring_summary()
        assert "alerts_24h" in summary
        assert "errors_1h" in summary
        assert "active_rules" in summary
        assert "strategy_health" in summary

    def test_singleton(self):
        from src.monitoring.system_monitor import get_system_monitor
        m1 = get_system_monitor()
        m2 = get_system_monitor()
        assert m1 is m2


# ═══════════════════════════════════════════════════════════════════════════════
# Position Sizing Tests
# ═══════════════════════════════════════════════════════════════════════════════



if __name__ == "__main__":
    pytest.main([__file__, "-v"])
