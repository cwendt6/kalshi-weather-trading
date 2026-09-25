"""
Calibration Tracker — ML Point 7: Force Calibration (Mandatory)

Tracks forecast accuracy by comparing predicted probabilities to actual outcomes.
When the model says 70%, it should resolve YES ~70% of the time.

Two main functions:
1. backfill_resolutions() — Matches resolved markets to historical forecasts
2. compute_calibration() — Buckets forecasts by probability and checks actual rates

Also computes Brier score (primary metric) and log loss.
"""
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from src.data.database import get_db_session
from src.data.models import CalibrationSummaryDB, ForecastDB, MarketDB
from src.utils.logging import logger


def backfill_resolutions() -> int:
    """
    Match resolved markets to their historical forecasts.

    Scans MarketDB for markets with result='yes' or result='no',
    then fills in ForecastDB.resolution and ForecastDB.brier_score
    for all matching forecasts that are still NULL.

    Returns: Number of forecasts updated.
    """
    updated = 0

    with next(get_db_session()) as session:
        # Get all resolved markets
        resolved_markets = (
            session.query(MarketDB)
            .filter(MarketDB.result.in_(["yes", "no"]))
            .all()
        )

        for market in resolved_markets:
            ticker = market.ticker
            resolution = 1 if market.result == "yes" else 0

            # Find unresolved forecasts for this ticker
            unresolved = (
                session.query(ForecastDB)
                .filter(
                    ForecastDB.ticker == ticker,
                    ForecastDB.resolution.is_(None),
                )
                .all()
            )

            for forecast in unresolved:
                forecast.resolution = resolution
                forecast.resolved_at = datetime.now(timezone.utc)

                # Compute Brier score: (predicted - actual)^2
                prob = forecast.probability or 0.5
                forecast.brier_score = (prob - resolution) ** 2

                # Ensure calibration bucket is set
                if forecast.calibration_bucket is None:
                    bucket = round(prob * 20) / 20.0
                    forecast.calibration_bucket = max(0.05, min(0.95, bucket))

                updated += 1

        if updated > 0:
            logger.info(f"Backfilled {updated} forecast resolutions from {len(resolved_markets)} resolved markets")

    return updated


def compute_calibration(
    method: Optional[str] = None,
    domain: Optional[str] = None,
) -> Dict:
    """
    Compute calibration curve for resolved forecasts.

    Buckets forecasts by predicted probability (0.05 to 0.95 in 0.10 steps),
    then computes the actual resolution rate in each bucket.

    Perfect calibration: predicted 70% bucket resolves YES 70% of the time.

    Args:
        method: Filter by forecast method (e.g., 'prob_engine_weather'), or None for all
        domain: Filter by domain ('weather', 'crypto', etc.), or None for all

    Returns:
        Dict with calibration stats, Brier score, and per-bucket breakdowns.
    """
    with next(get_db_session()) as session:
        query = session.query(ForecastDB).filter(
            ForecastDB.resolution.isnot(None),
        )

        if method:
            query = query.filter(ForecastDB.method.like(f"%{method}%"))
        if domain:
            query = query.filter(ForecastDB.domain == domain)

        forecasts = query.all()

    if not forecasts:
        return {
            "total_forecasts": 0,
            "total_resolved": 0,
            "brier_score": None,
            "log_loss": None,
            "buckets": {},
            "calibration_error": None,
        }

    # Bucket forecasts by predicted probability
    buckets = defaultdict(lambda: {"predictions": [], "outcomes": []})

    total_brier = 0.0
    total_log_loss = 0.0
    count = 0

    for f in forecasts:
        prob = f.probability or 0.5
        outcome = f.resolution  # 0 or 1

        # Bucket: round to nearest 0.10
        bucket_key = round(prob * 10) / 10.0
        bucket_key = max(0.05, min(0.95, bucket_key))

        buckets[bucket_key]["predictions"].append(prob)
        buckets[bucket_key]["outcomes"].append(outcome)

        # Brier score: (p - y)^2
        brier = (prob - outcome) ** 2
        total_brier += brier

        # Log loss: -(y*log(p) + (1-y)*log(1-p))
        p_clamp = max(1e-6, min(1 - 1e-6, prob))
        ll = -(outcome * math.log(p_clamp) + (1 - outcome) * math.log(1 - p_clamp))
        total_log_loss += ll

        count += 1

    # Compute per-bucket calibration
    bucket_results = {}
    total_cal_error = 0.0
    cal_buckets = 0

    for bucket_key in sorted(buckets.keys()):
        b = buckets[bucket_key]
        n = len(b["outcomes"])
        actual_rate = sum(b["outcomes"]) / n if n > 0 else 0
        mean_pred = sum(b["predictions"]) / n if n > 0 else bucket_key
        bucket_brier = sum((p - y) ** 2 for p, y in zip(b["predictions"], b["outcomes"])) / n

        bucket_results[bucket_key] = {
            "count": n,
            "predicted_mean": round(mean_pred, 3),
            "actual_rate": round(actual_rate, 3),
            "brier_score": round(bucket_brier, 4),
            "calibration_gap": round(actual_rate - mean_pred, 3),
        }

        total_cal_error += abs(actual_rate - mean_pred) * n
        cal_buckets += n

    overall_brier = total_brier / count if count > 0 else None
    overall_log_loss = total_log_loss / count if count > 0 else None
    mean_cal_error = total_cal_error / cal_buckets if cal_buckets > 0 else None

    return {
        "total_forecasts": len(forecasts),
        "total_resolved": count,
        "brier_score": round(overall_brier, 4) if overall_brier is not None else None,
        "log_loss": round(overall_log_loss, 4) if overall_log_loss is not None else None,
        "calibration_error": round(mean_cal_error, 4) if mean_cal_error is not None else None,
        "buckets": bucket_results,
        "method_filter": method,
        "domain_filter": domain,
    }


def save_calibration_snapshot(
    method: Optional[str] = None,
    domain: Optional[str] = None,
) -> None:
    """
    Compute calibration and save to CalibrationSummaryDB.
    Called periodically (e.g., once per hour or once per day).
    """
    stats = compute_calibration(method=method, domain=domain)

    if stats["total_resolved"] == 0:
        return

    now = datetime.now(timezone.utc)

    with next(get_db_session()) as session:
        # Save summary row (bucket=0)
        summary = CalibrationSummaryDB(
            computed_at=now,
            method=method or "all",
            domain=domain,
            bucket=0.0,
            bucket_count=0,
            actual_resolution_rate=None,
            predicted_mean=None,
            brier_score=None,
            total_forecasts=stats["total_forecasts"],
            total_resolved=stats["total_resolved"],
            overall_brier_score=stats["brier_score"],
            overall_log_loss=stats["log_loss"],
        )
        session.add(summary)

        # Save per-bucket rows
        for bucket_key, bdata in stats["buckets"].items():
            row = CalibrationSummaryDB(
                computed_at=now,
                method=method or "all",
                domain=domain,
                bucket=bucket_key,
                bucket_count=bdata["count"],
                actual_resolution_rate=bdata["actual_rate"],
                predicted_mean=bdata["predicted_mean"],
                brier_score=bdata["brier_score"],
            )
            session.add(row)

        session.commit()

    logger.info(
        "Calibration snapshot saved",
        method=method or "all",
        domain=domain,
        total_resolved=stats["total_resolved"],
        brier_score=stats["brier_score"],
        cal_error=stats["calibration_error"],
    )


def print_calibration_report(
    method: Optional[str] = None,
    domain: Optional[str] = None,
) -> str:
    """
    Generate a human-readable calibration report.
    """
    stats = compute_calibration(method=method, domain=domain)

    lines = []
    filter_str = f"method={method or 'all'}, domain={domain or 'all'}"
    lines.append(f"=== CALIBRATION REPORT ({filter_str}) ===")
    lines.append(f"Total forecasts: {stats['total_forecasts']}")
    lines.append(f"Resolved: {stats['total_resolved']}")

    if stats["brier_score"] is not None:
        lines.append(f"Brier score: {stats['brier_score']:.4f} (lower is better, 0 = perfect)")
        lines.append(f"Log loss: {stats['log_loss']:.4f}")
        lines.append(f"Mean calibration error: {stats['calibration_error']:.4f}")
        lines.append("")
        lines.append(f"{'Bucket':>8} | {'Count':>6} | {'Predicted':>10} | {'Actual':>8} | {'Gap':>8}")
        lines.append("-" * 55)

        for bucket_key in sorted(stats["buckets"].keys()):
            b = stats["buckets"][bucket_key]
            gap = b["calibration_gap"]
            gap_str = f"{gap:+.1%}"
            lines.append(
                f"{bucket_key:>7.0%} | {b['count']:>6} | {b['predicted_mean']:>9.1%} | "
                f"{b['actual_rate']:>7.1%} | {gap_str:>8}"
            )
    else:
        lines.append("No resolved forecasts yet.")

    report = "\n".join(lines)
    return report
