"""
Edge realization tracking — compares predicted edge at entry to actual settlement outcome.

This is the most important diagnostic for knowing whether the model actually has edge.
Tracks predicted_edge vs actual_return for every resolved trade, computes leakage,
and provides summary statistics for health monitoring.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from src.data.database import get_db_session
from src.data.models import EdgeRealizationDB, TradeDB
from src.utils.fees import KALSHI_WINNER_FEE_RATE
from src.utils.logging import logger


@dataclass
class EdgeRealizationSummary:
    """Summary of edge realization over a time window."""

    avg_predicted_edge: float
    avg_actual_return: float
    avg_edge_leakage: float
    trade_count: int
    win_count: int
    loss_count: int
    by_city: Dict[str, "CityEdgeSummary"]


@dataclass
class CityEdgeSummary:
    """Per-city edge realization breakdown."""

    city: str
    avg_predicted_edge: float
    avg_actual_return: float
    avg_edge_leakage: float
    trade_count: int


def _calculate_actual_return(
    side: str,
    price_cents: int,
    outcome: str,
) -> float:
    """
    Calculate actual return percentage for a resolved trade.

    Args:
        side: "yes" or "no"
        price_cents: Entry price in cents (1-99)
        outcome: "win" or "loss"

    Returns:
        Actual return as a decimal (e.g., 0.15 = 15% return).
    """
    if price_cents <= 0 or price_cents >= 100:
        return 0.0

    if outcome == "win":
        # Gross return = (payout - cost) / cost
        # Payout is always 100 cents per contract
        gross_return = (100 - price_cents) / price_cents
        # Subtract 2% winner fee (on $1.00 payout = $0.02 per contract)
        # Fee as fraction of cost: 0.02 / (price_cents / 100) = 2.0 / price_cents
        fee_impact = KALSHI_WINNER_FEE_RATE / (price_cents / 100.0)
        return gross_return - fee_impact
    else:
        # Loss: lost entire entry cost
        return -1.0


def _extract_city_from_ticker(ticker: str) -> Optional[str]:
    """
    Extract city identifier from a weather market ticker.

    Examples:
        KXHIGHNY-26FEB10-T39 -> NY
        KXLOWTNYC-26FEB10-B34.5 -> NYC
        KXHIGHATL-26FEB10-T55 -> ATL
    """
    # Weather tickers start with KX followed by HIGH/LOW then city code
    if not ticker.startswith("KX"):
        return None

    # Strip KX prefix
    rest = ticker[2:]

    # Remove HIGH/HIGHT/LOW/LOWT prefix
    for prefix in ("HIGHT", "HIGH", "LOWT", "LOW"):
        if rest.startswith(prefix):
            rest = rest[len(prefix):]
            break
    else:
        return None

    # City code is everything before the first dash
    dash_idx = rest.find("-")
    if dash_idx > 0:
        return rest[:dash_idx]
    return None


def record_edge_realizations() -> int:
    """
    Populate edge_realizations table from newly resolved trades.

    Scans TradeDB for resolved trades that have an edge value stored
    and haven't been recorded in edge_realizations yet.

    Returns:
        Number of new edge realization records created.
    """
    created = 0

    try:
        with next(get_db_session()) as session:
            # Get all resolved buy trades with edge data
            resolved_trades = (
                session.query(TradeDB)
                .filter(
                    TradeDB.resolved == 1,
                    TradeDB.edge.isnot(None),
                    TradeDB.action == "buy",
                    TradeDB.outcome.isnot(None),
                )
                .all()
            )

            if not resolved_trades:
                return 0

            # Get existing trade_ids to avoid duplicates
            existing_ids = set()
            existing = session.query(EdgeRealizationDB.trade_id).all()
            for (tid,) in existing:
                if tid:
                    existing_ids.add(tid)

            for trade in resolved_trades:
                if trade.order_id in existing_ids:
                    continue

                actual_return = _calculate_actual_return(
                    side=trade.side,
                    price_cents=trade.price,
                    outcome=trade.outcome,
                )

                predicted_edge = float(trade.edge)
                edge_leakage = predicted_edge - actual_return
                city = _extract_city_from_ticker(trade.ticker)

                realization = EdgeRealizationDB(
                    ticker=trade.ticker,
                    trade_id=trade.order_id,
                    entry_date=trade.timestamp,
                    settlement_date=trade.resolved_at,
                    predicted_edge=predicted_edge,
                    entry_price=trade.price,
                    side=trade.side,
                    settlement_outcome=1 if trade.outcome == "win" else 0,
                    actual_return_pct=round(actual_return, 6),
                    edge_leakage=round(edge_leakage, 6),
                    city=city,
                )
                session.add(realization)
                created += 1

            if created > 0:
                session.commit()
                logger.info(f"Edge realizations recorded: {created} new entries")

    except Exception as e:
        logger.warning(f"Failed to record edge realizations: {e}")

    return created


def get_edge_realization_summary(days: int = 14) -> Optional[EdgeRealizationSummary]:
    """
    Get edge realization summary over a time window.

    Args:
        days: Number of days to look back.

    Returns:
        EdgeRealizationSummary or None if insufficient data.
    """
    try:
        with next(get_db_session()) as session:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)

            realizations = (
                session.query(EdgeRealizationDB)
                .filter(EdgeRealizationDB.settlement_date >= cutoff)
                .all()
            )

            if not realizations:
                return None

            # Extract data within session to avoid DetachedInstanceError
            records = []
            for r in realizations:
                records.append({
                    "predicted_edge": float(r.predicted_edge or 0),
                    "actual_return": float(r.actual_return_pct or 0),
                    "edge_leakage": float(r.edge_leakage or 0),
                    "outcome": r.settlement_outcome,
                    "city": r.city,
                })

        if not records:
            return None

        # Overall stats
        edges = [r["predicted_edge"] for r in records]
        returns = [r["actual_return"] for r in records]
        leakages = [r["edge_leakage"] for r in records]
        wins = sum(1 for r in records if r["outcome"] == 1)
        losses = sum(1 for r in records if r["outcome"] == 0)

        # Per-city breakdown
        city_data: Dict[str, list] = {}
        for r in records:
            city = r["city"] or "unknown"
            if city not in city_data:
                city_data[city] = []
            city_data[city].append(r)

        by_city = {}
        for city, city_records in city_data.items():
            c_edges = [r["predicted_edge"] for r in city_records]
            c_returns = [r["actual_return"] for r in city_records]
            c_leakages = [r["edge_leakage"] for r in city_records]
            by_city[city] = CityEdgeSummary(
                city=city,
                avg_predicted_edge=sum(c_edges) / len(c_edges),
                avg_actual_return=sum(c_returns) / len(c_returns),
                avg_edge_leakage=sum(c_leakages) / len(c_leakages),
                trade_count=len(city_records),
            )

        return EdgeRealizationSummary(
            avg_predicted_edge=sum(edges) / len(edges),
            avg_actual_return=sum(returns) / len(returns),
            avg_edge_leakage=sum(leakages) / len(leakages),
            trade_count=len(records),
            win_count=wins,
            loss_count=losses,
            by_city=by_city,
        )

    except Exception as e:
        logger.warning(f"Failed to get edge realization summary: {e}")
        return None


def format_edge_realization_log(summary: EdgeRealizationSummary, days: int = 14) -> str:
    """Format edge realization summary for logging."""
    return (
        f"EDGE REALIZATION ({days}d): "
        f"predicted={summary.avg_predicted_edge:.1%}, "
        f"realized={summary.avg_actual_return:.1%}, "
        f"leakage={summary.avg_edge_leakage:.1%} "
        f"({summary.trade_count} trades, "
        f"{summary.win_count}W/{summary.loss_count}L)"
    )
