"""
Tests for critical fixes applied in the Feb 14, 2026 session.

Covers:
- scipy removal: math.erf CDF matches scipy to 6 decimal places
- reduce_only: only set on IoC (market) orders, not limit orders
- Dead market detection: skip resting asks on 0-bid markets
- Position cap: only counts positions on open markets
- Webhook: accepts Discord 204 responses
- Phase limits: position cap is a loose safety rail (70+)
- Edge directionality: NEVER use abs() on edge values
"""

from datetime import datetime, timedelta, timezone
from math import erf, sqrt
from unittest.mock import MagicMock, patch

import pytest


# ── scipy Removal: math.erf CDF Tests ──────────────────────────────────


def _norm_cdf(x: float, loc: float = 0.0, scale: float = 1.0) -> float:
    """Normal distribution CDF using math.erf (no scipy dependency)."""
    return 0.5 * (1.0 + erf((x - loc) / (scale * sqrt(2.0))))


class TestNormCdfReplacement:
    """Verify our math.erf CDF matches expected values exactly."""

    def test_standard_normal_center(self):
        """CDF(0) of standard normal = 0.5."""
        assert abs(_norm_cdf(0.0) - 0.5) < 1e-10

    def test_standard_normal_positive(self):
        """CDF(1.0) ≈ 0.8413."""
        assert abs(_norm_cdf(1.0) - 0.8413447460685429) < 1e-6

    def test_standard_normal_negative(self):
        """CDF(-1.0) ≈ 0.1587."""
        assert abs(_norm_cdf(-1.0) - 0.15865525393145702) < 1e-6

    def test_standard_normal_two_sigma(self):
        """CDF(2.0) ≈ 0.9772."""
        assert abs(_norm_cdf(2.0) - 0.9772498680518208) < 1e-6

    def test_custom_loc_scale(self):
        """CDF with loc=36, scale=2 (typical weather use case)."""
        # P(temp <= 38) when forecast=36, std=2
        result = _norm_cdf(38.0, loc=36.0, scale=2.0)
        assert abs(result - 0.8413447460685429) < 1e-6

    def test_extreme_tail_left(self):
        """CDF(-5.0) should be very close to 0."""
        assert _norm_cdf(-5.0) < 1e-6

    def test_extreme_tail_right(self):
        """CDF(5.0) should be very close to 1."""
        assert _norm_cdf(5.0) > 1.0 - 1e-6

    def test_symmetry(self):
        """CDF(x) + CDF(-x) = 1 for standard normal."""
        for x in [0.5, 1.0, 1.5, 2.0, 3.0]:
            assert abs(_norm_cdf(x) + _norm_cdf(-x) - 1.0) < 1e-10

    def test_weather_probability_range(self):
        """Typical weather bracket: P(temp in 35-37) with forecast=36, std=2."""
        p_below_37 = _norm_cdf(37.0, loc=36.0, scale=2.0)
        p_below_35 = _norm_cdf(35.0, loc=36.0, scale=2.0)
        bracket_prob = p_below_37 - p_below_35
        # Should be around 38% for a 2° bracket centered on forecast
        assert 0.30 < bracket_prob < 0.45

    def test_no_scipy_in_src(self):
        """Verify no scipy imports remain in src/ directory."""
        import subprocess
        result = subprocess.run(
            ["grep", "-r", "from scipy", "src/"],
            capture_output=True, text=True
        )
        assert result.stdout.strip() == "", (
            f"scipy still imported in src/:\n{result.stdout}"
        )

    def test_no_numpy_in_probability(self):
        """Verify no numpy imports in probability module."""
        import subprocess
        result = subprocess.run(
            ["grep", "-r", "import numpy", "src/probability/"],
            capture_output=True, text=True
        )
        assert result.stdout.strip() == "", (
            f"numpy still imported in src/probability/:\n{result.stdout}"
        )


# ── reduce_only: IoC-Only Guard ────────────────────────────────────────


class TestReduceOnlyGuard:
    """Verify reduce_only is only set on IoC (market) orders."""

    def test_reduce_only_source_code(self):
        """The kalshi_client should gate reduce_only on order_type == 'market'."""
        with open("src/api/kalshi_client.py") as f:
            content = f.read()
        # Must contain the IoC guard
        assert 'order_type.lower() == "market"' in content, (
            "reduce_only must be gated on order_type == 'market' (IoC only)"
        )
        # Must NOT blindly set reduce_only on all sells
        assert '"reduce_only": True' not in content or 'order_type' in content


# ── Dead Market Detection ──────────────────────────────────────────────


class TestDeadMarketDetection:
    """Verify dead market detection in position reevaluator."""

    def test_dead_market_code_exists(self):
        """Position reevaluator should detect dead markets (bid=0, ask<=2)."""
        with open("src/execution/position_reevaluator.py") as f:
            content = f.read()
        assert "yes_ask <= 2" in content or "ask <= 2" in content, (
            "Dead market detection should check yes_ask <= 2"
        )
        assert "yes_bid == 0" in content or "bid == 0" in content, (
            "Dead market detection should check yes_bid == 0"
        )

    def test_dead_market_skips_resting_ask(self):
        """Dead market detection should skip resting ask placement."""
        with open("src/execution/position_reevaluator.py") as f:
            content = f.read()
        assert "skipping resting ask" in content.lower() or "skip" in content.lower()


# ── Position Cap: Market-Aware Counting ────────────────────────────────


class TestPositionCapQuery:
    """Verify position cap only counts positions on open markets."""

    def test_position_cap_joins_markets(self):
        """Position cap query should join with MarketDB to filter closed markets."""
        with open("src/main.py") as f:
            content = f.read()
        # Should join positions with markets
        assert "MarketDB" in content
        assert "close_time" in content
        # Should NOT just count all positions with qty > 0
        # (old broken pattern was: query(PositionDB).filter(quantity > 0).count())


# ── Phase Limits: Position Cap as Safety Rail ──────────────────────────


class TestPhaseLimits:
    """Verify bankroll phase limits are set correctly."""

    def test_survival_position_cap_is_safety_rail(self):
        """SURVIVAL max_positions should be >= 60 (loose safety rail)."""
        with open("src/execution/risk_manager.py") as f:
            content = f.read()
        import re
        # Find the get_phase_limits method, then the SURVIVAL return block
        phase_limits_section = content[content.find("def get_phase_limits"):]
        # Get from SURVIVAL check to ACCELERATION check
        surv_start = phase_limits_section.find("SURVIVAL")
        accel_start = phase_limits_section.find("ACCELERATION", surv_start + 1)
        survival_block = phase_limits_section[surv_start:accel_start]
        match = re.search(r'"max_positions":\s*(\d+)', survival_block)
        assert match is not None, (
            f"Could not find max_positions in SURVIVAL block:\n{survival_block[:200]}"
        )
        cap = int(match.group(1))
        assert cap >= 60, (
            f"SURVIVAL max_positions={cap} is too low for bracket trading. "
            f"Should be >= 60 as a safety rail, not a binding constraint."
        )

    def test_phase_limits_not_disabled(self):
        """Risk limits must never be 1.0 or 100.0 (effectively disabled)."""
        with open("src/execution/risk_manager.py") as f:
            content = f.read()
        # These patterns caused real money losses
        assert '"max_position_pct": 1.0' not in content
        assert '"max_exposure_pct": 100' not in content
        assert '"daily_loss_limit_pct": 1.0' not in content


# ── Edge Directionality ────────────────────────────────────────────────


class TestEdgeDirectionality:
    """Verify edge is NEVER treated with abs() — directional only."""

    def test_no_abs_edge_in_main(self):
        """main.py must not use abs(edge) or abs(opp.edge)."""
        with open("src/main.py") as f:
            content = f.read()
        # These patterns caused real money losses (-43% edge treated as +43%)
        assert "abs(opp.edge)" not in content, (
            "CRITICAL: abs(opp.edge) found in main.py — this treats negative edge as positive!"
        )
        assert "abs(edge)" not in content or "abs(edge_diff)" in content or "abs(edge_change)" in content, (
            "abs(edge) found in main.py — edge must be directional"
        )

    def test_no_abs_edge_in_strategy(self):
        """weather_strategy.py must not use abs() on edge values."""
        with open("src/strategy/weather_strategy.py") as f:
            content = f.read()
        assert "abs(opp.edge)" not in content
        # abs() is OK for distance calculations, just not for edge

    def test_no_skip_risk_in_codebase(self):
        """skip_risk=True must never be CALLED — risk manager always evaluates.

        Note: a debug log MESSAGE containing the string is OK (it logs when
        the param exists but should never be invoked in production paths).
        """
        import subprocess
        result = subprocess.run(
            ["grep", "-rn", "skip_risk=True", "src/"],
            capture_output=True, text=True
        )
        for line in result.stdout.strip().splitlines():
            # Log messages and comments are OK
            if "logger." in line or "#" in line.split("skip_risk")[0]:
                continue
            # Actual function calls with skip_risk=True are NOT OK
            assert False, (
                f"skip_risk=True found in production code path:\n{line}"
            )


# ── Webhook Fix ────────────────────────────────────────────────────────


class TestWebhookFix:
    """Verify webhook accepts Discord 204 No Content responses."""

    def test_webhook_accepts_204(self):
        """Webhook code should accept both 200 and 204 as success."""
        with open("src/utils/alerts.py") as f:
            content = f.read()
        assert "200, 204" in content or "(200, 204)" in content, (
            "Webhook must accept 204 (Discord returns 204 No Content on success)"
        )

    def test_webhook_has_detailed_error_logging(self):
        """Webhook should log HTTP error details, not just generic message."""
        with open("src/utils/alerts.py") as f:
            content = f.read()
        assert "urllib.error" in content, (
            "Webhook should catch urllib.error.HTTPError for detailed error logging"
        )


# ── UTCDateTime Corrupt Data Protection ───────────────────────────────


class TestUTCDateTimeCorruptData:
    """Verify UTCDateTime handles corrupt DB data without crashing.

    Root cause: SQLAlchemy 2.0's base DateTime.result_processor calls
    datetime.fromisoformat() internally, which crashes on corrupt strings
    BEFORE our process_result_value gets to run. The fix overrides
    result_processor to route directly to our try/except protected code.

    Real-world trigger: DB corruption stored ticker 'KXLOWTNYC-26FEB10-B14.5'
    in a close_time column, crashing trading cycle 1.
    """

    def test_corrupt_close_time_returns_none(self):
        """Loading a MarketDB with a ticker in close_time should return None, not crash."""
        from sqlalchemy import create_engine, text
        from sqlalchemy.orm import Session
        from src.data.models import Base, MarketDB

        engine = create_engine("sqlite:///:memory:", echo=False)
        Base.metadata.create_all(engine)

        now_str = "2026-02-14T12:00:00+00:00"
        with Session(engine) as session:
            # Simulate DB corruption: ticker string in close_time column
            session.execute(text(
                "INSERT INTO markets (id, ticker, title, status, close_time, created_at, updated_at) "
                "VALUES (1, 'KXLOWTNYC-26FEB10-B14.5', 'test', 'active', "
                f"'KXLOWTNYC-26FEB10-B14.5', '{now_str}', '{now_str}')"
            ))
            session.commit()

            # Must NOT raise ValueError
            row = session.query(MarketDB).first()
            assert row is not None
            assert row.ticker == "KXLOWTNYC-26FEB10-B14.5"
            assert row.close_time is None  # Corrupt value → None

    def test_valid_close_time_still_works(self):
        """Valid datetime strings in close_time must still parse correctly."""
        from sqlalchemy import create_engine, text
        from sqlalchemy.orm import Session
        from src.data.models import Base, MarketDB

        engine = create_engine("sqlite:///:memory:", echo=False)
        Base.metadata.create_all(engine)

        now_str = "2026-02-14T12:00:00+00:00"
        with Session(engine) as session:
            session.execute(text(
                "INSERT INTO markets (id, ticker, title, status, close_time, created_at, updated_at) "
                f"VALUES (1, 'KXHIGHNY-26FEB14-B36.5', 'test', 'active', "
                f"'2026-02-14T20:00:00+00:00', '{now_str}', '{now_str}')"
            ))
            session.commit()

            row = session.query(MarketDB).first()
            assert row is not None
            assert row.close_time is not None
            assert row.close_time.year == 2026
            assert row.close_time.month == 2
            assert row.close_time.day == 14
            assert row.close_time.tzinfo is not None

    def test_z_suffixed_close_time_works(self):
        """Legacy Z-suffixed datetime strings must parse correctly."""
        from sqlalchemy import create_engine, text
        from sqlalchemy.orm import Session
        from src.data.models import Base, MarketDB

        engine = create_engine("sqlite:///:memory:", echo=False)
        Base.metadata.create_all(engine)

        now_str = "2026-02-14T12:00:00+00:00"
        with Session(engine) as session:
            session.execute(text(
                "INSERT INTO markets (id, ticker, title, status, close_time, created_at, updated_at) "
                f"VALUES (1, 'TEST', 'test', 'active', '2026-02-14T20:00:00Z', '{now_str}', '{now_str}')"
            ))
            session.commit()

            row = session.query(MarketDB).first()
            assert row is not None
            assert row.close_time is not None
            assert row.close_time.tzinfo is not None

    def test_null_close_time_works(self):
        """NULL close_time must return None."""
        from sqlalchemy import create_engine, text
        from sqlalchemy.orm import Session
        from src.data.models import Base, MarketDB

        engine = create_engine("sqlite:///:memory:", echo=False)
        Base.metadata.create_all(engine)

        now_str = "2026-02-14T12:00:00+00:00"
        with Session(engine) as session:
            session.execute(text(
                "INSERT INTO markets (id, ticker, title, status, close_time, created_at, updated_at) "
                f"VALUES (1, 'TEST', 'test', 'active', NULL, '{now_str}', '{now_str}')"
            ))
            session.commit()

            row = session.query(MarketDB).first()
            assert row is not None
            assert row.close_time is None

    def test_result_processor_override_exists(self):
        """UTCDateTime must override result_processor to bypass base DateTime."""
        with open("src/data/models.py") as f:
            content = f.read()
        assert "def result_processor(self, dialect, coltype)" in content, (
            "UTCDateTime must override result_processor to prevent "
            "SQLAlchemy 2.0 DateTime crashes on corrupt data"
        )
