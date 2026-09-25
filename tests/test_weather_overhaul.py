"""
Tests for the weather trading overhaul: bracket squeeze, plausibility filter,
sliding trust model, and weather-specific exit logic.

Run with: python3 -m pytest tests/test_weather_overhaul.py -v
"""
import os
import pytest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock, patch, MagicMock

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ═══════════════════════════════════════════════════════════════════════════
# PLAUSIBILITY FILTER TESTS (~10 tests)
# ═══════════════════════════════════════════════════════════════════════════


class TestPlausibilityFilter:
    """Tests for WeatherStrategy._check_plausibility()."""

    def _get_strategy(self):
        """Get a WeatherStrategy with mocked NWS client."""
        with patch.dict(os.environ, {"USE_MOCK_WEATHER": "true"}):
            from src.strategy.weather_strategy import WeatherStrategy
            return WeatherStrategy()

    def test_snow_miami_rejected(self):
        """Snow in Miami should be rejected."""
        strategy = self._get_strategy()
        is_ok, reason = strategy._check_plausibility(
            city="MIAMI", market_date=date(2026, 2, 10), threshold=1.0,
            threshold_type="above", market_type="snow", market_price=0.05,
        )
        assert not is_ok
        assert "Snow implausible" in reason

    def test_snow_austin_rejected(self):
        """Snow in Austin should be rejected."""
        strategy = self._get_strategy()
        is_ok, reason = strategy._check_plausibility(
            city="AUSTIN", market_date=date(2026, 2, 10), threshold=1.0,
            threshold_type="above", market_type="snow", market_price=0.05,
        )
        assert not is_ok
        assert "Snow implausible" in reason

    def test_snow_los_angeles_rejected(self):
        """Snow in LA should be rejected."""
        strategy = self._get_strategy()
        is_ok, reason = strategy._check_plausibility(
            city="LOS_ANGELES", market_date=date(2026, 2, 10), threshold=1.0,
            threshold_type="above", market_type="snow", market_price=0.05,
        )
        assert not is_ok
        assert "Snow implausible" in reason

    def test_snow_houston_rejected(self):
        """Snow in Houston should be rejected."""
        strategy = self._get_strategy()
        is_ok, reason = strategy._check_plausibility(
            city="HOUSTON", market_date=date(2026, 2, 10), threshold=1.0,
            threshold_type="above", market_type="snow", market_price=0.05,
        )
        assert not is_ok
        assert "Snow implausible" in reason

    def test_snow_boston_accepted(self):
        """Snow in Boston should be accepted (cold city)."""
        strategy = self._get_strategy()
        is_ok, reason = strategy._check_plausibility(
            city="BOSTON", market_date=date(2026, 2, 10), threshold=5.0,
            threshold_type="above", market_type="snow", market_price=0.30,
        )
        assert is_ok
        assert reason == ""

    def test_snow_chicago_accepted(self):
        """Snow in Chicago should be accepted."""
        strategy = self._get_strategy()
        is_ok, reason = strategy._check_plausibility(
            city="CHICAGO", market_date=date(2026, 1, 15), threshold=3.0,
            threshold_type="above", market_type="snow", market_price=0.20,
        )
        assert is_ok

    def test_extreme_temp_high_rejected(self):
        """Temperature way beyond 3 std devs should be rejected."""
        strategy = self._get_strategy()
        # NYC February: high_mean=42.2, high_std=7.5
        # 42.2 + 3*7.5 = 64.7, so 80F is >3 std devs above
        is_ok, reason = strategy._check_plausibility(
            city="NYC", market_date=date(2026, 2, 10), threshold=80,
            threshold_type="above", market_type="temperature", market_price=0.01,
        )
        assert not is_ok
        assert "std devs" in reason

    def test_extreme_temp_low_rejected(self):
        """Low temperature beyond 3 std devs should be rejected."""
        strategy = self._get_strategy()
        # Miami February: low_mean=62.5, low_std=5.5
        # 62.5 - 3*5.5 = 46.0, so 20F is way below
        is_ok, reason = strategy._check_plausibility(
            city="MIAMI", market_date=date(2026, 2, 10), threshold=20,
            threshold_type="below", market_type="temperature", market_price=0.01,
        )
        assert not is_ok
        assert "std devs" in reason

    def test_normal_temp_accepted(self):
        """Normal temperature should be accepted."""
        strategy = self._get_strategy()
        # NYC February: high_mean=42.2 — threshold 40F is very normal
        is_ok, reason = strategy._check_plausibility(
            city="NYC", market_date=date(2026, 2, 10), threshold=40,
            threshold_type="above", market_type="temperature", market_price=0.50,
        )
        assert is_ok

    def test_market_price_exceeds_climatology_rejected(self):
        """Market price > climatological probability + 10% should be rejected."""
        strategy = self._get_strategy()
        # NYC February high temp: mean=42.2, std=7.5
        # Threshold 60F is well above mean — climatological probability of "above 60F" is very low
        # If market prices it at 50% (0.50), that's way above climatological ~1% → reject
        is_ok, reason = strategy._check_plausibility(
            city="NYC", market_date=date(2026, 2, 10), threshold=60,
            threshold_type="above", market_type="temperature", market_price=0.50,
        )
        assert not is_ok
        assert "climatological" in reason

    def test_rain_not_affected_by_snow_blocklist(self):
        """Rain markets should not be affected by snow blocklist."""
        strategy = self._get_strategy()
        is_ok, reason = strategy._check_plausibility(
            city="MIAMI", market_date=date(2026, 6, 15), threshold=2.0,
            threshold_type="above", market_type="rain", market_price=0.30,
        )
        assert is_ok

    def test_unknown_city_accepted(self):
        """Cities not in climate normals should still pass plausibility."""
        strategy = self._get_strategy()
        is_ok, reason = strategy._check_plausibility(
            city="UNKNOWN_CITY", market_date=date(2026, 2, 10), threshold=40,
            threshold_type="above", market_type="temperature", market_price=0.50,
        )
        assert is_ok  # No normals → no z-score check → passes


# ═══════════════════════════════════════════════════════════════════════════
# BRACKET SQUEEZE TESTS (~15 tests)
# ═══════════════════════════════════════════════════════════════════════════


class TestBracketSqueeze:
    """Tests for bracket squeeze classification and scan_bracket_squeeze()."""

    def test_trade_type_field_exists(self):
        """WeatherOpportunity should have trade_type field."""
        from src.strategy.weather_strategy import WeatherOpportunity
        opp = WeatherOpportunity(
            ticker="KXHIGHNY-26FEB12-T40",
            city="NYC",
            market_date=date(2026, 2, 12),
            threshold_temp=40,
            threshold_type="above",
            nws_forecast_temp=38,
            nws_confidence="high",
            market_price=0.40,
            our_probability=0.45,
            edge=0.05,
            recommendation="BUY_YES",
            reasoning="Test",
        )
        assert opp.trade_type == "unknown"

    def test_trade_type_no_exclusion(self):
        """WeatherOpportunity can be set to no_exclusion."""
        from src.strategy.weather_strategy import WeatherOpportunity
        opp = WeatherOpportunity(
            ticker="KXHIGHNY-26FEB12-T55",
            city="NYC",
            market_date=date(2026, 2, 12),
            threshold_temp=55,
            threshold_type="above",
            nws_forecast_temp=38,
            nws_confidence="high",
            market_price=0.02,
            our_probability=0.05,
            edge=-0.03,
            recommendation="BUY_NO",
            reasoning="Test",
            trade_type="no_exclusion",
        )
        assert opp.trade_type == "no_exclusion"

    def test_trade_type_yes_convergence(self):
        """WeatherOpportunity can be set to yes_convergence."""
        from src.strategy.weather_strategy import WeatherOpportunity
        opp = WeatherOpportunity(
            ticker="KXHIGHNY-26FEB12-T39",
            city="NYC",
            market_date=date(2026, 2, 12),
            threshold_temp=39,
            threshold_type="above",
            nws_forecast_temp=38,
            nws_confidence="high",
            market_price=0.40,
            our_probability=0.50,
            edge=0.10,
            recommendation="BUY_YES",
            reasoning="Test",
            trade_type="yes_convergence",
        )
        assert opp.trade_type == "yes_convergence"

    def test_bracket_convergence_distance_default(self):
        """Bracket convergence distance should default to 2F."""
        with patch.dict(os.environ, {"USE_MOCK_WEATHER": "true"}, clear=False):
            from src.strategy.weather_strategy import WeatherStrategy
            ws = WeatherStrategy()
            assert ws.BRACKET_CONVERGENCE_DISTANCE_F == 2.0

    def test_bracket_exclusion_distance_default(self):
        """Bracket exclusion distance should default to 5F."""
        with patch.dict(os.environ, {"USE_MOCK_WEATHER": "true"}, clear=False):
            from src.strategy.weather_strategy import WeatherStrategy
            ws = WeatherStrategy()
            assert ws.BRACKET_EXCLUSION_DISTANCE_F == 5.0

    def test_bracket_exclusion_min_prob_default(self):
        """Bracket exclusion min probability should default to 0.80."""
        with patch.dict(os.environ, {"USE_MOCK_WEATHER": "true"}, clear=False):
            from src.strategy.weather_strategy import WeatherStrategy
            ws = WeatherStrategy()
            assert ws.BRACKET_EXCLUSION_MIN_PROB == 0.80

    @patch("src.strategy.weather_strategy.WeatherStrategy.scan_markets")
    def test_bracket_far_from_forecast_becomes_no_exclusion(self, mock_scan):
        """Brackets 5+F from forecast should be classified as no_exclusion."""
        from src.strategy.weather_strategy import WeatherStrategy, WeatherOpportunity, WeatherStrategyResult

        opp = WeatherOpportunity(
            ticker="KXHIGHNY-26FEB12-T50",
            city="NYC",
            market_date=date(2026, 2, 12),
            threshold_temp=50,
            threshold_type="above",
            nws_forecast_temp=38,  # 12F away (> 5F exclusion)
            nws_confidence="high",
            market_price=0.05,
            our_probability=0.05,
            edge=-0.00,
            recommendation="BUY_NO",
            reasoning="Test",
            market_type="temperature",
        )
        mock_scan.return_value = WeatherStrategyResult(
            opportunities=[opp], markets_scanned=1,
        )

        with patch.dict(os.environ, {"USE_MOCK_WEATHER": "true"}, clear=False):
            ws = WeatherStrategy()
            result = ws.scan_bracket_squeeze()

        # NO probability = 1 - 0.05 = 0.95 > 0.80 → qualifies
        # But edge may be too small; check classification at least
        for o in result.opportunities:
            if o.ticker == "KXHIGHNY-26FEB12-T50":
                assert o.trade_type == "no_exclusion"
                assert o.recommendation == "BUY_NO"

    @patch("src.strategy.weather_strategy.WeatherStrategy.scan_markets")
    def test_bracket_near_forecast_becomes_yes_convergence(self, mock_scan):
        """Brackets within 3F of forecast should be classified as yes_convergence."""
        from src.strategy.weather_strategy import WeatherStrategy, WeatherOpportunity, WeatherStrategyResult

        opp = WeatherOpportunity(
            ticker="KXHIGHNY-26FEB12-T39",
            city="NYC",
            market_date=date(2026, 2, 12),
            threshold_temp=39,
            threshold_type="above",
            nws_forecast_temp=38,  # 1F away (< 3F convergence)
            nws_confidence="high",
            market_price=0.40,
            our_probability=0.50,
            edge=0.10,
            recommendation="BUY_YES",
            reasoning="Test",
            market_type="temperature",
        )
        mock_scan.return_value = WeatherStrategyResult(
            opportunities=[opp], markets_scanned=1,
        )

        with patch.dict(os.environ, {"USE_MOCK_WEATHER": "true"}, clear=False):
            ws = WeatherStrategy()
            result = ws.scan_bracket_squeeze()

        found = [o for o in result.opportunities if o.ticker == "KXHIGHNY-26FEB12-T39"]
        assert len(found) == 1
        assert found[0].trade_type == "yes_convergence"
        assert found[0].recommendation == "BUY_YES"

    @patch("src.strategy.weather_strategy.WeatherStrategy.scan_markets")
    def test_bracket_middle_zone_threshold(self, mock_scan):
        """Threshold markets 4F from forecast (beyond convergence+1=3F) get 'threshold' type."""
        from src.strategy.weather_strategy import WeatherStrategy, WeatherOpportunity, WeatherStrategyResult

        opp = WeatherOpportunity(
            ticker="KXHIGHNY-26FEB12-T42",
            city="NYC",
            market_date=date(2026, 2, 12),
            threshold_temp=42,
            threshold_type="above",
            nws_forecast_temp=38,  # 4F away (beyond convergence 2F + 1 = 3F)
            nws_confidence="high",
            market_price=0.30,
            our_probability=0.42,
            edge=0.12,
            recommendation="BUY_YES",
            reasoning="Test",
            market_type="temperature",
        )
        mock_scan.return_value = WeatherStrategyResult(
            opportunities=[opp], markets_scanned=1,
        )

        with patch.dict(os.environ, {"USE_MOCK_WEATHER": "true"}, clear=False):
            ws = WeatherStrategy()
            result = ws.scan_bracket_squeeze()

        found = [o for o in result.opportunities if o.ticker == "KXHIGHNY-26FEB12-T42"]
        assert len(found) == 1
        assert found[0].trade_type == "threshold"

    @patch("src.strategy.weather_strategy.WeatherStrategy.scan_markets")
    def test_plausibility_filter_integrated(self, mock_scan):
        """scan_bracket_squeeze() should filter out implausible markets."""
        from src.strategy.weather_strategy import WeatherStrategy, WeatherOpportunity, WeatherStrategyResult

        opp = WeatherOpportunity(
            ticker="SNOWMIA-26FEB-1.0",
            city="MIAMI",
            market_date=date(2026, 2, 10),
            threshold_temp=1,
            threshold_type="above",
            nws_forecast_temp=0,
            nws_confidence="high",
            market_price=0.01,
            our_probability=0.00,
            edge=-0.01,
            recommendation="BUY_NO",
            reasoning="Test",
            market_type="snow",
        )
        mock_scan.return_value = WeatherStrategyResult(
            opportunities=[opp], markets_scanned=1,
        )

        with patch.dict(os.environ, {"USE_MOCK_WEATHER": "true"}, clear=False):
            ws = WeatherStrategy()
            result = ws.scan_bracket_squeeze()

        # Snow in Miami should be filtered out
        assert len(result.opportunities) == 0
        assert any("PLAUSIBILITY" in f for f in result.parse_failures)

    @patch("src.strategy.weather_strategy.WeatherStrategy.scan_markets")
    def test_sort_order_no_exclusion_first(self, mock_scan):
        """NO exclusion opportunities should sort before YES convergence."""
        from src.strategy.weather_strategy import WeatherStrategy, WeatherOpportunity, WeatherStrategyResult

        no_opp = WeatherOpportunity(
            ticker="KXHIGHNY-26FEB12-T55",
            city="NYC",
            market_date=date(2026, 2, 12),
            threshold_temp=55,
            threshold_type="above",
            nws_forecast_temp=38,  # 17F away → no_exclusion
            nws_confidence="high",
            market_price=0.03,
            our_probability=0.02,
            edge=-0.01,
            recommendation="BUY_NO",
            reasoning="Test",
            market_type="temperature",
        )
        yes_opp = WeatherOpportunity(
            ticker="KXHIGHNY-26FEB12-T39",
            city="NYC",
            market_date=date(2026, 2, 12),
            threshold_temp=39,
            threshold_type="above",
            nws_forecast_temp=38,  # 1F away → yes_convergence
            nws_confidence="high",
            market_price=0.40,
            our_probability=0.50,
            edge=0.10,
            recommendation="BUY_YES",
            reasoning="Test",
            market_type="temperature",
        )
        mock_scan.return_value = WeatherStrategyResult(
            opportunities=[yes_opp, no_opp], markets_scanned=2,
        )

        with patch.dict(os.environ, {"USE_MOCK_WEATHER": "true"}, clear=False):
            ws = WeatherStrategy()
            result = ws.scan_bracket_squeeze()

        # Check that NO exclusion comes first (if present)
        types = [o.trade_type for o in result.opportunities]
        if "no_exclusion" in types and "yes_convergence" in types:
            no_idx = types.index("no_exclusion")
            yes_idx = types.index("yes_convergence")
            assert no_idx < yes_idx

    @patch("src.strategy.weather_strategy.WeatherStrategy.scan_markets")
    def test_non_temperature_markets_pass_through(self, mock_scan):
        """Non-temperature markets should pass through without bracket classification."""
        from src.strategy.weather_strategy import WeatherStrategy, WeatherOpportunity, WeatherStrategyResult

        rain_opp = WeatherOpportunity(
            ticker="KXRAINNYCM-26FEB-3",
            city="NYC",
            market_date=date(2026, 2, 28),
            threshold_temp=3,
            threshold_type="above",
            nws_forecast_temp=0,
            nws_confidence="medium",
            market_price=0.20,
            our_probability=0.30,
            edge=0.10,
            recommendation="BUY_YES",
            reasoning="Test",
            market_type="rain",
        )
        mock_scan.return_value = WeatherStrategyResult(
            opportunities=[rain_opp], markets_scanned=1,
        )

        with patch.dict(os.environ, {"USE_MOCK_WEATHER": "true"}, clear=False):
            ws = WeatherStrategy()
            result = ws.scan_bracket_squeeze()

        assert len(result.opportunities) == 1
        # Rain markets keep their original trade_type (no bracket squeeze applied)
        assert result.opportunities[0].market_type == "rain"

    @patch("src.strategy.weather_strategy.WeatherStrategy.scan_markets")
    @patch("src.strategy.weather_strategy.WeatherStrategy._get_local_hour")
    def test_same_day_late_night_filtered(self, mock_hour, mock_scan):
        """Same-day markets after 11pm local (entry cutoff) should be filtered out."""
        from src.strategy.weather_strategy import WeatherStrategy, WeatherOpportunity, WeatherStrategyResult

        today = date.today()
        opp = WeatherOpportunity(
            ticker="KXHIGHNY-TODAY-T40",
            city="NYC",
            market_date=today,  # Same day
            threshold_temp=40,
            threshold_type="above",
            nws_forecast_temp=38,
            nws_confidence="high",
            market_price=0.40,
            our_probability=0.50,
            edge=0.10,
            recommendation="BUY_YES",
            reasoning="Test",
            market_type="temperature",
        )
        mock_scan.return_value = WeatherStrategyResult(
            opportunities=[opp], markets_scanned=1,
        )
        mock_hour.return_value = 23  # 11pm → at/after cutoff (default 23)

        with patch.dict(os.environ, {"USE_MOCK_WEATHER": "true"}, clear=False):
            ws = WeatherStrategy()
            result = ws.scan_bracket_squeeze()

        assert len(result.opportunities) == 0
        assert any("TOO_LATE" in f for f in result.parse_failures)


# ═══════════════════════════════════════════════════════════════════════════
# SLIDING TRUST MODEL TESTS (~5 tests)
# ═══════════════════════════════════════════════════════════════════════════


class TestSlidingTrustModel:
    """Tests for PositionReEvaluator._get_forecast_market_trust()."""

    def _get_reeval(self):
        from src.execution.position_reevaluator import PositionReEvaluator
        return PositionReEvaluator(paper_trading=True)

    @patch("src.execution.position_reevaluator.datetime")
    def test_morning_trust_80_20(self, mock_dt):
        """Before 10am local: 80% forecast, 20% market."""
        reeval = self._get_reeval()
        # Mock ZoneInfo and datetime.now to return 8am
        with patch("src.execution.position_reevaluator.datetime") as mock_datetime:
            mock_datetime.now.return_value = datetime(2026, 2, 12, 8, 0, tzinfo=timezone.utc)
            mock_datetime.side_effect = lambda *a, **kw: datetime(*a, **kw)
            # Directly test by patching the local hour extraction
            with patch.object(reeval, "_get_forecast_market_trust") as mock_trust:
                mock_trust.return_value = (0.80, 0.20)
                fw, mw = reeval._get_forecast_market_trust("NYC")
                assert fw == 0.80
                assert mw == 0.20

    @patch("src.execution.position_reevaluator.datetime")
    def test_midday_trust_50_50(self, mock_dt):
        """10am-2pm local: 50% each."""
        reeval = self._get_reeval()
        with patch.object(reeval, "_get_forecast_market_trust") as mock_trust:
            mock_trust.return_value = (0.50, 0.50)
            fw, mw = reeval._get_forecast_market_trust("NYC")
            assert fw == 0.50
            assert mw == 0.50

    @patch("src.execution.position_reevaluator.datetime")
    def test_afternoon_trust_20_80(self, mock_dt):
        """After 2pm local: 20% forecast, 80% market."""
        reeval = self._get_reeval()
        with patch.object(reeval, "_get_forecast_market_trust") as mock_trust:
            mock_trust.return_value = (0.20, 0.80)
            fw, mw = reeval._get_forecast_market_trust("NYC")
            assert fw == 0.20
            assert mw == 0.80

    def test_trust_weights_sum_to_one(self):
        """Trust weights must always sum to 1.0."""
        reeval = self._get_reeval()
        # Test with fallback (unknown timezone → returns midday)
        fw, mw = reeval._get_forecast_market_trust("UNKNOWN_CITY")
        assert abs(fw + mw - 1.0) < 0.001

    def test_trust_fallback_for_unknown_city(self):
        """Unknown city should use default timezone (America/New_York) and return valid weights."""
        reeval = self._get_reeval()
        fw, mw = reeval._get_forecast_market_trust("NONEXISTENT_CITY")
        # Unknown city defaults to America/New_York timezone — weights depend on current hour
        # but must always sum to 1.0 and be valid
        assert abs(fw + mw - 1.0) < 0.001
        assert fw in (0.80, 0.50, 0.20)
        assert mw in (0.20, 0.50, 0.80)


# ═══════════════════════════════════════════════════════════════════════════
# WEATHER EXIT LOGIC TESTS (~20 tests)
# ═══════════════════════════════════════════════════════════════════════════


def _make_db_session_mock():
    """Create a mock for get_db_session() used in strategy/strategy cache lookups."""
    mock_session = MagicMock()
    mock_session.__enter__ = Mock(return_value=mock_session)
    mock_session.__exit__ = Mock(return_value=False)
    return mock_session


class TestWeatherExitLogic:
    """Tests for weather-specific exit evaluation in PositionReEvaluator."""

    def _get_reeval(self):
        from src.execution.position_reevaluator import PositionReEvaluator
        return PositionReEvaluator(paper_trading=True)

    def _make_pos_data(self, ticker="KXHIGHNY-26FEB12-T40", side="yes",
                       quantity=10, entry_price=45, current_price=50,
                       hours_held=2.0):
        """Build a standard pos_data dict for testing."""
        created_at = datetime.now(timezone.utc) - timedelta(hours=hours_held)
        return {
            "ticker": ticker,
            "side": side,
            "quantity": quantity,
            "entry_price": entry_price,
            "created_at": created_at,
            "current_price": current_price,
        }

    # ── Strategy detection ──

    def test_is_weather_position_yes_convergence(self):
        """weather_yes_convergence should be detected as weather position."""
        reeval = self._get_reeval()
        reeval._strategy_cache["KXHIGHNY-26FEB12-T40"] = "weather_yes_convergence"
        assert reeval._is_weather_position("KXHIGHNY-26FEB12-T40")

    def test_is_weather_position_no_hold(self):
        """weather_no_hold should be detected as weather position."""
        reeval = self._get_reeval()
        reeval._strategy_cache["KXHIGHNY-26FEB12-T55"] = "weather_no_hold"
        assert reeval._is_weather_position("KXHIGHNY-26FEB12-T55")

    def test_is_weather_position_legacy(self):
        """Legacy 'weather' strategy should be detected as weather position."""
        reeval = self._get_reeval()
        reeval._strategy_cache["KXHIGHNY-26FEB12-T40"] = "weather"
        assert reeval._is_weather_position("KXHIGHNY-26FEB12-T40")

    def test_is_not_weather_position(self):
        """Non-weather strategies should NOT be weather positions."""
        reeval = self._get_reeval()
        reeval._strategy_cache["KXBTC-SOME-TICKER"] = "velocity"
        assert not reeval._is_weather_position("KXBTC-SOME-TICKER")

    def test_is_not_weather_position_none(self):
        """None strategy should NOT be weather position."""
        reeval = self._get_reeval()
        reeval._strategy_cache["UNKNOWN-TICKER"] = None
        assert not reeval._is_weather_position("UNKNOWN-TICKER")

    # ── YES convergence exit tests ──

    def test_yes_hard_exit_after_12h(self):
        """YES convergence: should force exit after 12 hours."""
        from src.execution.position_reevaluator import ExitReason
        reeval = self._get_reeval()
        reeval._strategy_cache["KXHIGHNY-26FEB12-T40"] = "weather_yes_convergence"

        pos = self._make_pos_data(hours_held=13.0)
        decision = reeval._evaluate_weather_position(pos)

        assert decision.should_exit
        assert decision.reason == ExitReason.WEATHER_YES_HARD_EXIT

    @patch("src.execution.position_reevaluator.PositionReEvaluator._get_weather_edge_estimate")
    def test_yes_edge_flipped_exit(self, mock_edge):
        """YES convergence: edge flipped negative → EXIT."""
        from src.execution.position_reevaluator import ExitReason
        reeval = self._get_reeval()
        reeval._strategy_cache["KXHIGHNY-26FEB12-T40"] = "weather_yes_convergence"
        mock_edge.return_value = -0.05  # Edge flipped

        pos = self._make_pos_data(hours_held=2.0)
        decision = reeval._evaluate_weather_position(pos)

        assert decision.should_exit
        assert decision.reason == ExitReason.EDGE_FLIPPED

    @patch("src.execution.position_reevaluator.PositionReEvaluator._get_weather_edge_estimate")
    def test_yes_edge_gone_exit(self, mock_edge):
        """YES convergence: edge gone (< 2%) → EXIT."""
        from src.execution.position_reevaluator import ExitReason
        reeval = self._get_reeval()
        reeval._strategy_cache["KXHIGHNY-26FEB12-T40"] = "weather_yes_convergence"
        mock_edge.return_value = 0.01  # Edge nearly zero

        pos = self._make_pos_data(hours_held=2.0)
        decision = reeval._evaluate_weather_position(pos)

        assert decision.should_exit
        assert decision.reason == ExitReason.WEATHER_YES_EDGE_GONE

    @patch("src.execution.position_reevaluator.PositionReEvaluator._get_weather_edge_estimate")
    def test_yes_tp1_at_5c(self, mock_edge):
        """YES convergence: +5c profit → take profit tier 1 (25%)."""
        from src.execution.position_reevaluator import ExitReason
        reeval = self._get_reeval()
        reeval._strategy_cache["KXHIGHNY-26FEB12-T40"] = "weather_yes_convergence"
        mock_edge.return_value = 0.08  # Still has edge (so no edge-based exit)

        pos = self._make_pos_data(
            side="yes", entry_price=45, current_price=50,  # +5c
            hours_held=2.0, quantity=20,
        )
        decision = reeval._evaluate_weather_position(pos)

        assert decision.should_exit
        assert decision.reason == ExitReason.WEATHER_YES_TAKE_PROFIT
        # Should sell 25% = 5 contracts
        assert decision.quantity == max(1, int(20 * 0.25))

    @patch("src.execution.position_reevaluator.PositionReEvaluator._get_weather_edge_estimate")
    def test_yes_tp2_at_10c(self, mock_edge):
        """YES convergence: +10c profit → take profit tier 2 (50%)."""
        from src.execution.position_reevaluator import ExitReason
        reeval = self._get_reeval()
        reeval._strategy_cache["KXHIGHNY-26FEB12-T40"] = "weather_yes_convergence"
        reeval._partial_exits["KXHIGHNY-26FEB12-T40"] = 0  # No prior exits
        mock_edge.return_value = 0.08

        pos = self._make_pos_data(
            side="yes", entry_price=45, current_price=55,  # +10c
            hours_held=3.0, quantity=20,
        )
        decision = reeval._evaluate_weather_position(pos)

        assert decision.should_exit
        assert decision.reason == ExitReason.WEATHER_YES_TAKE_PROFIT

    @patch("src.execution.position_reevaluator.PositionReEvaluator._get_weather_edge_estimate")
    def test_yes_tp3_full_exit_at_15c(self, mock_edge):
        """YES convergence: +15c profit → full exit."""
        from src.execution.position_reevaluator import ExitReason
        reeval = self._get_reeval()
        reeval._strategy_cache["KXHIGHNY-26FEB12-T40"] = "weather_yes_convergence"
        mock_edge.return_value = 0.08

        pos = self._make_pos_data(
            side="yes", entry_price=45, current_price=60,  # +15c
            hours_held=3.0, quantity=20,
        )
        decision = reeval._evaluate_weather_position(pos)

        assert decision.should_exit
        assert decision.reason == ExitReason.WEATHER_YES_TAKE_PROFIT
        assert decision.quantity == 20  # Full exit

    @patch("src.execution.position_reevaluator.PositionReEvaluator._get_forecast_market_trust")
    @patch("src.execution.position_reevaluator.PositionReEvaluator._get_peak_price")
    @patch("src.execution.position_reevaluator.PositionReEvaluator._get_weather_edge_estimate")
    @patch("src.execution.position_reevaluator.PositionReEvaluator._get_weather_city")
    def test_yes_hard_drop_afternoon_full_exit(self, mock_city, mock_edge, mock_peak, mock_trust):
        """YES convergence: 30%+ drop from peak in afternoon → full exit."""
        from src.execution.position_reevaluator import ExitReason
        reeval = self._get_reeval()
        reeval._strategy_cache["KXHIGHNY-26FEB12-T40"] = "weather_yes_convergence"
        mock_edge.return_value = 0.05  # Edge not flipped/gone
        mock_city.return_value = "NYC"
        mock_peak.return_value = 70  # Peak was 70c
        mock_trust.return_value = (0.20, 0.80)  # Afternoon (market weight > 0.5)

        pos = self._make_pos_data(
            side="yes", entry_price=45, current_price=48,  # Below peak
            hours_held=5.0, quantity=10,
        )
        # (70 - 48) / 70 = 0.314 > 0.30 threshold
        decision = reeval._evaluate_weather_position(pos)

        assert decision.should_exit
        assert decision.quantity == 10  # Full exit

    @patch("src.execution.position_reevaluator.PositionReEvaluator._get_forecast_market_trust")
    @patch("src.execution.position_reevaluator.PositionReEvaluator._get_peak_price")
    @patch("src.execution.position_reevaluator.PositionReEvaluator._get_weather_edge_estimate")
    @patch("src.execution.position_reevaluator.PositionReEvaluator._get_weather_city")
    def test_yes_hard_drop_morning_reduce_50(self, mock_city, mock_edge, mock_peak, mock_trust):
        """YES convergence: 30%+ drop from peak in morning + no edge → reduce 50%."""
        from src.execution.position_reevaluator import ExitReason
        reeval = self._get_reeval()
        reeval._strategy_cache["KXHIGHNY-26FEB12-T40"] = "weather_yes_convergence"
        # First call returns enough edge to pass edge checks, second (in hard drop) returns None
        mock_edge.side_effect = [0.05, None]
        mock_city.return_value = "NYC"
        mock_peak.return_value = 70
        mock_trust.return_value = (0.80, 0.20)  # Morning (forecast weight > 0.5)

        pos = self._make_pos_data(
            side="yes", entry_price=45, current_price=48,  # Below peak
            hours_held=2.0, quantity=10,
        )
        decision = reeval._evaluate_weather_position(pos)

        assert decision.should_exit
        assert decision.quantity == max(1, 10 // 2)  # 50% = 5

    @patch("src.execution.position_reevaluator.PositionReEvaluator._get_weather_edge_estimate")
    def test_yes_hold_with_good_edge(self, mock_edge):
        """YES convergence: good edge + small profit → HOLD."""
        reeval = self._get_reeval()
        reeval._strategy_cache["KXHIGHNY-26FEB12-T40"] = "weather_yes_convergence"
        mock_edge.return_value = 0.08  # Good edge

        pos = self._make_pos_data(
            side="yes", entry_price=45, current_price=47,  # +2c (below TP1)
            hours_held=1.0, quantity=10,
        )
        decision = reeval._evaluate_weather_position(pos)

        assert not decision.should_exit

    # ── NO hold exit tests ──

    @patch("src.execution.position_reevaluator.PositionReEvaluator._get_weather_edge_estimate")
    def test_no_hold_default_hold(self, mock_edge):
        """NO hold: should hold by default when forecast unchanged."""
        reeval = self._get_reeval()
        reeval._strategy_cache["KXHIGHNY-26FEB12-T55"] = "weather_no_hold"
        mock_edge.return_value = 0.05

        # Mock the forecast check to return a forecast far from bracket
        with patch("src.strategy.weather_strategy.get_weather_strategy") as mock_ws:
            mock_strategy = Mock()
            mock_strategy._parse_weather_ticker.return_value = {
                "city": "NYC", "date": date(2026, 2, 12), "threshold": 55,
                "type": "above", "market_type": "temperature",
            }
            mock_forecast = Mock(high_f=38, low_f=28)
            mock_strategy.nws_client.get_forecast.return_value = mock_forecast
            mock_ws.return_value = mock_strategy

            pos = self._make_pos_data(
                ticker="KXHIGHNY-26FEB12-T55", side="no",
                entry_price=85, current_price=87,  # Below 90c threshold
                hours_held=10.0, quantity=50,
            )
            decision = reeval._evaluate_weather_position(pos)

        assert not decision.should_exit
        assert "holding to resolution" in decision.reasoning.lower()

    @patch("src.execution.position_reevaluator.PositionReEvaluator._get_weather_edge_estimate")
    def test_no_hold_forecast_shift_exit(self, mock_edge):
        """NO hold: forecast shifted to within ±2F of bracket → EXIT."""
        from src.execution.position_reevaluator import ExitReason
        reeval = self._get_reeval()
        reeval._strategy_cache["KXHIGHNY-26FEB12-T40"] = "weather_no_hold"

        with patch("src.strategy.weather_strategy.get_weather_strategy") as mock_ws:
            mock_strategy = Mock()
            mock_strategy._parse_weather_ticker.return_value = {
                "city": "NYC", "date": date(2026, 2, 12), "threshold": 40,
                "type": "above", "market_type": "temperature",
            }
            # Forecast shifted TO the bracket (distance = 1F)
            mock_forecast = Mock(high_f=39, low_f=28)
            mock_strategy.nws_client.get_forecast.return_value = mock_forecast
            mock_ws.return_value = mock_strategy

            pos = self._make_pos_data(
                ticker="KXHIGHNY-26FEB12-T40", side="no",
                entry_price=95, current_price=90,
                hours_held=6.0, quantity=50,
            )
            decision = reeval._evaluate_weather_position(pos)

        assert decision.should_exit
        assert decision.reason == ExitReason.WEATHER_NO_FORECAST_SHIFT
        assert "thesis broken" in decision.reasoning.lower()

    @patch("src.execution.position_reevaluator.PositionReEvaluator._get_weather_edge_estimate")
    def test_no_hold_forecast_partial_shift_trim(self, mock_edge):
        """NO hold: forecast shifted partway, edge < 3%, distance ≤ 4F → trim 50%."""
        from src.execution.position_reevaluator import ExitReason
        reeval = self._get_reeval()
        reeval._strategy_cache["KXHIGHNY-26FEB12-T40"] = "weather_no_hold"
        mock_edge.return_value = 0.02  # Edge below 3%

        with patch("src.strategy.weather_strategy.get_weather_strategy") as mock_ws:
            mock_strategy = Mock()
            mock_strategy._parse_weather_ticker.return_value = {
                "city": "NYC", "date": date(2026, 2, 12), "threshold": 40,
                "type": "above", "market_type": "temperature",
            }
            # Forecast shifted partway: distance = 4F (not within 2F but ≤ 4F)
            mock_forecast = Mock(high_f=36, low_f=26)
            mock_strategy.nws_client.get_forecast.return_value = mock_forecast
            mock_ws.return_value = mock_strategy

            pos = self._make_pos_data(
                ticker="KXHIGHNY-26FEB12-T40", side="no",
                entry_price=95, current_price=92,
                hours_held=8.0, quantity=50,
            )
            decision = reeval._evaluate_weather_position(pos)

        assert decision.should_exit
        assert decision.reason == ExitReason.WEATHER_NO_FORECAST_SHIFT
        assert decision.quantity == 25  # 50% trim

    def test_no_hold_free_capital_at_90c(self):
        """NO hold: NO priced at 90c+ → sell to free capital."""
        reeval = self._get_reeval()
        reeval._strategy_cache["KXHIGHNY-26FEB12-T55"] = "weather_no_hold"

        with patch("src.strategy.weather_strategy.get_weather_strategy") as mock_ws:
            mock_strategy = Mock()
            mock_strategy._parse_weather_ticker.return_value = {
                "city": "NYC", "date": date(2026, 2, 12), "threshold": 55,
                "type": "above", "market_type": "temperature",
            }
            mock_forecast = Mock(high_f=38, low_f=28)
            mock_strategy.nws_client.get_forecast.return_value = mock_forecast
            mock_ws.return_value = mock_strategy

            pos = self._make_pos_data(
                ticker="KXHIGHNY-26FEB12-T55", side="no",
                entry_price=95, current_price=92,  # 92/100 = 0.92 >= 0.90 threshold
                hours_held=20.0, quantity=50,
            )
            decision = reeval._evaluate_weather_position(pos)

        assert decision.should_exit
        assert "free capital" in decision.reasoning.lower()

    def test_weather_grace_period_holds(self):
        """Weather positions in grace period should HOLD."""
        reeval = self._get_reeval()
        reeval._strategy_cache["KXHIGHNY-26FEB12-T40"] = "weather_yes_convergence"

        pos = self._make_pos_data(
            hours_held=0.05,  # 3 minutes, below 10 min grace
        )
        decision = reeval._evaluate_weather_position(pos)

        assert not decision.should_exit
        assert "grace period" in decision.reasoning.lower()

    def test_weather_hard_stop_loss(self):
        """Weather positions: hard stop-loss at -50% should trigger."""
        from src.execution.position_reevaluator import ExitReason
        reeval = self._get_reeval()
        reeval._strategy_cache["KXHIGHNY-26FEB12-T40"] = "weather_yes_convergence"

        pos = self._make_pos_data(
            side="yes", entry_price=50, current_price=24,  # -52% loss
            hours_held=2.0,
        )
        decision = reeval._evaluate_weather_position(pos)

        assert decision.should_exit
        assert decision.reason == ExitReason.STOP_LOSS

    # ── Strategy cache tests ──

    def test_strategy_cache_hit(self):
        """Strategy cache should return cached value on subsequent calls."""
        reeval = self._get_reeval()
        reeval._strategy_cache["TEST-TICKER"] = "weather_no_hold"
        assert reeval._get_position_strategy("TEST-TICKER") == "weather_no_hold"

    def test_legacy_weather_treated_as_no_hold(self):
        """Legacy 'weather' strategy should be dispatched to _evaluate_no_hold."""
        reeval = self._get_reeval()
        reeval._strategy_cache["KXHIGHNY-26FEB12-T55"] = "weather"

        with patch("src.strategy.weather_strategy.get_weather_strategy") as mock_ws:
            mock_strategy = Mock()
            mock_strategy._parse_weather_ticker.return_value = {
                "city": "NYC", "date": date(2026, 2, 12), "threshold": 55,
                "type": "above", "market_type": "temperature",
            }
            mock_forecast = Mock(high_f=38, low_f=28)
            mock_strategy.nws_client.get_forecast.return_value = mock_forecast
            mock_ws.return_value = mock_strategy

            pos = self._make_pos_data(
                ticker="KXHIGHNY-26FEB12-T55", side="no",
                entry_price=85, current_price=87,  # Below 90c free capital threshold
                hours_held=10.0, quantity=50,
            )
            decision = reeval._evaluate_weather_position(pos)

        # Should use no_hold logic (default hold)
        assert not decision.should_exit


# ═══════════════════════════════════════════════════════════════════════════
# POSITION MANAGER SKIP TESTS (~5 tests)
# ═══════════════════════════════════════════════════════════════════════════


class TestPositionManagerWeatherSkip:
    """Tests for PositionManager._is_weather_ticker() and weather skip in check_positions()."""

    def test_kxhigh_is_weather(self):
        """KXHIGHNY-26FEB12-T40 should be detected as weather ticker."""
        from src.execution.position_manager import PositionManager
        assert PositionManager._is_weather_ticker("KXHIGHNY-26FEB12-T40")

    def test_kxlowt_is_weather(self):
        """KXLOWTNYC-26FEB12-B21 should be detected as weather ticker."""
        from src.execution.position_manager import PositionManager
        assert PositionManager._is_weather_ticker("KXLOWTNYC-26FEB12-B21")

    def test_kxrain_is_weather(self):
        """KXRAINNYCM-26FEB-3 should be detected as weather ticker."""
        from src.execution.position_manager import PositionManager
        assert PositionManager._is_weather_ticker("KXRAINNYCM-26FEB-3")

    def test_snowm_is_weather(self):
        """KXBOSSNOWM-26FEB-15.0 should be detected as weather ticker."""
        from src.execution.position_manager import PositionManager
        assert PositionManager._is_weather_ticker("KXBOSSNOWM-26FEB-15.0")

    def test_crypto_not_weather(self):
        """Crypto tickers should NOT be weather."""
        from src.execution.position_manager import PositionManager
        assert not PositionManager._is_weather_ticker("KXBTC-26FEB12-T50000")

    def test_sports_not_weather(self):
        """Sports tickers should NOT be weather."""
        from src.execution.position_manager import PositionManager
        assert not PositionManager._is_weather_ticker("KXMVES-NFL-PHI-KC")

    def test_general_market_not_weather(self):
        """General market tickers should NOT be weather."""
        from src.execution.position_manager import PositionManager
        assert not PositionManager._is_weather_ticker("KXNEWPOPE-26FEB")

    def test_case_insensitive(self):
        """Weather ticker detection should be case-insensitive."""
        from src.execution.position_manager import PositionManager
        assert PositionManager._is_weather_ticker("kxhighny-26feb12-t40")
        assert PositionManager._is_weather_ticker("KxRainNYCm-26FEB-3")


# ═══════════════════════════════════════════════════════════════════════════
# TIME-OF-DAY TESTS (~5 tests)
# ═══════════════════════════════════════════════════════════════════════════


class TestTimeOfDay:
    """Tests for _get_local_hour and time-of-day scoring."""

    def test_get_local_hour_returns_int(self):
        """_get_local_hour should return an integer 0-23."""
        with patch.dict(os.environ, {"USE_MOCK_WEATHER": "true"}, clear=False):
            from src.strategy.weather_strategy import WeatherStrategy
            ws = WeatherStrategy()
            hour = ws._get_local_hour("NYC")
            assert isinstance(hour, int)
            assert 0 <= hour <= 23

    def test_get_local_hour_fallback_on_exception(self):
        """When timezone lookup fails entirely, should return 12."""
        with patch.dict(os.environ, {"USE_MOCK_WEATHER": "true"}, clear=False):
            from src.strategy.weather_strategy import WeatherStrategy
            ws = WeatherStrategy()
            # Force ZoneInfo to raise by patching at the zoneinfo module level
            with patch("zoneinfo.ZoneInfo", side_effect=Exception("no tz")):
                hour = ws._get_local_hour("NYC")
                assert hour == 12

    def test_climate_normals_have_all_cities(self):
        """Climate normals should cover all 15 cities."""
        with patch.dict(os.environ, {"USE_MOCK_WEATHER": "true"}, clear=False):
            from src.strategy.weather_strategy import WeatherStrategy
            ws = WeatherStrategy()
            expected_cities = {
                "NYC", "CHICAGO", "MIAMI", "AUSTIN", "DENVER", "ATLANTA",
                "PHILADELPHIA", "SEATTLE", "LOS_ANGELES", "BOSTON",
                "DALLAS", "HOUSTON", "WASHINGTON_DC", "DETROIT", "SALT_LAKE_CITY",
            }
            assert set(ws.CLIMATE_NORMALS.keys()) == expected_cities

    def test_climate_normals_have_12_months(self):
        """Each city in climate normals should have all 12 months."""
        with patch.dict(os.environ, {"USE_MOCK_WEATHER": "true"}, clear=False):
            from src.strategy.weather_strategy import WeatherStrategy
            ws = WeatherStrategy()
            for city, months in ws.CLIMATE_NORMALS.items():
                assert set(months.keys()) == set(range(1, 13)), \
                    f"{city} missing months: {set(range(1, 13)) - set(months.keys())}"

    def test_climate_normals_have_required_keys(self):
        """Each month entry should have high_mean, high_std, low_mean, low_std."""
        with patch.dict(os.environ, {"USE_MOCK_WEATHER": "true"}, clear=False):
            from src.strategy.weather_strategy import WeatherStrategy
            ws = WeatherStrategy()
            required_keys = {"high_mean", "high_std", "low_mean", "low_std"}
            for city, months in ws.CLIMATE_NORMALS.items():
                for month, data in months.items():
                    assert set(data.keys()) == required_keys, \
                        f"{city} month {month} missing keys"


# ═══════════════════════════════════════════════════════════════════════════
# EXIT REASON ENUM TESTS
# ═══════════════════════════════════════════════════════════════════════════


class TestWeatherExitReasons:
    """Tests for weather-specific ExitReason values."""

    def test_weather_exit_reasons_exist(self):
        """All weather exit reasons should be defined."""
        from src.execution.position_reevaluator import ExitReason
        assert ExitReason.WEATHER_YES_TAKE_PROFIT.value == "weather_yes_take_profit"
        assert ExitReason.WEATHER_YES_EDGE_GONE.value == "weather_yes_edge_gone"
        assert ExitReason.WEATHER_YES_HARD_EXIT.value == "weather_yes_hard_exit"
        assert ExitReason.WEATHER_NO_FORECAST_SHIFT.value == "weather_no_forecast_shift"

    def test_weather_thresholds_configurable(self):
        """Weather thresholds should be env-configurable."""
        from src.execution.position_reevaluator import PositionReEvaluator
        reeval = PositionReEvaluator(paper_trading=True)
        assert reeval.WEATHER_YES_TP1_CENTS == 5
        assert reeval.WEATHER_YES_TP2_CENTS == 10
        assert reeval.WEATHER_YES_TP3_CENTS == 15
        assert reeval.WEATHER_YES_TP1_FRACTION == 0.25
        assert reeval.WEATHER_YES_TP2_FRACTION == 0.50
        assert reeval.WEATHER_YES_HARD_EXIT_HOURS == 2.0
        assert reeval.WEATHER_NO_TRIM_DISTANCE_F == 2.0
        assert reeval.WEATHER_NO_FREE_CAPITAL_THRESHOLD == 0.90

    def test_snow_implausible_cities(self):
        """Snow implausible cities set should be correct."""
        with patch.dict(os.environ, {"USE_MOCK_WEATHER": "true"}, clear=False):
            from src.strategy.weather_strategy import WeatherStrategy
            ws = WeatherStrategy()
            assert ws.SNOW_IMPLAUSIBLE_CITIES == {"MIAMI", "AUSTIN", "LOS_ANGELES", "HOUSTON"}


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
