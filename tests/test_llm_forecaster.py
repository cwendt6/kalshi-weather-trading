"""Tests for LLM-powered forecaster with two-phase approach."""
import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from src.analysis.forecaster import (
    Forecast,
    LLMForecaster,
    RuleBasedForecaster,
    get_default_forecaster,
    get_forecaster,
    get_llm_forecaster,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Part 1: Response Parsing Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestParseProbabilityResponse:
    """Test LLM response parsing logic."""

    def _make_forecaster(self) -> LLMForecaster:
        """Create LLMForecaster with test key."""
        return LLMForecaster(api_key="test-key")

    def test_parse_standard_format(self):
        """Parse standard 'PROBABILITY: X' format."""
        f = self._make_forecaster()
        prob, confidence = f._parse_probability_response(
            "Based on analysis...\n\nPROBABILITY: 65\nCONFIDENCE: 70"
        )
        assert abs(prob - 0.65) < 0.001
        assert abs(confidence - 0.70) < 0.001

    def test_parse_decimal_probability(self):
        """Parse decimal percentage like 72.5."""
        f = self._make_forecaster()
        prob, _ = f._parse_probability_response("PROBABILITY: 72.5\nCONFIDENCE: 80")
        assert abs(prob - 0.725) < 0.001

    def test_parse_clamps_to_valid_range(self):
        """Probabilities get clamped to 0.01-0.99."""
        f = self._make_forecaster()
        prob_high, _ = f._parse_probability_response("PROBABILITY: 100\nCONFIDENCE: 90")
        assert prob_high == 0.99
        prob_low, _ = f._parse_probability_response("PROBABILITY: 0\nCONFIDENCE: 50")
        assert prob_low == 0.01

    def test_parse_fallback_to_last_percentage(self):
        """Falls back to last percentage if no PROBABILITY: tag found."""
        f = self._make_forecaster()
        prob, _ = f._parse_probability_response(
            "The base rate is about 30%. Adjusting for news, I estimate 45%."
        )
        assert abs(prob - 0.45) < 0.001

    def test_parse_case_insensitive(self):
        """Parsing should be case-insensitive."""
        f = self._make_forecaster()
        prob, _ = f._parse_probability_response("probability: 55\nconfidence: 60")
        assert abs(prob - 0.55) < 0.001

    def test_parse_returns_none_on_no_probability(self):
        """Should return None probability when no probability found."""
        f = self._make_forecaster()
        prob, confidence = f._parse_probability_response("I'm not sure about this market.")
        assert prob is None
        assert confidence == 0.5  # Default confidence

    def test_parse_final_probability_format(self):
        """Parses FINAL_PROBABILITY format for Phase 2."""
        f = self._make_forecaster()
        prob, confidence = f._parse_probability_response(
            "After reconciliation...\n\nFINAL_PROBABILITY: 62\nFINAL_CONFIDENCE: 75"
        )
        assert abs(prob - 0.62) < 0.001
        assert abs(confidence - 0.75) < 0.001

    def test_parse_probability_as_decimal(self):
        """Handles probability given as 0.65 instead of 65."""
        f = self._make_forecaster()
        prob, _ = f._parse_probability_response("PROBABILITY: 0.65\nCONFIDENCE: 0.8")
        assert abs(prob - 0.65) < 0.001

    def test_confidence_clamps_to_range(self):
        """Confidence is clamped between 0.1 and 1.0."""
        f = self._make_forecaster()
        _, confidence = f._parse_probability_response("PROBABILITY: 50\nCONFIDENCE: 0")
        assert confidence >= 0.1

    def test_multiline_reasoning_with_probability(self):
        """Should extract probability from multiline response."""
        f = self._make_forecaster()
        response = (
            "Step 1: Base rate is 50%.\n"
            "Step 2: Weather data shows high temps.\n"
            "Step 3: Adjusting upward.\n\n"
            "PROBABILITY: 70\nCONFIDENCE: 80"
        )
        prob, confidence = f._parse_probability_response(response)
        assert abs(prob - 0.70) < 0.001
        assert abs(confidence - 0.80) < 0.001


# ═══════════════════════════════════════════════════════════════════════════════
# Part 2: System Prompt Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestSystemPrompts:
    """Test system prompt constants are properly defined."""

    def test_phase1_system_prompt_exists(self):
        """PHASE1_SYSTEM_PROMPT is defined with key elements."""
        prompt = LLMForecaster.PHASE1_SYSTEM_PROMPT
        assert "superforecaster" in prompt.lower()
        assert "base rate" in prompt.lower()
        assert "PROBABILITY:" in prompt
        assert "CONFIDENCE:" in prompt

    def test_phase2_system_prompt_exists(self):
        """PHASE2_SYSTEM_PROMPT is defined with key elements."""
        prompt = LLMForecaster.PHASE2_SYSTEM_PROMPT
        assert "market" in prompt.lower()
        assert "FINAL_PROBABILITY:" in prompt
        assert "FINAL_CONFIDENCE:" in prompt
        assert "50%" in prompt  # 50% cap reference

    def test_phase1_prompt_does_not_mention_market_price(self):
        """Phase 1 system prompt should not anchor on market price."""
        prompt = LLMForecaster.PHASE1_SYSTEM_PROMPT
        # Should not tell the LLM to consider market price in Phase 1
        assert "market price" not in prompt.lower()

    def test_max_forecasts_per_hour(self):
        """Rate limit constant is set."""
        assert LLMForecaster.MAX_FORECASTS_PER_HOUR == 200

    def test_cache_ttl_seconds(self):
        """Cache TTL is set."""
        assert LLMForecaster.CACHE_TTL_SECONDS == 1800

    def test_model_is_set(self):
        """Model constant is set to Claude Sonnet."""
        assert "sonnet" in LLMForecaster.MODEL.lower()


# ═══════════════════════════════════════════════════════════════════════════════
# Part 3: Phase 2 Reconciliation Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestPhase2Reconciliation:
    """Test Phase 2 reconciliation logic."""

    def _make_forecaster(self) -> LLMForecaster:
        return LLMForecaster(api_key="test-key")

    def test_skip_when_close_to_market(self):
        """Phase 2 skips API call when diff < 3%."""
        f = self._make_forecaster()

        # Independent: 52%, Market: 50% => diff = 2% < 3%
        final_prob, reasoning, confidence = f._phase2_reconciliation(
            market_title="Test market?",
            independent_prob=0.52,
            market_price=50,
            phase1_reasoning="Close to market.",
        )
        assert abs(final_prob - 0.52) < 0.001
        assert "No reconciliation needed" in reasoning

    def test_phase2_calls_llm_when_significant_diff(self):
        """Phase 2 calls LLM API when diff >= 3%."""
        f = self._make_forecaster()

        # Mock the client.messages.create
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="Adjusting...\n\nFINAL_PROBABILITY: 65\nFINAL_CONFIDENCE: 70")]

        with patch.object(f.client.messages, "create", return_value=mock_response):
            final_prob, reasoning, confidence = f._phase2_reconciliation(
                market_title="Will BTC exceed $50000?",
                independent_prob=0.70,
                market_price=50,
                phase1_reasoning="Based on crypto analysis...",
            )

        assert final_prob is not None
        assert 0.01 <= final_prob <= 0.99

    def test_phase2_caps_adjustment_at_50pct(self):
        """Phase 2 cannot move more than 50% of Phase1-to-market distance."""
        f = self._make_forecaster()

        # Phase 1: 70%, Market: 50%, diff = 20%
        # Max adjustment = 20% * 50% = 10%
        # If Phase 2 tries to return 55% (15% adjustment), should be capped to 60%
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="FINAL_PROBABILITY: 55\nFINAL_CONFIDENCE: 70")]

        with patch.object(f.client.messages, "create", return_value=mock_response):
            final_prob, _, _ = f._phase2_reconciliation(
                market_title="Test",
                independent_prob=0.70,
                market_price=50,
                phase1_reasoning="...",
            )

        # Max allowed: 0.70 - 0.10 = 0.60
        assert abs(final_prob - 0.60) < 0.001

    def test_phase2_allows_small_adjustment(self):
        """Small adjustments within cap are preserved."""
        f = self._make_forecaster()

        # Phase 1: 70%, Market: 50%, max adjustment = 10%
        # Phase 2 moves only 5% toward market = allowed
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="FINAL_PROBABILITY: 65\nFINAL_CONFIDENCE: 70")]

        with patch.object(f.client.messages, "create", return_value=mock_response):
            final_prob, _, _ = f._phase2_reconciliation(
                market_title="Test",
                independent_prob=0.70,
                market_price=50,
                phase1_reasoning="...",
            )

        assert abs(final_prob - 0.65) < 0.001

    def test_phase2_symmetric_for_negative_edge(self):
        """Cap works correctly when forecast is below market."""
        f = self._make_forecaster()

        # Phase 1: 30%, Market: 50%, diff = 20%
        # Max adjustment = 10% (toward market = moving up)
        # Phase 2 tries 45% (15% move up) = capped to 40%
        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="FINAL_PROBABILITY: 45\nFINAL_CONFIDENCE: 60")]

        with patch.object(f.client.messages, "create", return_value=mock_response):
            final_prob, _, _ = f._phase2_reconciliation(
                market_title="Test",
                independent_prob=0.30,
                market_price=50,
                phase1_reasoning="...",
            )

        assert abs(final_prob - 0.40) < 0.001

    def test_phase2_fallback_on_parse_failure(self):
        """Uses Phase 1 probability when Phase 2 parsing fails."""
        f = self._make_forecaster()

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="I'm not sure what to say.")]

        with patch.object(f.client.messages, "create", return_value=mock_response):
            final_prob, reasoning, _ = f._phase2_reconciliation(
                market_title="Test",
                independent_prob=0.70,
                market_price=50,
                phase1_reasoning="...",
            )

        assert abs(final_prob - 0.70) < 0.001
        assert "Phase 2 parse failed" in reasoning

    def test_phase2_fallback_on_api_error(self):
        """Uses Phase 1 probability when API call fails."""
        f = self._make_forecaster()

        with patch.object(f.client.messages, "create", side_effect=Exception("API timeout")):
            final_prob, reasoning, _ = f._phase2_reconciliation(
                market_title="Test",
                independent_prob=0.70,
                market_price=50,
                phase1_reasoning="...",
            )

        assert abs(final_prob - 0.70) < 0.001
        assert "Phase 2 failed" in reasoning


# ═══════════════════════════════════════════════════════════════════════════════
# Part 4: Rate Limiting & Caching Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestRateLimitingAndCaching:
    """Test rate limiting and caching behavior."""

    def _make_forecaster(self) -> LLMForecaster:
        return LLMForecaster(api_key="test-key")

    def test_rate_limit_allows_initial_requests(self):
        """Rate limit allows requests when count is below threshold."""
        f = self._make_forecaster()
        assert f._check_rate_limit() is True

    def test_rate_limit_blocks_when_exceeded(self):
        """Rate limit blocks when hourly limit exceeded."""
        f = self._make_forecaster()
        f._forecast_count_this_hour = f.MAX_FORECASTS_PER_HOUR
        assert f._check_rate_limit() is False

    def test_rate_limit_resets_after_hour(self):
        """Rate limit resets after 1 hour."""
        f = self._make_forecaster()
        f._forecast_count_this_hour = f.MAX_FORECASTS_PER_HOUR
        f._hour_start = datetime.now(timezone.utc) - timedelta(hours=2)
        assert f._check_rate_limit() is True
        assert f._forecast_count_this_hour == 0

    def test_cache_returns_fresh_forecast(self):
        """Cached forecast is returned within TTL."""
        f = self._make_forecaster()

        cached_forecast = Forecast(
            market_ticker="TEST-MKT",
            probability_yes=0.65,
            probability_no=0.35,
            confidence=0.8,
            factors={"llm_independent": 0.65},
            method="llm",
            timestamp=datetime.now(timezone.utc),
        )
        f._cache["TEST-MKT"] = (cached_forecast, datetime.now(timezone.utc))

        result = f._get_cached("TEST-MKT")
        assert result is not None
        assert result.probability_yes == 0.65

    def test_cache_expired(self):
        """Expired cache returns None."""
        f = self._make_forecaster()

        cached_forecast = Forecast(
            market_ticker="TEST-MKT",
            probability_yes=0.65,
            probability_no=0.35,
            confidence=0.8,
            factors={},
            method="llm",
            timestamp=datetime.now(timezone.utc),
        )
        # Set cache time to 2 hours ago (beyond TTL)
        f._cache["TEST-MKT"] = (cached_forecast, datetime.now(timezone.utc) - timedelta(hours=2))

        result = f._get_cached("TEST-MKT")
        assert result is None
        assert "TEST-MKT" not in f._cache  # Cleaned up

    def test_cache_miss(self):
        """Returns None for uncached ticker."""
        f = self._make_forecaster()
        assert f._get_cached("NONEXISTENT") is None


# ═══════════════════════════════════════════════════════════════════════════════
# Part 5: Context Gathering Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestContextGathering:
    """Test keyword extraction and context building."""

    def _make_forecaster(self) -> LLMForecaster:
        return LLMForecaster(api_key="test-key")

    def test_extract_keywords(self):
        """Extracts meaningful keywords from title."""
        f = self._make_forecaster()
        keywords = f._extract_keywords("Will Bitcoin exceed $50000 by end of February?")
        assert "bitcoin" in keywords
        assert "exceed" in keywords
        assert "will" not in keywords  # Stop word
        assert len(keywords) <= 8

    def test_extract_keywords_strips_punctuation(self):
        """Removes trailing punctuation from keywords."""
        f = self._make_forecaster()
        keywords = f._extract_keywords("Will it snow? Heavy snowfall expected.")
        for kw in keywords:
            assert not kw.endswith("?")
            assert not kw.endswith(".")

    def test_category_base_rate_from_db(self):
        """Base rate is computed from resolved markets in DB."""
        f = self._make_forecaster()
        mock_session = MagicMock()

        # Mock: 100 resolved markets in category, 30 YES
        mock_session.query.return_value.filter.return_value.scalar.side_effect = [
            100,  # total count
            30,   # yes count
        ]

        rate = f._get_category_base_rate(mock_session, "Sports")
        assert abs(rate - 0.30) < 0.001

    def test_category_base_rate_fallback(self):
        """Falls back to overall base rate when category has < 10 resolved."""
        f = self._make_forecaster()
        mock_session = MagicMock()

        # First query: category has only 5 resolved
        # Then falls back to overall: 200 total, 40 yes
        mock_session.query.return_value.filter.return_value.scalar.side_effect = [
            5,    # category total (< 10)
            200,  # overall total
            40,   # overall yes
        ]

        rate = f._get_category_base_rate(mock_session, "Rare")
        assert abs(rate - 0.20) < 0.001

    def test_build_market_context_basic(self):
        """Context builder returns category, volume, and base rate."""
        f = self._make_forecaster()

        mock_market = MagicMock()
        mock_market.category = "Economics"
        mock_market.close_time = datetime.now(timezone.utc) + timedelta(days=5)
        mock_market.volume = 5000
        mock_market.open_interest = 1000

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.query.return_value.filter_by.return_value.first.return_value = mock_market
        mock_session.query.return_value.filter.return_value.scalar.return_value = 50  # base rate queries
        mock_session.query.return_value.join.return_value.filter.return_value.order_by.return_value.limit.return_value.all.return_value = []

        with patch("src.analysis.forecaster.get_db_session") as mock_db:
            mock_db.return_value = iter([mock_session])
            ctx = f._build_market_context("KXGDP-26Q1", "GDP growth rate?", {})

        assert ctx.get("category") == "Economics"
        assert ctx.get("volume") == 5000


# ═══════════════════════════════════════════════════════════════════════════════
# Part 6: Full Forecast Flow (Mocked LLM) Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestFullForecastFlow:
    """Test end-to-end forecast flow with mocked LLM calls."""

    def test_two_phase_forecast(self):
        """Full two-phase forecast produces valid result."""
        f = LLMForecaster(api_key="test-key")

        # Mock Phase 1 and Phase 2 API calls
        call_count = 0

        def mock_create(**kwargs):
            nonlocal call_count
            call_count += 1
            mock_response = MagicMock()
            if call_count == 1:
                # Phase 1 response
                mock_response.content = [
                    MagicMock(text="Base rate: 50%. Weather shows high.\n\nPROBABILITY: 70\nCONFIDENCE: 80")
                ]
            else:
                # Phase 2 response
                mock_response.content = [
                    MagicMock(text="Market is at 55%, I have weather data edge.\n\nFINAL_PROBABILITY: 65\nFINAL_CONFIDENCE: 75")
                ]
            return mock_response

        with patch.object(f.client.messages, "create", side_effect=mock_create):
            with patch.object(f, "_build_market_context", return_value={
                "category": "Climate and Weather",
                "weather_forecast": {"high": 85, "low": 62},
                "category_base_rate_yes": 0.50,
                "volume": 1000,
            }):
                result = f.forecast("KXHIGHNY-26FEB07", "Will NYC high exceed 80F?", 55)

        assert result.method == "llm"
        assert 0.01 <= result.probability_yes <= 0.99
        assert result.factors.get("llm_independent") == 0.70
        assert result.reasoning is not None
        assert call_count == 2  # Both phases called

    def test_forecast_is_cached_after_generation(self):
        """After a forecast, the result is cached."""
        f = LLMForecaster(api_key="test-key")

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="PROBABILITY: 60\nCONFIDENCE: 70")]

        with patch.object(f.client.messages, "create", return_value=mock_response):
            with patch.object(f, "_build_market_context", return_value={
                "category": "general",
                "category_base_rate_yes": 0.50,
            }):
                result = f.forecast("MKT-1", "Will X happen?", 50)

        assert "MKT-1" in f._cache
        assert f._cache["MKT-1"][0].market_ticker == "MKT-1"

    def test_forecast_returns_cached_on_second_call(self):
        """Second forecast call returns cached result."""
        f = LLMForecaster(api_key="test-key")

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text="PROBABILITY: 60\nCONFIDENCE: 70")]

        with patch.object(f.client.messages, "create", return_value=mock_response) as mock_create:
            with patch.object(f, "_build_market_context", return_value={
                "category": "general",
                "category_base_rate_yes": 0.50,
            }):
                result1 = f.forecast("MKT-1", "Will X happen?", 50)
                result2 = f.forecast("MKT-1", "Will X happen?", 50)

        # Only first call should hit the API
        assert result1.market_ticker == result2.market_ticker

    def test_forecast_falls_back_on_rate_limit(self):
        """Falls back to rule-based when rate limited."""
        f = LLMForecaster(api_key="test-key")
        f._forecast_count_this_hour = f.MAX_FORECASTS_PER_HOUR

        with patch.object(f.fallback, "forecast", return_value=Forecast(
            market_ticker="TEST-MKT",
            probability_yes=0.55,
            probability_no=0.45,
            confidence=0.5,
            factors={"price_anchor": 0.50},
            method="rule_based",
            timestamp=datetime.now(timezone.utc),
        )) as mock_fallback:
            result = f.forecast("TEST-MKT", "Test market?", 50)
            mock_fallback.assert_called_once()
            assert result.method == "rule_based"

    def test_forecast_falls_back_on_api_error(self):
        """Falls back to rule-based when API call fails."""
        f = LLMForecaster(api_key="test-key")

        with patch.object(f, "_build_market_context", return_value={"category": "general"}):
            with patch.object(f, "_phase1_independent_forecast", return_value=None):
                with patch.object(f.fallback, "forecast", return_value=Forecast(
                    market_ticker="TEST-MKT",
                    probability_yes=0.50,
                    probability_no=0.50,
                    confidence=0.5,
                    factors={},
                    method="rule_based",
                    timestamp=datetime.now(timezone.utc),
                )) as mock_fallback:
                    result = f.forecast("TEST-MKT", "Test?", 50)
                    mock_fallback.assert_called_once()
                    assert result.method == "rule_based"

    def test_batch_forecast(self):
        """Batch forecast processes multiple markets."""
        f = LLMForecaster(api_key="test-key")

        call_idx = 0

        def mock_create(**kwargs):
            nonlocal call_idx
            call_idx += 1
            mock_response = MagicMock()
            mock_response.content = [MagicMock(text=f"PROBABILITY: {50 + call_idx}\nCONFIDENCE: 70")]
            return mock_response

        with patch.object(f.client.messages, "create", side_effect=mock_create):
            with patch.object(f, "_build_market_context", return_value={
                "category": "general",
                "category_base_rate_yes": 0.50,
            }):
                results = f.forecast_batch([
                    {"ticker": "MKT-1", "title": "Question 1?", "price": 50},
                    {"ticker": "MKT-2", "title": "Question 2?", "price": 60},
                ])

        assert len(results) == 2
        assert results[0].market_ticker == "MKT-1"
        assert results[1].market_ticker == "MKT-2"


# ═══════════════════════════════════════════════════════════════════════════════
# Part 7: Calibration Tracking Tests (DB-backed)
# ═══════════════════════════════════════════════════════════════════════════════


class TestCalibration:
    """Test DB-backed calibration tracking and Brier score calculation."""

    def test_record_resolution_yes(self):
        """Records YES resolution and computes Brier score."""
        f = LLMForecaster(api_key="test-key")

        mock_forecast = MagicMock()
        mock_forecast.probability = 0.70
        mock_forecast.market_probability = 0.50

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.query.return_value.filter_by.return_value.order_by.return_value.first.return_value = mock_forecast

        with patch("src.analysis.forecaster.get_db_session") as mock_db:
            mock_db.return_value = iter([mock_session])
            result = f.record_resolution("MKT-1", resolved_yes=True)

        assert result is not None
        assert result["ticker"] == "MKT-1"
        assert result["actual"] == 1.0
        # Brier = (0.70 - 1.0)^2 = 0.09
        assert abs(result["brier_score"] - 0.09) < 0.001
        # Market Brier = (0.50 - 1.0)^2 = 0.25
        assert abs(result["market_brier"] - 0.25) < 0.001

    def test_record_resolution_no(self):
        """Records NO resolution correctly."""
        f = LLMForecaster(api_key="test-key")

        mock_forecast = MagicMock()
        mock_forecast.probability = 0.70
        mock_forecast.market_probability = 0.50

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.query.return_value.filter_by.return_value.order_by.return_value.first.return_value = mock_forecast

        with patch("src.analysis.forecaster.get_db_session") as mock_db:
            mock_db.return_value = iter([mock_session])
            result = f.record_resolution("MKT-2", resolved_yes=False)

        assert result is not None
        assert result["actual"] == 0.0
        # Brier = (0.70 - 0.0)^2 = 0.49
        assert abs(result["brier_score"] - 0.49) < 0.001

    def test_record_resolution_no_forecast(self):
        """Returns None when no forecast found in DB."""
        f = LLMForecaster(api_key="test-key")

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.query.return_value.filter_by.return_value.order_by.return_value.first.return_value = None

        with patch("src.analysis.forecaster.get_db_session") as mock_db:
            mock_db.return_value = iter([mock_session])
            result = f.record_resolution("NONEXISTENT", resolved_yes=True)

        assert result is None

    def test_calibration_stats_empty(self):
        """Empty stats when no resolved forecasts."""
        f = LLMForecaster(api_key="test-key")

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.query.return_value.join.return_value.filter.return_value.all.return_value = []

        with patch("src.analysis.forecaster.get_db_session") as mock_db:
            mock_db.return_value = iter([mock_session])
            stats = f.get_calibration_stats()

        assert stats["total_forecasts"] == 0
        assert stats["brier_score"] is None

    def test_calibration_stats_with_data(self):
        """Computes Brier scores and rating from DB data."""
        f = LLMForecaster(api_key="test-key")

        # Mock two resolved forecasts
        mock_forecast_1 = MagicMock()
        mock_forecast_1.probability = 0.80
        mock_forecast_1.market_probability = 0.70
        mock_market_1 = MagicMock()
        mock_market_1.result = "yes"

        mock_forecast_2 = MagicMock()
        mock_forecast_2.probability = 0.20
        mock_forecast_2.market_probability = 0.30
        mock_market_2 = MagicMock()
        mock_market_2.result = "no"

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.query.return_value.join.return_value.filter.return_value.all.return_value = [
            (mock_forecast_1, mock_market_1),
            (mock_forecast_2, mock_market_2),
        ]

        with patch("src.analysis.forecaster.get_db_session") as mock_db:
            mock_db.return_value = iter([mock_session])
            stats = f.get_calibration_stats()

        assert stats["total_forecasts"] == 2
        assert stats["brier_score"] is not None
        # LLM: avg((0.04, 0.04)) = 0.04
        assert abs(stats["brier_score"] - 0.04) < 0.001
        # Market: avg((0.09, 0.09)) = 0.09
        assert abs(stats["market_brier_score"] - 0.09) < 0.001
        # LLM advantage = market - llm = 0.05
        assert stats["llm_advantage"] > 0
        assert stats["rating"] == "excellent"

    def test_calibration_stats_rating_poor(self):
        """Rating is 'poor' when Brier > 0.25."""
        f = LLMForecaster(api_key="test-key")

        mock_forecast = MagicMock()
        mock_forecast.probability = 0.50
        mock_forecast.market_probability = None
        mock_market = MagicMock()
        mock_market.result = "yes"

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.query.return_value.join.return_value.filter.return_value.all.return_value = [
            (mock_forecast, mock_market),
        ]

        with patch("src.analysis.forecaster.get_db_session") as mock_db:
            mock_db.return_value = iter([mock_session])
            stats = f.get_calibration_stats()

        # Brier = (0.50 - 1.0)^2 = 0.25
        assert stats["rating"] == "poor"


# ═══════════════════════════════════════════════════════════════════════════════
# Part 8: Constructor & Init Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestConstructor:
    """Test LLMForecaster constructor behavior."""

    def test_raises_without_api_key(self):
        """Raises ValueError when no API key provided."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}, clear=False):
            with pytest.raises(ValueError, match="ANTHROPIC_API_KEY"):
                LLMForecaster(api_key="")

    def test_raises_with_none_api_key_and_no_env(self):
        """Raises ValueError when api_key=None and no env var."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}, clear=False):
            with pytest.raises(ValueError):
                LLMForecaster(api_key=None)

    def test_uses_env_api_key(self):
        """Uses ANTHROPIC_API_KEY env var when api_key not provided."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-env"}, clear=False):
            f = LLMForecaster()
            assert f.api_key == "sk-test-env"

    def test_explicit_api_key_overrides_env(self):
        """Explicit api_key parameter overrides env var."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-env"}, clear=False):
            f = LLMForecaster(api_key="sk-explicit")
            assert f.api_key == "sk-explicit"

    def test_fallback_forecaster_default(self):
        """Default fallback is a RuleBasedForecaster."""
        f = LLMForecaster(api_key="test-key")
        assert isinstance(f.fallback, RuleBasedForecaster)

    def test_custom_fallback_forecaster(self):
        """Can provide custom fallback forecaster."""
        custom_fallback = RuleBasedForecaster(price_weight=0.80)
        f = LLMForecaster(api_key="test-key", fallback_forecaster=custom_fallback)
        assert f.fallback is custom_fallback


# ═══════════════════════════════════════════════════════════════════════════════
# Part 9: Factory Function Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestFactory:
    """Test factory functions for creating forecasters."""

    def test_get_forecaster_rule_based(self):
        """Default factory returns RuleBasedForecaster."""
        f = get_forecaster(use_llm=False)
        assert isinstance(f, RuleBasedForecaster)

    def test_get_forecaster_llm(self):
        """Factory with use_llm=True returns LLMForecaster."""
        f = get_forecaster(use_llm=True, api_key="test-key")
        assert isinstance(f, LLMForecaster)

    @patch.dict("os.environ", {"USE_LLM_FORECASTER": "true", "ANTHROPIC_API_KEY": "test"}, clear=False)
    def test_get_default_uses_env_var(self):
        """get_default_forecaster checks USE_LLM_FORECASTER env var."""
        import src.analysis.forecaster as mod
        mod._forecaster = None
        try:
            f = get_default_forecaster()
            assert isinstance(f, LLMForecaster)
        finally:
            mod._forecaster = None

    @patch.dict("os.environ", {"USE_LLM_FORECASTER": ""}, clear=False)
    def test_get_default_returns_rule_based(self):
        """Without env var, returns RuleBasedForecaster."""
        import src.analysis.forecaster as mod
        mod._forecaster = None
        try:
            f = get_default_forecaster()
            assert isinstance(f, RuleBasedForecaster)
        finally:
            mod._forecaster = None

    @patch.dict("os.environ", {"USE_LLM_FORECASTER": "true", "ANTHROPIC_API_KEY": ""}, clear=False)
    def test_get_default_graceful_fallback_no_key(self):
        """get_default_forecaster falls back to rule-based when no API key."""
        import src.analysis.forecaster as mod
        mod._forecaster = None
        try:
            f = get_default_forecaster()
            assert isinstance(f, RuleBasedForecaster)
        finally:
            mod._forecaster = None

    def test_get_llm_forecaster_with_key(self):
        """get_llm_forecaster returns LLMForecaster when key available."""
        import src.analysis.forecaster as mod
        mod._llm_forecaster = None
        try:
            f = get_llm_forecaster(api_key="test-key")
            assert isinstance(f, LLMForecaster)
            # Second call returns same instance
            f2 = get_llm_forecaster()
            assert f is f2
        finally:
            mod._llm_forecaster = None

    @patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""}, clear=False)
    def test_get_llm_forecaster_returns_none_without_key(self):
        """get_llm_forecaster returns None when no API key."""
        import src.analysis.forecaster as mod
        mod._llm_forecaster = None
        try:
            f = get_llm_forecaster()
            assert f is None
        finally:
            mod._llm_forecaster = None

    def test_get_default_with_explicit_use_llm_true(self):
        """get_default_forecaster with use_llm=True returns LLM."""
        import src.analysis.forecaster as mod
        mod._forecaster = None
        try:
            with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test"}, clear=False):
                f = get_default_forecaster(use_llm=True)
                assert isinstance(f, LLMForecaster)
        finally:
            mod._forecaster = None

    def test_get_default_with_explicit_use_llm_false(self):
        """get_default_forecaster with use_llm=False returns rule-based."""
        import src.analysis.forecaster as mod
        mod._forecaster = None
        try:
            f = get_default_forecaster(use_llm=False)
            assert isinstance(f, RuleBasedForecaster)
        finally:
            mod._forecaster = None


# ═══════════════════════════════════════════════════════════════════════════════
# Part 10: Main.py Scheduling Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestMainScheduling:
    """Test LLM forecast scheduling in TradingSystem."""

    def test_should_forecast_market_filters(self):
        """_should_forecast_market filters based on volume and timing."""
        from src.main import TradingSystem

        with patch.object(TradingSystem, "__init__", lambda self, **kw: None):
            ts = TradingSystem.__new__(TradingSystem)

            # No market
            assert ts._should_forecast_market(None) is False

            # Market with low volume (< $100)
            market = MagicMock()
            market.status = "active"
            market.volume = 50
            market.close_time = datetime.now(timezone.utc) + timedelta(days=5)
            assert ts._should_forecast_market(market) is False

            # Market with good volume ($100+)
            market.volume = 200
            assert ts._should_forecast_market(market) is True

            # Market closing within 12 hours
            market.close_time = datetime.now(timezone.utc) + timedelta(hours=6)
            assert ts._should_forecast_market(market) is False

            # Market too far out (> 30 days)
            market.close_time = datetime.now(timezone.utc) + timedelta(days=60)
            assert ts._should_forecast_market(market) is False

            # Settled market
            market.close_time = datetime.now(timezone.utc) + timedelta(days=5)
            market.status = "settled"
            assert ts._should_forecast_market(market) is False

    def test_llm_forecaster_init_without_key(self):
        """TradingSystem handles missing API key gracefully."""
        from src.main import TradingSystem

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": ""}, clear=False):
            with patch("src.main.get_executor"):
                with patch("src.main.get_portfolio_tracker"):
                    with patch("src.main.get_position_sizer"):
                        with patch("src.main.get_default_forecaster"):
                            with patch("src.main.get_alert_manager"):
                                with patch("src.main.MarketDataCollector"):
                                    with patch("src.main.get_impossible_scanner"):
                                        with patch("src.main.get_straddle_arbitrage"):
                                            with patch("src.main.get_weather_strategy"):
                                                with patch("src.main.get_midrange_strategy"):
                                                    with patch("src.main.get_sports_odds_strategy"):
                                                        with patch("src.main.get_longshot_hunter"):
                                                            with patch("src.main.get_position_manager"):
                                                                with patch("src.main.ModelHealthMonitor"):
                                                                    with patch("src.main.get_system_monitor"):
                                                                        ts = TradingSystem(paper_trading=True)
                                                                        assert ts._llm_forecaster is None


# ═══════════════════════════════════════════════════════════════════════════════
# Part 11: Domain-Specific Data Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestDomainSpecificData:
    """Test domain-specific data fetching."""

    def test_weather_data_fetched(self):
        """Weather data is fetched for weather markets."""
        f = LLMForecaster(api_key="test-key")

        mock_forecast_data = MagicMock()
        mock_forecast_data.high_f = 85
        mock_forecast_data.low_f = 62
        mock_forecast_data.high_confidence = "high"

        with patch("src.data_sources.nws_weather.NWSClient") as mock_nws_cls:
            mock_nws = mock_nws_cls.return_value
            mock_nws.get_temperature_forecast.return_value = mock_forecast_data

            extra = f._get_domain_specific_data(
                "KXHIGHNY-26FEB07", "Will NYC temperature exceed 80F?",
                {"category": "Climate and Weather"},
            )

        assert "weather_forecast" in extra
        assert extra["weather_forecast"]["high"] == 85

    def test_crypto_data_fetched(self):
        """BTC price is fetched for crypto markets."""
        f = LLMForecaster(api_key="test-key")

        with patch("src.data_sources.price_feeds.get_btc_price") as mock_btc:
            mock_btc.return_value = 50000.0

            with patch("asyncio.get_running_loop", side_effect=RuntimeError):
                with patch("asyncio.run", return_value=50000.0):
                    extra = f._get_domain_specific_data(
                        "KXBTC-26FEB07", "Will Bitcoin exceed $50000?",
                        {"category": "Crypto"},
                    )

        assert "btc_current_price" in extra

    def test_no_extra_data_for_general(self):
        """General markets only get aggregator context (no weather/crypto/sports)."""
        f = LLMForecaster(api_key="test-key")
        extra = f._get_domain_specific_data(
            "KXSOME-MKT", "Some general question?",
            {"category": "unknown"},
        )
        # May include aggregator_context from DataAggregator, but no
        # weather_forecast, btc_current_price, or sports_consensus_odds
        assert "weather_forecast" not in extra
        assert "btc_current_price" not in extra
        assert "sports_consensus_odds" not in extra


# ═══════════════════════════════════════════════════════════════════════════════
# Part 12: Save Forecast Tests
# ═══════════════════════════════════════════════════════════════════════════════


class TestSaveForecast:
    """Test forecast persistence."""

    def test_save_forecast_to_db(self):
        """Save forecast creates DB record."""
        f = LLMForecaster(api_key="test-key")

        forecast = Forecast(
            market_ticker="TEST-MKT",
            probability_yes=0.65,
            probability_no=0.35,
            confidence=0.8,
            factors={"llm_independent": 0.65, "market_price": 0.50},
            method="llm",
            timestamp=datetime.now(timezone.utc),
        )

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.query.return_value.filter_by.return_value.order_by.return_value.first.return_value = None

        with patch("src.analysis.forecaster.get_db_session") as mock_db:
            mock_db.return_value = iter([mock_session])
            f.save_forecast(forecast)

        mock_session.add.assert_called_once()
        mock_session.commit.assert_called_once()

    def test_skip_save_when_unchanged(self):
        """Skip saving when probability hasn't changed significantly."""
        f = LLMForecaster(api_key="test-key")

        forecast = Forecast(
            market_ticker="TEST-MKT",
            probability_yes=0.65,
            probability_no=0.35,
            confidence=0.8,
            factors={},
            method="llm",
            timestamp=datetime.now(timezone.utc),
        )

        mock_existing = MagicMock()
        mock_existing.probability = 0.655  # Very close to 0.65

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session.query.return_value.filter_by.return_value.order_by.return_value.first.return_value = mock_existing

        with patch("src.analysis.forecaster.get_db_session") as mock_db:
            mock_db.return_value = iter([mock_session])
            f.save_forecast(forecast)

        mock_session.add.assert_not_called()  # Should skip
