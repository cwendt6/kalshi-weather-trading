"""
Tests for edge safety, scoring, and forecast audit modules.

Covers:
- EdgeScoreCalculator: scoring, time discount, uncertainty, fee-adjusted Kelly
- ForecastAudit: snapshot logging, settlement logging, error distribution
- Safety gates: negative edge rejection, price floor, fee protection
"""

import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from src.analysis.edge_score import (
    EdgeScoreCalculator,
    EdgeScoreResult,
    get_edge_score_calculator,
)
from src.analytics.forecast_audit import ForecastAudit, get_forecast_audit
from src.utils.fees import KALSHI_WINNER_FEE_RATE


# ── Fixtures ──────────────────────────────────────────────────────────


@dataclass
class MockWeatherOpp:
    """Minimal mock matching WeatherOpportunity interface."""

    ticker: str = "KXHIGHNY-26FEB12-T38"
    city: str = "NYC"
    market_date: date = date.today()  # Always "today" so time_discount is stable
    threshold_temp: int = 38
    threshold_type: str = "above"
    nws_forecast_temp: int = 42
    nws_confidence: str = "high"
    market_price: float = 0.80
    our_probability: float = 0.90
    edge: float = 0.10
    recommendation: str = "BUY_YES"
    reasoning: str = "test"
    market_type: str = "temperature"
    trade_type: str = "no_exclusion"


@pytest.fixture
def calculator():
    return EdgeScoreCalculator()


@pytest.fixture
def audit():
    return ForecastAudit()


# ── EdgeScoreCalculator Tests ─────────────────────────────────────────


class TestEdgeScoreCalculator:
    """Tests for the EdgeScore calculator."""

    def test_buy_yes_scoring(self, calculator):
        """BUY_YES opportunity should produce a valid score."""
        opp = MockWeatherOpp(recommendation="BUY_YES", market_price=0.80, our_probability=0.90, edge=0.10)
        result = calculator.score(opp)
        assert result is not None
        assert result.side == "yes"
        assert result.price_cents == 80
        assert result.score > 0

    def test_buy_no_scoring(self, calculator):
        """BUY_NO opportunity should produce a valid score."""
        opp = MockWeatherOpp(
            recommendation="BUY_NO",
            market_price=0.95,
            our_probability=0.02,
            edge=0.07,
        )
        result = calculator.score(opp)
        assert result is not None
        assert result.side == "no"
        assert result.price_cents == 5  # 1.0 - 0.95 = 0.05 → 5c
        assert result.score > 0

    def test_no_trade_filtered(self, calculator):
        """NO_TRADE recommendation should return None."""
        opp = MockWeatherOpp(recommendation="NO_TRADE")
        result = calculator.score(opp)
        assert result is None

    def test_negative_edge_rejected(self, calculator):
        """Negative edge should produce negative Kelly → filtered out."""
        opp = MockWeatherOpp(
            recommendation="BUY_YES",
            market_price=0.90,
            our_probability=0.80,  # Below market
            edge=-0.10,
        )
        result = calculator.score(opp)
        # Kelly is negative when prob < implied → filtered
        assert result is None

    def test_zero_edge_rejected(self, calculator):
        """Zero edge means Kelly=0 → filtered out."""
        opp = MockWeatherOpp(
            recommendation="BUY_YES",
            market_price=0.50,
            our_probability=0.50,
            edge=0.0,
        )
        result = calculator.score(opp)
        # With fees, this is negative EV
        assert result is None

    def test_price_floor_filter(self, calculator):
        """Price cents <= 0 should be filtered."""
        opp = MockWeatherOpp(recommendation="BUY_YES", market_price=0.001)
        result = calculator.score(opp)
        assert result is None

    def test_price_ceiling_filter(self, calculator):
        """Price > 96c should be filtered (fee protection)."""
        opp = MockWeatherOpp(
            recommendation="BUY_YES",
            market_price=0.97,
            our_probability=0.99,
            edge=0.02,
        )
        result = calculator.score(opp)
        assert result is None

    def test_fee_adjusted_kelly(self, calculator):
        """Kelly calculation should account for 2% winner fee."""
        opp = MockWeatherOpp(
            recommendation="BUY_YES",
            market_price=0.50,
            our_probability=0.70,
            edge=0.20,
        )
        result = calculator.score(opp)
        assert result is not None
        # Net payout should be less than gross payout due to fees
        gross_payout = 1.0 - 0.50
        expected_net = gross_payout - KALSHI_WINNER_FEE_RATE * gross_payout
        assert abs(result.net_payout - expected_net) < 0.001

    def test_time_discount_today(self, calculator):
        """Today's markets should get 1.2x time bonus."""
        assert calculator._calc_time_discount(0) == 1.2

    def test_time_discount_tomorrow(self, calculator):
        """Tomorrow's markets should get 1.0x time bonus."""
        assert calculator._calc_time_discount(1) == 1.0

    def test_time_discount_decays(self, calculator):
        """Markets 3+ days out should get decreasing discount."""
        d2 = calculator._calc_time_discount(2)
        d3 = calculator._calc_time_discount(3)
        assert d2 > d3
        assert d2 < 1.0  # Should be less than tomorrow
        assert d3 >= 0.3  # Should not go below minimum

    def test_time_discount_floor(self, calculator):
        """Time discount should never go below 0.3."""
        assert calculator._calc_time_discount(100) == 0.3

    def test_uncertainty_buffer_high_confidence(self, calculator):
        """High NWS confidence should give highest uncertainty buffer."""
        high = calculator._calc_uncertainty_buffer("high")
        medium = calculator._calc_uncertainty_buffer("medium")
        low = calculator._calc_uncertainty_buffer("low")
        assert high > medium > low

    def test_uncertainty_buffer_range(self, calculator):
        """Uncertainty buffer should be between 0.5 and 1.0."""
        for conf in ["high", "medium", "low", "unknown"]:
            val = calculator._calc_uncertainty_buffer(conf)
            assert 0.5 <= val <= 1.0, f"Buffer for {conf} = {val}"

    def test_liquidity_bonus_mid_price(self, calculator):
        """Mid-priced markets (20-80c) should get full liquidity bonus."""
        assert calculator._calc_liquidity_bonus(0.50) == 1.0
        assert calculator._calc_liquidity_bonus(0.30) == 1.0
        assert calculator._calc_liquidity_bonus(0.70) == 1.0

    def test_liquidity_bonus_extreme_price(self, calculator):
        """Extreme-priced markets (<10c or >90c) should get penalty."""
        assert calculator._calc_liquidity_bonus(0.05) == 0.4
        assert calculator._calc_liquidity_bonus(0.95) == 0.4

    def test_trade_type_no_exclusion_bonus(self, calculator):
        """No-exclusion trades with high prob should get 1.3x bonus."""
        opp = MockWeatherOpp(trade_type="no_exclusion")
        bonus = calculator._calc_trade_type_bonus(opp, our_prob=0.95, days_out=0)
        assert bonus == 1.3

    def test_trade_type_unknown_no_bonus(self, calculator):
        """Unknown trade types should get 1.0x (no bonus/penalty)."""
        opp = MockWeatherOpp(trade_type="unknown")
        bonus = calculator._calc_trade_type_bonus(opp, our_prob=0.50, days_out=0)
        assert bonus == 1.0

    def test_to_dict_compatibility(self, calculator):
        """to_dict() should include 'time_bonus' key for backward compat."""
        opp = MockWeatherOpp()
        result = calculator.score(opp)
        assert result is not None
        d = result.to_dict()
        assert "time_bonus" in d
        assert "score" in d
        assert "side" in d
        assert "price_cents" in d

    def test_singleton_factory(self):
        """get_edge_score_calculator should return the same instance."""
        c1 = get_edge_score_calculator()
        c2 = get_edge_score_calculator()
        assert c1 is c2

    def test_directional_edge_not_abs(self, calculator):
        """Edge should be directional (positive for our side), not abs()."""
        # A negative edge should NOT be treated as positive
        opp = MockWeatherOpp(
            recommendation="BUY_YES",
            market_price=0.90,
            our_probability=0.80,
            edge=-0.10,  # Wrong direction!
        )
        result = calculator.score(opp)
        # Should be filtered (Kelly < 0) — NOT scored with abs(edge)
        assert result is None

    def test_no_side_ev_uses_entry_price(self, calculator):
        """NO side should use (1 - market_price) as entry, not YES price."""
        opp = MockWeatherOpp(
            recommendation="BUY_NO",
            market_price=0.95,
            our_probability=0.02,
            edge=0.07,
        )
        result = calculator.score(opp)
        assert result is not None
        assert result.entry_price == pytest.approx(0.05, abs=0.001)
        assert result.price_cents == 5

    def test_higher_edge_higher_score(self, calculator):
        """Higher edge opportunities should score higher, all else equal."""
        opp_low = MockWeatherOpp(
            recommendation="BUY_YES",
            market_price=0.50,
            our_probability=0.60,
            edge=0.10,
        )
        opp_high = MockWeatherOpp(
            recommendation="BUY_YES",
            market_price=0.50,
            our_probability=0.70,
            edge=0.20,
        )
        low = calculator.score(opp_low)
        high = calculator.score(opp_high)
        assert low is not None and high is not None
        assert high.score > low.score


# ── ForecastAudit Tests ───────────────────────────────────────────────


def _mock_db_session(session):
    """Helper to create a generator that yields a context manager wrapping session."""
    class _CM:
        def __enter__(self):
            return session
        def __exit__(self, *args):
            pass
    yield _CM()


class TestForecastAudit:
    """Tests for the ForecastAudit module."""

    @patch("src.analytics.forecast_audit.get_db_session")
    def test_log_snapshot(self, mock_get_db):
        """log_snapshot should add a ForecastSnapshotDB to the session."""
        mock_session = MagicMock()
        mock_get_db.side_effect = lambda: _mock_db_session(mock_session)

        audit = ForecastAudit()
        audit.log_snapshot(
            city="NYC",
            market_date=date(2026, 2, 12),
            forecast_temp=38.0,
            nws_confidence="high",
            source="nws",
            hours_to_close=8.5,
        )

        mock_session.add.assert_called_once()
        mock_session.commit.assert_called_once()
        snap = mock_session.add.call_args[0][0]
        assert snap.city == "NYC"
        assert snap.forecast_temp == 38.0

    @patch("src.analytics.forecast_audit.get_db_session")
    def test_log_settlement_insert(self, mock_get_db):
        """log_settlement should insert when no existing record."""
        mock_session = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value.first.return_value = None
        mock_session.query.return_value = mock_query
        mock_get_db.side_effect = lambda: _mock_db_session(mock_session)

        audit = ForecastAudit()
        audit.log_settlement(
            city="NYC",
            market_date=date(2026, 2, 12),
            settlement_temp=37.0,
        )

        mock_session.add.assert_called_once()
        mock_session.commit.assert_called_once()

    @patch("src.analytics.forecast_audit.get_db_session")
    def test_log_settlement_upsert(self, mock_get_db):
        """log_settlement should update existing record."""
        existing = MagicMock()
        existing.settlement_temp = 36.0
        mock_session = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value.first.return_value = existing
        mock_session.query.return_value = mock_query
        mock_get_db.side_effect = lambda: _mock_db_session(mock_session)

        audit = ForecastAudit()
        audit.log_settlement(
            city="NYC",
            market_date=date(2026, 2, 12),
            settlement_temp=37.5,
        )

        assert existing.settlement_temp == 37.5
        mock_session.add.assert_not_called()  # Update, not insert
        mock_session.commit.assert_called_once()

    @patch("src.analytics.forecast_audit.get_db_session")
    def test_error_distribution_empty(self, mock_get_db):
        """get_error_distribution returns empty when no settlements."""
        mock_session = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value.all.return_value = []
        mock_session.query.return_value = mock_query
        mock_get_db.side_effect = lambda: _mock_db_session(mock_session)

        audit = ForecastAudit()
        stats = audit.get_error_distribution(days_back=7)
        assert stats["count"] == 0
        assert stats["errors"] == []

    def test_error_distribution_math(self):
        """Verify error math with known values (no DB)."""
        # Manually compute expected error stats
        errors = [1.0, -2.0, 3.0, -1.0, 0.5]
        abs_errors = sorted([abs(e) for e in errors])
        n = len(errors)
        mean_error = sum(errors) / n
        mean_abs = sum(abs_errors) / n
        std = (sum((e - mean_error) ** 2 for e in errors) / n) ** 0.5
        median_abs = abs_errors[n // 2]

        assert abs(mean_error - 0.3) < 0.01
        assert abs(mean_abs - 1.5) < 0.01
        assert median_abs == 1.0  # sorted: [0.5, 1.0, 1.0, 2.0, 3.0], idx 2 = 1.0

    def test_forecast_audit_singleton(self):
        """get_forecast_audit should return same instance."""
        a1 = get_forecast_audit()
        a2 = get_forecast_audit()
        assert a1 is a2

    @patch("src.analytics.forecast_audit.get_db_session")
    def test_log_snapshot_handles_exception(self, mock_get_db):
        """log_snapshot should not raise on DB errors."""
        mock_get_db.side_effect = lambda: (_ for _ in ()).throw(Exception("DB error"))

        audit = ForecastAudit()
        # Should not raise
        audit.log_snapshot("NYC", date(2026, 2, 12), 38.0)

    @patch("src.analytics.forecast_audit.get_db_session")
    def test_log_settlement_handles_exception(self, mock_get_db):
        """log_settlement should not raise on DB errors."""
        mock_get_db.side_effect = lambda: (_ for _ in ()).throw(Exception("DB error"))

        audit = ForecastAudit()
        # Should not raise
        audit.log_settlement("NYC", date(2026, 2, 12), 37.0)

    @patch("src.analytics.forecast_audit.get_db_session")
    def test_accuracy_by_lead_time_empty(self, mock_get_db):
        """get_accuracy_by_lead_time returns empty buckets when no data."""
        mock_session = MagicMock()
        mock_session.query.return_value.all.return_value = []
        mock_get_db.side_effect = lambda: _mock_db_session(mock_session)

        audit = ForecastAudit()
        result = audit.get_accuracy_by_lead_time()
        assert result == {"buckets": {}}

    def test_generate_daily_report_no_data(self):
        """generate_daily_report should handle no data gracefully."""
        audit = ForecastAudit()
        # Patch get_error_distribution to return empty
        with patch.object(audit, "get_error_distribution", return_value={"count": 0, "errors": []}):
            with patch.object(audit, "get_accuracy_by_lead_time", return_value={"buckets": {}}):
                report = audit.generate_daily_report()
                assert "No forecast-settlement pairs" in report


# ── Safety Gate Tests ─────────────────────────────────────────────────


class TestSafetyGates:
    """Tests verifying safety gates in scoring/trading logic."""

    def test_fee_protection_97c(self):
        """97c contract should be rejected (fee protection)."""
        calc = EdgeScoreCalculator()
        opp = MockWeatherOpp(
            recommendation="BUY_YES",
            market_price=0.97,
            our_probability=0.99,
            edge=0.02,
        )
        assert calc.score(opp) is None

    def test_fee_protection_96c_allowed(self):
        """96c contract should be allowed (at boundary)."""
        calc = EdgeScoreCalculator()
        opp = MockWeatherOpp(
            recommendation="BUY_YES",
            market_price=0.96,
            our_probability=0.99,
            edge=0.03,
        )
        result = calc.score(opp)
        assert result is not None
        assert result.price_cents == 96

    def test_fee_rate_is_two_percent(self):
        """Kalshi winner fee rate should be 2%."""
        assert KALSHI_WINNER_FEE_RATE == 0.02

    def test_kelly_requires_positive_net_payout(self):
        """When net payout (after fees) <= 0, should be rejected."""
        calc = EdgeScoreCalculator()
        # At 99c, payout = 1c, fee = 0.02c → net = 0.98c (still positive)
        # But 100c is filtered by price ceiling
        opp = MockWeatherOpp(
            recommendation="BUY_YES",
            market_price=0.99,
            our_probability=0.999,
            edge=0.01,
        )
        result = calc.score(opp)
        assert result is None  # Filtered by 96c ceiling

    def test_half_kelly_not_full_kelly(self):
        """Position sizing should use half-Kelly, not full Kelly."""
        calc = EdgeScoreCalculator()
        opp = MockWeatherOpp(
            recommendation="BUY_YES",
            market_price=0.50,
            our_probability=0.70,
            edge=0.20,
        )
        result = calc.score(opp)
        assert result is not None
        assert result.half_kelly == pytest.approx(result.kelly_f / 2, abs=0.001)
        assert result.half_kelly < result.kelly_f
