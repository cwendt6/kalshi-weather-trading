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

class TestImpossibleScannerThresholds:
    """Test that impossible scanner thresholds are correctly tightened."""

    def test_max_no_price_lowered(self):
        """MAX_NO_PRICE should be 0.93 (was 0.97)."""
        from src.strategy.impossible_scanner import ImpossibleEventScanner
        assert ImpossibleEventScanner.MAX_NO_PRICE == 0.93

    def test_min_no_price_unchanged(self):
        """MIN_NO_PRICE should still be 0.90."""
        from src.strategy.impossible_scanner import ImpossibleEventScanner
        assert ImpossibleEventScanner.MIN_NO_PRICE == 0.90

    def test_max_yes_price_unchanged(self):
        """MAX_YES_PRICE should still be 0.10."""
        from src.strategy.impossible_scanner import ImpossibleEventScanner
        assert ImpossibleEventScanner.MAX_YES_PRICE == 0.10

    def test_fee_rate(self):
        """Fee rate should be 2%."""
        from src.strategy.impossible_scanner import ImpossibleEventScanner
        assert ImpossibleEventScanner.KALSHI_WINNER_FEE_PCT == 0.02

    def test_max_trades_per_market(self):
        """MAX_TRADES_PER_MARKET should be 1."""
        from src.strategy.impossible_scanner import ImpossibleEventScanner
        assert ImpossibleEventScanner.MAX_TRADES_PER_MARKET == 1


# ═══════════════════════════════════════════════════════════════════════════════
# 1B: Price Validation Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestPriceValidation:
    """Test that price validation rejects defaults and boundary values."""

    def test_rejects_default_no_price_zero(self):
        """Should reject market with no_price=0.0 (default)."""
        from src.strategy.impossible_scanner import ImpossibleEventScanner
        scanner = ImpossibleEventScanner()
        result = scanner._analyze_market({
            "ticker": "TEST-ZERO",
            "title": "Test Market",
            "yes_price": 0.05,
            "no_price": 0.0,
            "category": "test",
        })
        assert result is None

    def test_rejects_default_no_price_one(self):
        """Should reject market with no_price=1.0 (default)."""
        from src.strategy.impossible_scanner import ImpossibleEventScanner
        scanner = ImpossibleEventScanner()
        result = scanner._analyze_market({
            "ticker": "TEST-ONE",
            "title": "Test Market",
            "yes_price": 0.05,
            "no_price": 1.0,
            "category": "test",
        })
        assert result is None

    def test_rejects_none_no_price(self):
        """Should reject market with no_price=None."""
        from src.strategy.impossible_scanner import ImpossibleEventScanner
        scanner = ImpossibleEventScanner()
        result = scanner._analyze_market({
            "ticker": "TEST-NONE",
            "title": "Test Market",
            "yes_price": 0.05,
            "no_price": None,
            "category": "test",
        })
        assert result is None

    def test_rejects_none_yes_price(self):
        """Should reject market with yes_price=None."""
        from src.strategy.impossible_scanner import ImpossibleEventScanner
        scanner = ImpossibleEventScanner()
        result = scanner._analyze_market({
            "ticker": "TEST-YESNONE",
            "title": "Test Market",
            "yes_price": None,
            "no_price": 0.92,
            "category": "test",
        })
        assert result is None

    def test_rejects_97c_too_expensive(self):
        """Should reject 97c NO price (above MAX_NO_PRICE=93c)."""
        from src.strategy.impossible_scanner import ImpossibleEventScanner
        scanner = ImpossibleEventScanner()
        result = scanner._analyze_market({
            "ticker": "TEST-97C",
            "title": "Test Market",
            "yes_price": 0.03,
            "no_price": 0.97,
            "category": "test",
        })
        assert result is None

    def test_accepts_valid_92c(self):
        """Should accept valid 92c NO price."""
        from src.strategy.impossible_scanner import ImpossibleEventScanner
        scanner = ImpossibleEventScanner(min_profit_pct=0.01)
        result = scanner._analyze_market({
            "ticker": "KXNFL-TEST-92C",
            "title": "NFL test market",
            "yes_price": 0.08,
            "no_price": 0.92,
            "category": "Sports",
        })
        assert result is not None
        assert result.no_price == 0.92

    def test_accepts_valid_90c(self):
        """Should accept valid 90c NO price."""
        from src.strategy.impossible_scanner import ImpossibleEventScanner
        scanner = ImpossibleEventScanner(min_profit_pct=0.01)
        result = scanner._analyze_market({
            "ticker": "KXNFL-TEST-90C",
            "title": "NFL test market",
            "yes_price": 0.10,
            "no_price": 0.90,
            "category": "Sports",
        })
        assert result is not None
        assert result.no_price == 0.90


# ═══════════════════════════════════════════════════════════════════════════════
# 1C: Deduplication Tests
# ═══════════════════════════════════════════════════════════════════════════════

class TestDeduplication:
    """Test DB-level trade deduplication."""

    def test_has_existing_position_method_exists(self):
        """Scanner should have _has_existing_position method."""
        from src.strategy.impossible_scanner import ImpossibleEventScanner
        scanner = ImpossibleEventScanner()
        assert hasattr(scanner, '_has_existing_position')
        assert callable(scanner._has_existing_position)

    def test_max_trades_per_market_constant(self):
        """MAX_TRADES_PER_MARKET should be defined."""
        from src.strategy.impossible_scanner import ImpossibleEventScanner
        assert hasattr(ImpossibleEventScanner, 'MAX_TRADES_PER_MARKET')
        assert ImpossibleEventScanner.MAX_TRADES_PER_MARKET == 1


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

class TestMidRangeStrategy:
    """Test mid-range strategy initialization and configuration."""

    def test_strategy_initializes(self):
        from src.strategy.midrange_strategy import MidRangeStrategy
        strategy = MidRangeStrategy()
        assert strategy is not None

    def test_price_range(self):
        from src.strategy.midrange_strategy import MidRangeStrategy
        assert MidRangeStrategy.MIN_PRICE == 0.30
        assert MidRangeStrategy.MAX_PRICE == 0.70

    def test_min_edge(self):
        from src.strategy.midrange_strategy import MidRangeStrategy
        strategy = MidRangeStrategy(min_edge=0.08)
        assert strategy.min_edge == 0.08

    def test_contract_calculation(self):
        from src.strategy.midrange_strategy import MidRangeStrategy
        strategy = MidRangeStrategy()
        # High edge should give more contracts
        high_contracts = strategy._calculate_contracts(0.20, 0.50)
        low_contracts = strategy._calculate_contracts(0.06, 0.50)
        assert high_contracts >= low_contracts

    def test_contract_capped_by_dollars(self):
        from src.strategy.midrange_strategy import MidRangeStrategy
        strategy = MidRangeStrategy(max_position_dollars=10.0)
        # At $0.50/contract, max $10 = 20 contracts
        contracts = strategy._calculate_contracts(0.20, 0.50)
        assert contracts <= 20

    def test_get_stats(self):
        from src.strategy.midrange_strategy import MidRangeStrategy
        strategy = MidRangeStrategy()
        stats = strategy.get_stats()
        assert "min_edge" in stats
        assert "price_range" in stats


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

class TestPositionSizing:
    """Test impossible scanner position sizing with new thresholds."""

    def test_90c_full_size(self):
        """90c NO price should get full position size."""
        from src.strategy.impossible_scanner import ImpossibleEventScanner, ConfidenceLevel
        scanner = ImpossibleEventScanner()
        size = scanner._get_position_size(ConfidenceLevel.HIGH, 0.90)
        assert size == 50.0  # HIGH confidence base × 1.0 multiplier

    def test_92c_medium_size(self):
        """92c NO price should get 0.8x multiplier."""
        from src.strategy.impossible_scanner import ImpossibleEventScanner, ConfidenceLevel
        scanner = ImpossibleEventScanner()
        size = scanner._get_position_size(ConfidenceLevel.HIGH, 0.92)
        assert size == 40.0  # 50 * 0.8

    def test_93c_good_size(self):
        """93c NO price should get 0.6x multiplier."""
        from src.strategy.impossible_scanner import ImpossibleEventScanner, ConfidenceLevel
        scanner = ImpossibleEventScanner()
        size = scanner._get_position_size(ConfidenceLevel.HIGH, 0.93)
        assert size == 30.0  # 50 * 0.6

    def test_above_93c_zero_size(self):
        """Above 93c should get 0 size (filtered by MAX_NO_PRICE)."""
        from src.strategy.impossible_scanner import ImpossibleEventScanner, ConfidenceLevel
        scanner = ImpossibleEventScanner()
        size = scanner._get_position_size(ConfidenceLevel.HIGH, 0.95)
        assert size == 0.0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
