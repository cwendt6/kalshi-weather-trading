"""
System health monitor with automated alerting.

Aggregates health checks, tracks system metrics over time, and triggers
alerts when thresholds are breached. Integrates with the AlertManager
for multi-channel notifications.

Monitors:
- Trading activity (trades/hour, consecutive losses)
- P&L trends (daily drawdown, losing streaks)
- Data freshness (stale prices, missing collections)
- System resources (DB size, error rates)
- Strategy health (per-strategy win rates)
"""
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import func

from src.data.database import db_manager
from src.data.models import MarketDB, PriceDB, TradeDB, SignalExecutionDB
from src.monitoring.health_check import (
    HealthStatus,
    SystemHealth,
    run_health_check,
    format_health_report,
)
from src.utils.alerts import AlertManager, AlertPriority, AlertType, Alert, get_alert_manager
from src.utils.logging import logger


@dataclass
class AlertRule:
    """A monitoring rule that triggers alerts."""
    name: str
    check_fn: str  # Method name on SystemMonitor
    threshold: float
    priority: AlertPriority
    cooldown_minutes: int = 30  # Minimum time between alerts for this rule
    enabled: bool = True


@dataclass
class MonitoringEvent:
    """A recorded monitoring event."""
    timestamp: datetime
    event_type: str  # 'alert', 'health_check', 'metric'
    severity: str  # 'info', 'warning', 'error', 'critical'
    source: str
    message: str
    data: Dict[str, Any] = field(default_factory=dict)


class SystemMonitor:
    """
    Centralized system health monitor with automated alerting.

    Runs periodic checks and triggers alerts when conditions are met.
    Persists alert history to disk for dashboard display.
    """

    ALERT_LOG_PATH = Path("data/alerts.json")
    MONITOR_STATE_PATH = Path("data/monitor_state.json")

    # Default alert rules
    DEFAULT_RULES = [
        AlertRule("consecutive_losses", "check_consecutive_losses", 5, AlertPriority.HIGH, 60),
        AlertRule("daily_drawdown", "check_daily_drawdown", -50.0, AlertPriority.CRITICAL, 30),
        AlertRule("stale_prices", "check_stale_prices", 3600, AlertPriority.MEDIUM, 120),
        AlertRule("low_win_rate", "check_low_win_rate", 0.40, AlertPriority.HIGH, 360),
        AlertRule("high_error_rate", "check_high_error_rate", 5, AlertPriority.HIGH, 60),
        AlertRule("no_trades", "check_no_trades", 3600, AlertPriority.MEDIUM, 120),
    ]

    def __init__(
        self,
        alert_manager: Optional[AlertManager] = None,
        rules: Optional[List[AlertRule]] = None,
    ):
        self.alert_manager = alert_manager or get_alert_manager()
        self.rules = rules or self.DEFAULT_RULES
        self._events: List[MonitoringEvent] = []
        self._last_alert_times: Dict[str, datetime] = {}
        self._error_counts: Dict[str, int] = {}

        # Load persisted state
        self._load_state()

        logger.info("SystemMonitor initialized", rules=len(self.rules))

    def _load_state(self) -> None:
        """Load persisted monitoring state."""
        try:
            if self.MONITOR_STATE_PATH.exists():
                data = json.loads(self.MONITOR_STATE_PATH.read_text())
                for name, ts_str in data.get("last_alert_times", {}).items():
                    ts = datetime.fromisoformat(ts_str)
                    # Ensure timezone-aware (fromisoformat may produce naive datetime)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    self._last_alert_times[name] = ts
        except Exception:
            pass

    def _save_state(self) -> None:
        """Persist monitoring state to disk."""
        try:
            self.MONITOR_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "last_alert_times": {
                    name: ts.isoformat()
                    for name, ts in self._last_alert_times.items()
                },
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            self.MONITOR_STATE_PATH.write_text(json.dumps(data, indent=2))
        except Exception as e:
            logger.warning(f"Failed to save monitor state: {e}")

    def _should_alert(self, rule_name: str, cooldown_minutes: int) -> bool:
        """Check if enough time has passed since last alert for this rule."""
        last_time = self._last_alert_times.get(rule_name)
        if not last_time:
            return True
        elapsed = (datetime.now(timezone.utc) - last_time).total_seconds() / 60
        return elapsed >= cooldown_minutes

    def _record_event(
        self,
        event_type: str,
        severity: str,
        source: str,
        message: str,
        data: Optional[Dict] = None,
    ) -> MonitoringEvent:
        """Record a monitoring event and persist to log."""
        event = MonitoringEvent(
            timestamp=datetime.now(timezone.utc),
            event_type=event_type,
            severity=severity,
            source=source,
            message=message,
            data=data or {},
        )
        self._events.append(event)

        # Keep only last 500 events in memory
        if len(self._events) > 500:
            self._events = self._events[-500:]

        # Append to alert log file
        self._append_to_log(event)

        return event

    def _append_to_log(self, event: MonitoringEvent) -> None:
        """Append event to persistent JSON log."""
        try:
            self.ALERT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

            entries: list = []
            if self.ALERT_LOG_PATH.exists():
                try:
                    entries = json.loads(self.ALERT_LOG_PATH.read_text())
                except (json.JSONDecodeError, ValueError):
                    entries = []

            entries.append({
                "timestamp": event.timestamp.isoformat(),
                "event_type": event.event_type,
                "severity": event.severity,
                "source": event.source,
                "message": event.message,
                "data": event.data,
            })

            # Keep last 1000 entries on disk
            if len(entries) > 1000:
                entries = entries[-1000:]

            self.ALERT_LOG_PATH.write_text(json.dumps(entries, indent=2))
        except Exception as e:
            logger.warning(f"Failed to write alert log: {e}")

    # ── Health Check Methods ──────────────────────────────────────────

    def check_consecutive_losses(self) -> Optional[Dict[str, Any]]:
        """Check for consecutive losing trades."""
        try:
            with db_manager.get_session() as session:
                recent_trades = (
                    session.query(TradeDB)
                    .filter(TradeDB.resolved == 1)
                    .order_by(TradeDB.resolved_at.desc())
                    .limit(20)
                    .all()
                )

                if not recent_trades:
                    return None

                consecutive_losses = 0
                for trade in recent_trades:
                    if trade.outcome == "loss":
                        consecutive_losses += 1
                    else:
                        break

                if consecutive_losses >= 5:
                    return {
                        "consecutive_losses": consecutive_losses,
                        "last_trade": recent_trades[0].ticker if recent_trades else "",
                    }
        except Exception as e:
            logger.warning(f"Consecutive losses check failed: {e}")
        return None

    def check_daily_drawdown(self) -> Optional[Dict[str, Any]]:
        """Check today's P&L drawdown."""
        try:
            with db_manager.get_session() as session:
                today_start = datetime.now(timezone.utc).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                today_trades = (
                    session.query(TradeDB)
                    .filter(
                        TradeDB.resolved == 1,
                        TradeDB.resolved_at >= today_start,
                    )
                    .all()
                )

                daily_pnl = sum(float(t.pnl or 0) for t in today_trades)
                if daily_pnl < -50.0:
                    return {
                        "daily_pnl": round(daily_pnl, 2),
                        "trade_count": len(today_trades),
                    }
        except Exception as e:
            logger.warning(f"Daily drawdown check failed: {e}")
        return None

    def check_stale_prices(self) -> Optional[Dict[str, Any]]:
        """Check if price data is stale (no updates in >1 hour)."""
        try:
            with db_manager.get_session() as session:
                latest_price = (
                    session.query(func.max(PriceDB.timestamp)).scalar()
                )
                if latest_price:
                    if latest_price.tzinfo is None:
                        latest_price = latest_price.replace(tzinfo=timezone.utc)
                    age_seconds = (datetime.now(timezone.utc) - latest_price).total_seconds()
                    if age_seconds > 3600:
                        return {
                            "last_price_age_hours": round(age_seconds / 3600, 1),
                            "last_price_time": latest_price.isoformat(),
                        }
        except Exception as e:
            logger.warning(f"Stale prices check failed: {e}")
        return None

    def check_low_win_rate(self) -> Optional[Dict[str, Any]]:
        """Check 7-day rolling win rate."""
        try:
            with db_manager.get_session() as session:
                cutoff = datetime.now(timezone.utc) - timedelta(days=7)
                resolved = (
                    session.query(TradeDB)
                    .filter(
                        TradeDB.resolved == 1,
                        TradeDB.resolved_at >= cutoff,
                    )
                    .all()
                )

                if len(resolved) < 10:
                    return None  # Not enough data

                wins = sum(1 for t in resolved if t.outcome == "win")
                win_rate = wins / len(resolved)
                if win_rate < 0.40:
                    return {
                        "win_rate": round(win_rate, 3),
                        "wins": wins,
                        "total": len(resolved),
                        "period_days": 7,
                    }
        except Exception as e:
            logger.warning(f"Win rate check failed: {e}")
        return None

    def check_high_error_rate(self) -> Optional[Dict[str, Any]]:
        """Check if error count threshold is breached."""
        total_errors = sum(self._error_counts.values())
        if total_errors >= 5:
            result = {
                "total_errors": total_errors,
                "error_breakdown": dict(self._error_counts),
            }
            self._error_counts.clear()
            return result
        return None

    def check_no_trades(self) -> Optional[Dict[str, Any]]:
        """Check if no trades have been executed recently."""
        try:
            with db_manager.get_session() as session:
                latest_trade = (
                    session.query(func.max(TradeDB.timestamp)).scalar()
                )
                if latest_trade:
                    if latest_trade.tzinfo is None:
                        latest_trade = latest_trade.replace(tzinfo=timezone.utc)
                    age_seconds = (datetime.now(timezone.utc) - latest_trade).total_seconds()
                    if age_seconds > 3600:
                        return {
                            "hours_since_trade": round(age_seconds / 3600, 1),
                            "last_trade_time": latest_trade.isoformat(),
                        }
        except Exception as e:
            logger.warning(f"Trade activity check failed: {e}")
        return None

    # ── Main Monitor Loop ─────────────────────────────────────────────

    def record_error(self, source: str, error: str) -> None:
        """Record an error for error rate tracking."""
        self._error_counts[source] = self._error_counts.get(source, 0) + 1
        self._record_event("error", "error", source, error)

    def run_monitoring_cycle(self) -> SystemHealth:
        """
        Run a complete monitoring cycle.

        1. Run all health checks
        2. Evaluate alert rules
        3. Send alerts for triggered rules
        4. Return system health report

        Returns:
            SystemHealth report with all indicators.
        """
        # Run standard health checks
        health = run_health_check()

        self._record_event(
            "health_check",
            "info",
            "system_monitor",
            f"Health check: {health.overall_status.value}",
            {"status": health.overall_status.value},
        )

        # Evaluate each alert rule
        for rule in self.rules:
            if not rule.enabled:
                continue

            if not self._should_alert(rule.name, rule.cooldown_minutes):
                continue

            try:
                check_method = getattr(self, rule.check_fn, None)
                if not check_method:
                    continue

                result = check_method()
                if result is not None:
                    # Rule triggered — send alert
                    self._trigger_alert(rule, result)
            except Exception as e:
                logger.warning(f"Alert rule {rule.name} failed: {e}")

        # Alert on UNHEALTHY overall status
        if health.overall_status == HealthStatus.UNHEALTHY:
            unhealthy = [
                i for i in health.indicators
                if i.status == HealthStatus.UNHEALTHY
            ]
            if self._should_alert("system_unhealthy", 30):
                self._trigger_alert(
                    AlertRule(
                        "system_unhealthy", "", 0,
                        AlertPriority.CRITICAL, 30,
                    ),
                    {
                        "unhealthy_checks": [
                            f"{i.name}: {i.message}" for i in unhealthy
                        ],
                    },
                )

        self._save_state()
        return health

    def _trigger_alert(self, rule: AlertRule, data: Dict[str, Any]) -> None:
        """Send an alert and record it."""
        severity = "critical" if rule.priority == AlertPriority.CRITICAL else "warning"
        message = f"[{rule.name}] {json.dumps(data, default=str)}"

        self._record_event("alert", severity, rule.name, message, data)
        self._last_alert_times[rule.name] = datetime.now(timezone.utc)

        # Send via AlertManager
        alert = Alert(
            alert_type=AlertType.RISK_WARNING if severity == "warning" else AlertType.RISK_VIOLATION,
            priority=rule.priority,
            title=f"Monitor Alert: {rule.name}",
            message=message,
            data=data,
        )
        self.alert_manager.send(alert)

        logger.warning(
            f"Monitor alert triggered: {rule.name}",
            severity=severity,
            data=data,
        )

    # ── Query Methods ─────────────────────────────────────────────────

    def get_recent_events(
        self,
        limit: int = 50,
        severity: Optional[str] = None,
    ) -> List[MonitoringEvent]:
        """Get recent monitoring events."""
        events = self._events
        if severity:
            events = [e for e in events if e.severity == severity]
        return sorted(events, key=lambda e: e.timestamp, reverse=True)[:limit]

    def get_alert_log(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Load alert log from disk."""
        try:
            if not self.ALERT_LOG_PATH.exists():
                return []
            entries = json.loads(self.ALERT_LOG_PATH.read_text())
            # Return newest first
            return list(reversed(entries[-limit:]))
        except Exception:
            return []

    def get_strategy_health(self) -> Dict[str, Dict[str, Any]]:
        """Get per-strategy health metrics."""
        strategies: Dict[str, Dict[str, Any]] = {}
        try:
            with db_manager.get_session() as session:
                cutoff = datetime.now(timezone.utc) - timedelta(days=7)
                trades = (
                    session.query(TradeDB)
                    .filter(
                        TradeDB.resolved == 1,
                        TradeDB.resolved_at >= cutoff,
                    )
                    .all()
                )

                for trade in trades:
                    strategy = trade.strategy or "unknown"
                    if strategy not in strategies:
                        strategies[strategy] = {
                            "trades": 0, "wins": 0, "losses": 0,
                            "pnl": 0.0, "win_rate": 0.0,
                        }
                    s = strategies[strategy]
                    s["trades"] += 1
                    if trade.outcome == "win":
                        s["wins"] += 1
                    else:
                        s["losses"] += 1
                    s["pnl"] += float(trade.pnl or 0)

                for s in strategies.values():
                    if s["trades"] > 0:
                        s["win_rate"] = round(s["wins"] / s["trades"], 3)
                    s["pnl"] = round(s["pnl"], 2)

        except Exception as e:
            logger.warning(f"Strategy health check failed: {e}")

        return strategies

    def get_monitoring_summary(self) -> Dict[str, Any]:
        """Get a summary of current monitoring state."""
        recent_alerts = [
            e for e in self._events
            if e.event_type == "alert"
            and (datetime.now(timezone.utc) - e.timestamp).total_seconds() < 86400
        ]
        recent_errors = [
            e for e in self._events
            if e.severity == "error"
            and (datetime.now(timezone.utc) - e.timestamp).total_seconds() < 3600
        ]

        return {
            "alerts_24h": len(recent_alerts),
            "errors_1h": len(recent_errors),
            "active_rules": sum(1 for r in self.rules if r.enabled),
            "strategy_health": self.get_strategy_health(),
        }


# Module-level singleton
_system_monitor: Optional[SystemMonitor] = None


def get_system_monitor() -> SystemMonitor:
    """Get or create the global system monitor instance."""
    global _system_monitor
    if _system_monitor is None:
        _system_monitor = SystemMonitor()
    return _system_monitor
