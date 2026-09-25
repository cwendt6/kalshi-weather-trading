"""
Forecast audit module: tracks forecast accuracy over time.

Logs forecast snapshots at each scan cycle and compares to
actual settlement values to validate that our edge is real.

Usage:
    audit = get_forecast_audit()
    audit.log_snapshot("NYC", date(2026, 2, 12), 38.0, "high", "openweather", 8.5)
    # ... later when market resolves ...
    audit.log_settlement("NYC", date(2026, 2, 12), 37.0, "nws")
    # ... for reporting ...
    stats = audit.get_error_distribution("NYC", days_back=30)
"""

from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func

from src.data.database import get_db_session
from src.data.models import ForecastSnapshotDB, MarketDB, SettlementDB
from src.utils.logging import logger


class ForecastAudit:
    """Logs forecast snapshots and compares to settlement values."""

    def log_snapshot(
        self,
        city: str,
        market_date: date,
        forecast_temp: float,
        nws_confidence: str = "unknown",
        source: str = "unknown",
        hours_to_close: float = 0.0,
    ) -> None:
        """Log a forecast snapshot with timestamp."""
        try:
            with next(get_db_session()) as session:
                snap = ForecastSnapshotDB(
                    city=city,
                    market_date=market_date,
                    forecast_temp=forecast_temp,
                    nws_confidence=nws_confidence,
                    source=source,
                    hours_to_close=hours_to_close,
                )
                session.add(snap)
                session.commit()
        except Exception as e:
            logger.debug(f"Failed to log forecast snapshot: {e}")

    def log_settlement(
        self,
        city: str,
        market_date: date,
        settlement_temp: float,
        settlement_source: str = "nws",
    ) -> None:
        """Log the actual settlement value when a market resolves."""
        try:
            with next(get_db_session()) as session:
                # Upsert: update if exists, insert if not
                existing = session.query(SettlementDB).filter(
                    SettlementDB.city == city,
                    SettlementDB.market_date == market_date,
                ).first()
                if existing:
                    existing.settlement_temp = settlement_temp
                    existing.settlement_source = settlement_source
                    existing.recorded_at = datetime.now(timezone.utc)
                else:
                    settlement = SettlementDB(
                        city=city,
                        market_date=market_date,
                        settlement_temp=settlement_temp,
                        settlement_source=settlement_source,
                    )
                    session.add(settlement)
                session.commit()
        except Exception as e:
            logger.debug(f"Failed to log settlement: {e}")

    def get_error_distribution(
        self,
        city: Optional[str] = None,
        days_back: int = 30,
    ) -> Dict[str, Any]:
        """
        Return forecast error stats: mean, std, median, percentiles.

        Joins forecast_snapshots with settlements to compute errors.
        Only uses the LAST forecast snapshot per (city, market_date) pair
        (closest to settlement time).
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

        try:
            with next(get_db_session()) as session:
                # Get all settlements in the window
                settle_query = session.query(SettlementDB).filter(
                    SettlementDB.recorded_at >= cutoff,
                )
                if city:
                    settle_query = settle_query.filter(SettlementDB.city == city)

                settlements = settle_query.all()
                if not settlements:
                    return {"count": 0, "errors": []}

                errors = []
                for s in settlements:
                    # Get the last forecast snapshot for this city+date
                    last_snap = session.query(ForecastSnapshotDB).filter(
                        ForecastSnapshotDB.city == s.city,
                        ForecastSnapshotDB.market_date == s.market_date,
                    ).order_by(ForecastSnapshotDB.snapshot_time.desc()).first()

                    if last_snap:
                        error = last_snap.forecast_temp - s.settlement_temp
                        errors.append({
                            "city": s.city,
                            "date": str(s.market_date),
                            "forecast": last_snap.forecast_temp,
                            "actual": s.settlement_temp,
                            "error": error,
                            "abs_error": abs(error),
                            "hours_to_close": last_snap.hours_to_close,
                        })

                if not errors:
                    return {"count": 0, "errors": []}

                abs_errors = sorted([e["abs_error"] for e in errors])
                raw_errors = [e["error"] for e in errors]
                n = len(abs_errors)

                return {
                    "count": n,
                    "mean_error": sum(raw_errors) / n,
                    "mean_abs_error": sum(abs_errors) / n,
                    "std_error": (sum((e - sum(raw_errors) / n) ** 2 for e in raw_errors) / n) ** 0.5,
                    "median_abs_error": abs_errors[n // 2],
                    "p90_abs_error": abs_errors[int(n * 0.9)] if n >= 10 else abs_errors[-1],
                    "max_abs_error": abs_errors[-1],
                    "errors": errors,
                }

        except Exception as e:
            logger.error(f"Error distribution calculation failed: {e}")
            return {"count": 0, "errors": []}

    def get_accuracy_by_lead_time(
        self,
        hours_buckets: Optional[List[int]] = None,
    ) -> Dict[str, Any]:
        """Return forecast accuracy broken down by hours-to-close buckets."""
        if hours_buckets is None:
            hours_buckets = [6, 12, 24, 48]

        try:
            with next(get_db_session()) as session:
                # Get all paired forecast-settlement data
                settlements = session.query(SettlementDB).all()
                if not settlements:
                    return {"buckets": {}}

                bucket_errors: Dict[str, List[float]] = {}
                for bucket in hours_buckets:
                    bucket_errors[f"<{bucket}h"] = []

                bucket_errors[f">={hours_buckets[-1]}h"] = []

                for s in settlements:
                    snaps = session.query(ForecastSnapshotDB).filter(
                        ForecastSnapshotDB.city == s.city,
                        ForecastSnapshotDB.market_date == s.market_date,
                    ).all()

                    for snap in snaps:
                        error = abs(snap.forecast_temp - s.settlement_temp)
                        hours = snap.hours_to_close or 0

                        placed = False
                        for bucket in hours_buckets:
                            if hours < bucket:
                                bucket_errors[f"<{bucket}h"].append(error)
                                placed = True
                                break
                        if not placed:
                            bucket_errors[f">={hours_buckets[-1]}h"].append(error)

                result = {}
                for bucket_name, errs in bucket_errors.items():
                    if errs:
                        result[bucket_name] = {
                            "count": len(errs),
                            "mean_abs_error": sum(errs) / len(errs),
                            "max_error": max(errs),
                        }
                    else:
                        result[bucket_name] = {"count": 0}

                return {"buckets": result}

        except Exception as e:
            logger.error(f"Accuracy by lead time failed: {e}")
            return {"buckets": {}}

    def generate_daily_report(self) -> str:
        """Generate a human-readable daily forecast accuracy report."""
        stats = self.get_error_distribution(days_back=1)
        weekly = self.get_error_distribution(days_back=7)
        lead_time = self.get_accuracy_by_lead_time()

        lines = ["# Forecast Accuracy Report", ""]

        if stats["count"] == 0:
            lines.append("No forecast-settlement pairs available for today.")
            return "\n".join(lines)

        lines.append(f"## Today ({stats['count']} markets)")
        lines.append(f"- Mean error: {stats['mean_error']:+.1f}F")
        lines.append(f"- Mean absolute error: {stats['mean_abs_error']:.1f}F")
        lines.append(f"- Std dev: {stats['std_error']:.1f}F")
        lines.append(f"- Median absolute error: {stats['median_abs_error']:.1f}F")
        lines.append(f"- Max absolute error: {stats['max_abs_error']:.1f}F")
        lines.append("")

        if weekly["count"] > 0:
            lines.append(f"## 7-Day ({weekly['count']} markets)")
            lines.append(f"- Mean absolute error: {weekly['mean_abs_error']:.1f}F")
            lines.append(f"- Std dev: {weekly['std_error']:.1f}F")
            lines.append("")

        if lead_time.get("buckets"):
            lines.append("## Accuracy by Lead Time")
            for bucket, data in lead_time["buckets"].items():
                if data.get("count", 0) > 0:
                    lines.append(
                        f"- {bucket}: MAE={data['mean_abs_error']:.1f}F "
                        f"(n={data['count']})"
                    )
            lines.append("")

        return "\n".join(lines)


# ─── Global instance ────────────────────────────────────────────────────

_audit: Optional[ForecastAudit] = None


def get_forecast_audit() -> ForecastAudit:
    """Get or create the global forecast audit instance."""
    global _audit
    if _audit is None:
        _audit = ForecastAudit()
    return _audit
