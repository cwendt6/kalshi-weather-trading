"""
Health monitoring for the Kalshi trading system.

Checks database connectivity, API status, scheduler health,
and trading activity. Reports issues via alerts.
"""
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional

from sqlalchemy import func, text

from src.data.database import db_manager
from src.data.models import TradeDB, MarketDB, PortfolioSnapshotDB
from src.utils.logging import logger


class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


@dataclass
class HealthIndicator:
    """Single health check result."""

    name: str
    status: HealthStatus
    message: str
    last_checked: datetime = field(default_factory=datetime.utcnow)
    details: Optional[Dict] = None


@dataclass
class SystemHealth:
    """Overall system health report."""

    overall_status: HealthStatus
    indicators: List[HealthIndicator]
    checked_at: datetime = field(default_factory=datetime.utcnow)


def check_database() -> HealthIndicator:
    """Check database connectivity and integrity."""
    try:
        with db_manager.get_session() as session:
            result = session.execute(text("PRAGMA integrity_check")).scalar()
            trade_count = session.query(func.count(TradeDB.id)).scalar()

            if result == "ok":
                return HealthIndicator(
                    name="Database",
                    status=HealthStatus.HEALTHY,
                    message=f"OK - {trade_count} trades stored",
                    details={"integrity": result, "trade_count": trade_count},
                )
            else:
                return HealthIndicator(
                    name="Database",
                    status=HealthStatus.UNHEALTHY,
                    message=f"Integrity check failed: {result}",
                )
    except Exception as e:
        return HealthIndicator(
            name="Database",
            status=HealthStatus.UNHEALTHY,
            message=f"Connection error: {str(e)[:100]}",
        )


def check_api_status() -> HealthIndicator:
    """Check Kalshi API connectivity."""
    try:
        from config.settings import settings

        has_key = bool(settings.kalshi_api_key)
        has_private_key = bool(settings.kalshi_private_key_path) or bool(
            getattr(settings, "kalshi_private_key", None)
        )

        if has_key and has_private_key:
            return HealthIndicator(
                name="Kalshi API",
                status=HealthStatus.HEALTHY,
                message="Credentials configured",
                details={"environment": settings.kalshi_environment},
            )
        elif has_key:
            return HealthIndicator(
                name="Kalshi API",
                status=HealthStatus.DEGRADED,
                message="API key present but private key missing",
            )
        else:
            return HealthIndicator(
                name="Kalshi API",
                status=HealthStatus.UNHEALTHY,
                message="No API credentials configured",
            )
    except Exception as e:
        return HealthIndicator(
            name="Kalshi API",
            status=HealthStatus.DEGRADED,
            message=f"Could not check: {str(e)[:100]}",
        )


def check_last_trade() -> HealthIndicator:
    """Check when the last trade was executed."""
    try:
        with db_manager.get_session() as session:
            last_trade = (
                session.query(TradeDB)
                .order_by(TradeDB.timestamp.desc())
                .first()
            )

            if not last_trade:
                return HealthIndicator(
                    name="Last Trade",
                    status=HealthStatus.DEGRADED,
                    message="No trades found",
                )

            trade_ts = last_trade.timestamp
            if trade_ts.tzinfo is None:
                trade_ts = trade_ts.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - trade_ts
            hours_ago = age.total_seconds() / 3600

            if hours_ago < 1:
                status = HealthStatus.HEALTHY
                msg = f"{int(age.total_seconds() / 60)}m ago - {last_trade.ticker}"
            elif hours_ago < 24:
                status = HealthStatus.HEALTHY
                msg = f"{hours_ago:.1f}h ago - {last_trade.ticker}"
            elif hours_ago < 72:
                status = HealthStatus.DEGRADED
                msg = f"{hours_ago:.0f}h since last trade"
            else:
                status = HealthStatus.UNHEALTHY
                msg = f"{hours_ago / 24:.0f} days since last trade"

            return HealthIndicator(
                name="Last Trade",
                status=status,
                message=msg,
                details={
                    "last_trade_time": last_trade.timestamp.isoformat(),
                    "ticker": last_trade.ticker,
                    "hours_ago": round(hours_ago, 1),
                },
            )
    except Exception as e:
        return HealthIndicator(
            name="Last Trade",
            status=HealthStatus.UNHEALTHY,
            message=f"Error: {str(e)[:100]}",
        )


def check_scheduler() -> HealthIndicator:
    """Check scheduler status from its state file."""
    try:
        status_file = Path("data/scheduler_status.json")
        if not status_file.exists():
            return HealthIndicator(
                name="Scheduler",
                status=HealthStatus.DEGRADED,
                message="No scheduler status file found",
            )

        data = json.loads(status_file.read_text())
        status_val = data.get("status", "unknown")

        if status_val == "running":
            return HealthIndicator(
                name="Scheduler",
                status=HealthStatus.HEALTHY,
                message=f"Running - {data.get('cycles_completed', 0)} cycles",
                details=data,
            )
        elif status_val == "stopped":
            return HealthIndicator(
                name="Scheduler",
                status=HealthStatus.DEGRADED,
                message="Stopped",
                details=data,
            )
        else:
            return HealthIndicator(
                name="Scheduler",
                status=HealthStatus.UNHEALTHY,
                message=f"Status: {status_val}",
                details=data,
            )
    except Exception as e:
        return HealthIndicator(
            name="Scheduler",
            status=HealthStatus.DEGRADED,
            message=f"Could not read status: {str(e)[:100]}",
        )


def check_unresolved_trades() -> HealthIndicator:
    """Check ratio of unresolved to total trades."""
    try:
        with db_manager.get_session() as session:
            total = session.query(func.count(TradeDB.id)).scalar() or 0
            unresolved = (
                session.query(func.count(TradeDB.id))
                .filter(TradeDB.resolved == 0)
                .scalar() or 0
            )

            if total == 0:
                return HealthIndicator(
                    name="Resolution",
                    status=HealthStatus.DEGRADED,
                    message="No trades to resolve",
                )

            pct_unresolved = unresolved / total
            if pct_unresolved < 0.5:
                status = HealthStatus.HEALTHY
            elif pct_unresolved < 0.8:
                status = HealthStatus.DEGRADED
            else:
                status = HealthStatus.DEGRADED

            return HealthIndicator(
                name="Resolution",
                status=status,
                message=f"{unresolved}/{total} unresolved ({pct_unresolved:.0%})",
                details={"total": total, "unresolved": unresolved},
            )
    except Exception as e:
        return HealthIndicator(
            name="Resolution",
            status=HealthStatus.UNHEALTHY,
            message=f"Error: {str(e)[:100]}",
        )


def check_win_rate() -> HealthIndicator:
    """Check rolling 7-day win rate."""
    try:
        with db_manager.get_session() as session:
            cutoff = datetime.now(timezone.utc) - timedelta(days=7)
            recent_trades = session.query(TradeDB).filter(
                TradeDB.resolved == 1,
                TradeDB.resolved_at >= cutoff,
            ).all()

            if not recent_trades:
                return HealthIndicator(
                    name="Win Rate (7d)",
                    status=HealthStatus.DEGRADED,
                    message="No resolved trades in last 7 days",
                )

            wins = sum(1 for t in recent_trades if t.outcome == "win")
            total = len(recent_trades)
            win_rate = wins / total if total > 0 else 0

            if win_rate >= 0.55:
                status = HealthStatus.HEALTHY
            elif win_rate >= 0.45:
                status = HealthStatus.DEGRADED
            else:
                status = HealthStatus.UNHEALTHY

            return HealthIndicator(
                name="Win Rate (7d)",
                status=status,
                message=f"{win_rate:.0%} ({wins}/{total} trades)",
                details={"win_rate": win_rate, "wins": wins, "total": total},
            )
    except Exception as e:
        return HealthIndicator(
            name="Win Rate (7d)",
            status=HealthStatus.DEGRADED,
            message=f"Error: {str(e)[:100]}",
        )


def check_daily_pnl() -> HealthIndicator:
    """Check today's P&L status."""
    try:
        with db_manager.get_session() as session:
            today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            today_trades = session.query(TradeDB).filter(
                TradeDB.resolved == 1,
                TradeDB.resolved_at >= today_start,
            ).all()

            if not today_trades:
                return HealthIndicator(
                    name="Daily P&L",
                    status=HealthStatus.HEALTHY,
                    message="No resolved trades today",
                )

            total_pnl = sum(float(t.pnl or 0) for t in today_trades)

            if total_pnl >= 0:
                status = HealthStatus.HEALTHY
                msg = f"+${total_pnl:.2f} ({len(today_trades)} trades)"
            elif total_pnl > -50:
                status = HealthStatus.DEGRADED
                msg = f"-${abs(total_pnl):.2f} ({len(today_trades)} trades)"
            else:
                status = HealthStatus.UNHEALTHY
                msg = f"-${abs(total_pnl):.2f} ({len(today_trades)} trades) - HIGH LOSS"

            return HealthIndicator(
                name="Daily P&L",
                status=status,
                message=msg,
                details={"pnl": total_pnl, "trade_count": len(today_trades)},
            )
    except Exception as e:
        return HealthIndicator(
            name="Daily P&L",
            status=HealthStatus.DEGRADED,
            message=f"Error: {str(e)[:100]}",
        )


def run_health_check() -> SystemHealth:
    """
    Run all health checks and return system health report.

    Returns:
        SystemHealth with overall status and individual indicators.
    """
    indicators = [
        check_database(),
        check_api_status(),
        check_last_trade(),
        check_scheduler(),
        check_unresolved_trades(),
        check_win_rate(),
        check_daily_pnl(),
    ]

    # Overall status is the worst individual status
    if any(i.status == HealthStatus.UNHEALTHY for i in indicators):
        overall = HealthStatus.UNHEALTHY
    elif any(i.status == HealthStatus.DEGRADED for i in indicators):
        overall = HealthStatus.DEGRADED
    else:
        overall = HealthStatus.HEALTHY

    health = SystemHealth(
        overall_status=overall,
        indicators=indicators,
    )

    logger.info(
        "Health check complete",
        overall=overall.value,
        checks={i.name: i.status.value for i in indicators},
    )

    return health


def format_health_report(health: SystemHealth) -> str:
    """Format health check as a readable string."""
    status_icons = {
        HealthStatus.HEALTHY: "[OK]",
        HealthStatus.DEGRADED: "[WARN]",
        HealthStatus.UNHEALTHY: "[FAIL]",
    }

    lines = [
        f"System Health: {status_icons[health.overall_status]} {health.overall_status.value.upper()}",
        f"Checked: {health.checked_at.strftime('%Y-%m-%d %H:%M UTC')}",
        "",
    ]

    for ind in health.indicators:
        icon = status_icons[ind.status]
        lines.append(f"  {icon} {ind.name}: {ind.message}")

    return "\n".join(lines)
