"""
Alert system for trading notifications.

Supports console output and multi-channel Discord webhook alerts.
Routes alerts to appropriate Discord channels:
  #general       — Daily summaries, bot startup/shutdown
  #trades        — Trade executions (opened/closed), settlements
  #trading-alerts— High-edge opportunities, risk warnings, forecast shifts
  #portfolio     — Portfolio snapshots, phase changes, obs pool status
  #system-health — System errors, health checks, crashes
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Callable, Dict, List, Optional
import json

from src.utils.logging import logger


class AlertType(Enum):
    """Types of alerts."""

    OPPORTUNITY = "opportunity"  # High-edge opportunity found
    TRADE_OPENED = "trade_opened"  # Position opened
    TRADE_CLOSED = "trade_closed"  # Position closed
    RISK_WARNING = "risk_warning"  # Risk limit warning
    RISK_VIOLATION = "risk_violation"  # Risk limit exceeded
    SYSTEM_ERROR = "system_error"  # System error
    SYSTEM_INFO = "system_info"  # System information

    # Phase 5 alert types
    SETTLEMENT = "settlement"          # Market settled, position closed
    OBS_SETTLED = "obs_settled"        # Obs-settled opportunity executed
    FORECAST_SHIFT = "forecast_shift"  # Significant forecast change
    STAGE_EXIT = "stage_exit"          # YES exit stage triggered
    RESHAPE = "reshape"                # Portfolio reshape executed
    POOL_STATUS = "pool_status"        # Obs-settled pool update
    DAILY_SUMMARY = "daily_summary"    # End-of-day P&L summary

    # Portfolio tracking
    PORTFOLIO_SNAPSHOT = "portfolio_snapshot"  # Periodic portfolio update
    PHASE_CHANGE = "phase_change"              # Bankroll phase transition


class AlertPriority(Enum):
    """Alert priority levels."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class DiscordChannel(Enum):
    """Discord channel targets for alert routing."""

    GENERAL = "general"
    TRADES = "trades"
    TRADING_ALERTS = "trading_alerts"
    PORTFOLIO = "portfolio"
    SYSTEM_HEALTH = "system_health"


# ── Channel routing: which alert types go to which Discord channel ──
ALERT_CHANNEL_MAP: Dict[AlertType, DiscordChannel] = {
    # #general — summaries and announcements
    AlertType.DAILY_SUMMARY: DiscordChannel.GENERAL,

    # #trades — all trade activity
    AlertType.TRADE_OPENED: DiscordChannel.TRADES,
    AlertType.TRADE_CLOSED: DiscordChannel.TRADES,
    AlertType.SETTLEMENT: DiscordChannel.TRADES,
    AlertType.OBS_SETTLED: DiscordChannel.TRADES,
    AlertType.STAGE_EXIT: DiscordChannel.TRADES,

    # #trading-alerts — opportunities, risk, forecast changes
    AlertType.OPPORTUNITY: DiscordChannel.TRADING_ALERTS,
    AlertType.RISK_WARNING: DiscordChannel.TRADING_ALERTS,
    AlertType.RISK_VIOLATION: DiscordChannel.TRADING_ALERTS,
    AlertType.FORECAST_SHIFT: DiscordChannel.TRADING_ALERTS,
    AlertType.RESHAPE: DiscordChannel.TRADING_ALERTS,

    # #portfolio — portfolio state
    AlertType.PORTFOLIO_SNAPSHOT: DiscordChannel.PORTFOLIO,
    AlertType.PHASE_CHANGE: DiscordChannel.PORTFOLIO,
    AlertType.POOL_STATUS: DiscordChannel.PORTFOLIO,

    # #system-health — system events
    AlertType.SYSTEM_ERROR: DiscordChannel.SYSTEM_HEALTH,
    AlertType.SYSTEM_INFO: DiscordChannel.SYSTEM_HEALTH,
}


@dataclass
class Alert:
    """An alert notification."""

    alert_type: AlertType
    priority: AlertPriority
    title: str
    message: str
    data: Dict[str, Any]
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Tracking
    alert_id: str = ""
    sent: bool = False
    sent_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert alert to dictionary."""
        return {
            "alert_id": self.alert_id,
            "type": self.alert_type.value,
            "priority": self.priority.value,
            "title": self.title,
            "message": self.message,
            "data": self.data,
            "timestamp": self.timestamp.isoformat(),
        }

    def to_console_string(self) -> str:
        """Format alert for console output."""
        priority_emoji = {
            AlertPriority.LOW: "\u2139\ufe0f",
            AlertPriority.MEDIUM: "\u26a0\ufe0f",
            AlertPriority.HIGH: "\U0001f6a8",
            AlertPriority.CRITICAL: "\U0001f534",
        }
        emoji = priority_emoji.get(self.priority, "\U0001f4e2")
        return f"{emoji} [{self.alert_type.value.upper()}] {self.title}: {self.message}"

    def to_discord_embed(self) -> Dict[str, Any]:
        """Build a rich Discord embed payload for this alert."""
        color_map = {
            AlertPriority.LOW: 0x5865F2,       # Discord blurple
            AlertPriority.MEDIUM: 0xFEE75C,    # Yellow
            AlertPriority.HIGH: 0xED4245,       # Red
            AlertPriority.CRITICAL: 0xFF0000,   # Bright red
        }

        # Build fields from data dict for structured display
        fields: List[Dict[str, Any]] = []
        for key, val in self.data.items():
            if val is not None and key not in ("ticker",):
                display_key = key.replace("_", " ").title()
                if isinstance(val, float):
                    if "pct" in key or "edge" in key:
                        display_val = f"{val:.1%}"
                    else:
                        display_val = f"${val:.2f}" if abs(val) < 10000 else f"{val:,.0f}"
                else:
                    display_val = str(val)
                fields.append({"name": display_key, "value": display_val, "inline": True})

        embed: Dict[str, Any] = {
            "title": self.title,
            "description": self.message,
            "color": color_map.get(self.priority, 0x5865F2),
            "timestamp": self.timestamp.isoformat(),
        }

        if fields:
            embed["fields"] = fields[:25]  # Discord limit

        return {"embeds": [embed]}

    # Keep legacy method for backwards compat
    def to_webhook_payload(self, format: str = "discord") -> Dict[str, Any]:
        """Format alert for webhook delivery."""
        if format == "discord":
            return self.to_discord_embed()
        elif format == "slack":
            return {
                "text": f"*{self.title}*",
                "blocks": [
                    {"type": "header", "text": {"type": "plain_text", "text": self.title}},
                    {"type": "section", "text": {"type": "mrkdwn", "text": self.message}},
                ],
            }
        else:
            return self.to_dict()


class AlertManager:
    """
    Manages alerts and notifications with multi-channel Discord routing.

    Features:
    - Console output alerts
    - Multi-channel Discord webhooks (5 channels)
    - Rich embed formatting per alert type
    - Configurable alert thresholds
    - Rate limiting to prevent spam
    """

    DEFAULT_EDGE_THRESHOLD = 0.10
    DEFAULT_PNL_THRESHOLD = 50.0
    DEFAULT_RATE_LIMIT_SECONDS = 60

    def __init__(
        self,
        edge_threshold: float = 0.10,
        pnl_threshold: float = 50.0,
        rate_limit_seconds: int = 60,
        console_enabled: bool = True,
        webhook_url: Optional[str] = None,
        webhook_format: str = "discord",
        discord_webhooks: Optional[Dict[DiscordChannel, str]] = None,
    ) -> None:
        self.edge_threshold = edge_threshold
        self.pnl_threshold = pnl_threshold
        self.rate_limit_seconds = rate_limit_seconds
        self.console_enabled = console_enabled
        self.webhook_url = webhook_url  # Legacy single-webhook fallback
        self.webhook_format = webhook_format

        # Multi-channel Discord webhooks
        self.discord_webhooks: Dict[DiscordChannel, str] = discord_webhooks or {}

        # Alert history for deduplication/rate limiting
        self._alert_history: List[Alert] = []
        self._last_alert_time: Dict[str, datetime] = {}
        self._alert_counter = 0

        # Custom handlers
        self._handlers: List[Callable[[Alert], None]] = []

        channel_count = len(self.discord_webhooks)
        logger.info(
            "Alert manager initialized",
            edge_threshold=edge_threshold,
            console_enabled=console_enabled,
            discord_channels=channel_count,
            legacy_webhook=webhook_url is not None,
        )

    def _generate_alert_id(self) -> str:
        """Generate unique alert ID."""
        self._alert_counter += 1
        return f"ALT-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{self._alert_counter:04d}"

    def _should_send(self, alert: Alert) -> bool:
        """Check if alert should be sent (rate limiting)."""
        key = f"{alert.alert_type.value}:{alert.title}"
        last_time = self._last_alert_time.get(key)

        if last_time:
            elapsed = (datetime.now(timezone.utc) - last_time).total_seconds()
            if elapsed < self.rate_limit_seconds:
                return False

        return True

    def _send_console(self, alert: Alert) -> None:
        """Send alert to console."""
        if self.console_enabled:
            logger.info(
                f"[DISCORD] {alert.to_console_string()}",
            )

    def _post_discord(self, url: str, payload: Dict[str, Any]) -> bool:
        """Post a payload to a Discord webhook URL.

        Uses httpx (same HTTP stack as Kalshi API client) to avoid
        macOS Python SSL certificate issues with urllib.request.
        """
        try:
            import httpx

            resp = httpx.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=10,
            )

            if resp.status_code in (200, 204):
                return True
            else:
                logger.warning(
                    f"Discord webhook HTTP {resp.status_code}: "
                    f"{resp.text[:200]} | url={url[:60]}..."
                )
                return False

        except Exception as e:
            logger.error(
                f"Failed to send Discord webhook: {type(e).__name__}: "
                f"{str(e)[:200]} | url={url[:60]}..."
            )
            return False

    def _send_webhook(self, alert: Alert) -> bool:
        """Route alert to the correct Discord channel webhook."""
        payload = alert.to_discord_embed()

        # 1. Try multi-channel routing first
        target_channel = ALERT_CHANNEL_MAP.get(alert.alert_type)
        if target_channel and target_channel in self.discord_webhooks:
            url = self.discord_webhooks[target_channel]
            return self._post_discord(url, payload)

        # 2. Fall back to legacy single webhook
        if self.webhook_url:
            if self.webhook_format == "discord":
                return self._post_discord(self.webhook_url, payload)
            else:
                # Slack format fallback
                payload = alert.to_webhook_payload("slack")
                return self._post_discord(self.webhook_url, payload)

        return False

    def _call_handlers(self, alert: Alert) -> None:
        """Call custom alert handlers."""
        for handler in self._handlers:
            try:
                handler(alert)
            except Exception as e:
                logger.error("Alert handler failed", error=str(e))

    def send(self, alert: Alert) -> bool:
        """Send an alert through all configured channels."""
        # Check rate limiting
        if not self._should_send(alert):
            logger.debug("Alert rate limited", alert_type=alert.alert_type.value)
            return False

        # Generate ID
        alert.alert_id = self._generate_alert_id()

        # Send to all channels
        self._send_console(alert)
        self._send_webhook(alert)
        self._call_handlers(alert)

        # Update tracking
        alert.sent = True
        alert.sent_at = datetime.now(timezone.utc)
        self._alert_history.append(alert)
        self._last_alert_time[f"{alert.alert_type.value}:{alert.title}"] = datetime.now(timezone.utc)

        return True

    def add_handler(self, handler: Callable[[Alert], None]) -> None:
        """Add custom alert handler."""
        self._handlers.append(handler)

    # ── Trade alerts (→ #trades) ─────────────────────────────────────

    def alert_trade(
        self,
        ticker: str,
        action: str,
        side: str,
        quantity: int,
        price: int,
        pnl: Optional[float] = None,
        edge: Optional[float] = None,
        trade_type: Optional[str] = None,
        bracket_role: Optional[str] = None,
        city_event: Optional[str] = None,
        budget_spent: Optional[float] = None,
        budget_total: Optional[float] = None,
    ) -> Optional[Alert]:
        """Alert on trade execution with rich context."""
        is_close = action == "sell"
        alert_type = AlertType.TRADE_CLOSED if is_close else AlertType.TRADE_OPENED

        # Build description
        if is_close:
            emoji = "\U0001f4b0" if (pnl and pnl > 0) else "\U0001f4c9"
            pnl_str = f"\nP&L: **${pnl:+.2f}**" if pnl is not None else ""
            desc = (
                f"{emoji} **{action.upper()} {quantity} {side.upper()}** @ {price}\u00a2"
                f"{pnl_str}"
            )
        else:
            emoji = "\U0001f7e2" if side == "yes" else "\U0001f534"
            parts = [f"{emoji} **BUY {quantity} {side.upper()}** @ {price}\u00a2"]
            if edge is not None:
                parts.append(f"Edge: **{edge:.1%}**")
            if trade_type:
                parts.append(f"Type: {trade_type}")
            if bracket_role:
                parts.append(f"Role: {bracket_role}")
            if city_event and budget_spent is not None and budget_total is not None:
                parts.append(f"Budget: ${budget_spent:.2f}/${budget_total:.2f} ({city_event})")
            desc = " | ".join(parts[:2]) + "\n" + " | ".join(parts[2:]) if len(parts) > 2 else " | ".join(parts)

        alert = Alert(
            alert_type=alert_type,
            priority=AlertPriority.LOW,
            title=f"{'Exit' if is_close else 'Entry'}: {ticker}",
            message=desc,
            data={k: v for k, v in {
                "ticker": ticker, "action": action, "side": side,
                "quantity": quantity, "price": price, "pnl": pnl,
                "edge": edge, "trade_type": trade_type,
                "bracket_role": bracket_role, "city_event": city_event,
            }.items() if v is not None},
        )

        if self.send(alert):
            return alert
        return None

    # ── Opportunity alerts (→ #trading-alerts) ───────────────────────

    def alert_opportunity(
        self,
        ticker: str,
        edge: float,
        side: str,
        price: int,
        ev: float,
    ) -> Optional[Alert]:
        """Alert on high-edge trading opportunity."""
        if abs(edge) < self.edge_threshold:
            return None

        alert = Alert(
            alert_type=AlertType.OPPORTUNITY,
            priority=AlertPriority.MEDIUM if abs(edge) < 0.15 else AlertPriority.HIGH,
            title=f"\U0001f3af Opportunity: {ticker}",
            message=f"Edge: **{edge:+.1%}** | {side.upper()} @ {price}\u00a2 | EV: ${ev:.2f}",
            data={"ticker": ticker, "edge": edge, "side": side, "price": price, "ev": ev},
        )

        if self.send(alert):
            return alert
        return None

    # ── Risk alerts (→ #trading-alerts) ──────────────────────────────

    def alert_risk(
        self,
        violation_type: str,
        message: str,
        is_critical: bool = False,
        data: Optional[Dict[str, Any]] = None,
    ) -> Optional[Alert]:
        """Alert on risk event."""
        alert = Alert(
            alert_type=AlertType.RISK_VIOLATION if is_critical else AlertType.RISK_WARNING,
            priority=AlertPriority.CRITICAL if is_critical else AlertPriority.HIGH,
            title=f"\u26a0\ufe0f Risk: {violation_type}",
            message=message,
            data=data or {},
        )

        if self.send(alert):
            return alert
        return None

    # ── System alerts (→ #system-health) ─────────────────────────────

    def alert_system(
        self,
        title: str,
        message: str,
        is_error: bool = False,
        data: Optional[Dict[str, Any]] = None,
    ) -> Optional[Alert]:
        """Alert on system event."""
        emoji = "\u274c" if is_error else "\u2705"
        alert = Alert(
            alert_type=AlertType.SYSTEM_ERROR if is_error else AlertType.SYSTEM_INFO,
            priority=AlertPriority.HIGH if is_error else AlertPriority.LOW,
            title=f"{emoji} {title}",
            message=message,
            data=data or {},
        )

        if self.send(alert):
            return alert
        return None

    # ── Portfolio alerts (→ #portfolio) ──────────────────────────────

    def alert_portfolio_snapshot(
        self,
        cash: float,
        positions: float,
        total: float,
        phase: str,
        weather_exposure: float,
        num_positions: int,
        obs_pool: float = 0.0,
    ) -> Optional[Alert]:
        """Send periodic portfolio snapshot to #portfolio."""
        phase_emoji = {
            "survival": "\U0001f6e1\ufe0f",
            "acceleration": "\U0001f680",
            "scaling": "\U0001f4c8",
        }
        emoji = phase_emoji.get(phase.lower(), "\U0001f4bc")

        alert = Alert(
            alert_type=AlertType.PORTFOLIO_SNAPSHOT,
            priority=AlertPriority.LOW,
            title=f"{emoji} Portfolio Update",
            message=(
                f"**${total:.2f}** total | Phase: **{phase.upper()}**\n"
                f"Cash: ${cash:.2f} | Positions: ${positions:.2f}\n"
                f"Weather exposure: ${weather_exposure:.2f} | "
                f"Active positions: {num_positions}\n"
                f"Obs pool: ${obs_pool:.2f}"
            ),
            data={
                "cash": cash, "positions": positions, "total": total,
                "phase": phase, "weather_exposure": weather_exposure,
                "num_positions": num_positions, "obs_pool": obs_pool,
            },
        )

        if self.send(alert):
            return alert
        return None

    def alert_phase_change(
        self,
        old_phase: str,
        new_phase: str,
        bankroll: float,
    ) -> Optional[Alert]:
        """Alert when bankroll phase changes."""
        if old_phase == new_phase:
            return None

        going_up = new_phase in ("acceleration", "scaling")
        emoji = "\u2b06\ufe0f" if going_up else "\u2b07\ufe0f"

        alert = Alert(
            alert_type=AlertType.PHASE_CHANGE,
            priority=AlertPriority.HIGH,
            title=f"{emoji} Phase Change: {old_phase.upper()} \u2192 {new_phase.upper()}",
            message=(
                f"Bankroll: **${bankroll:.2f}**\n"
                f"Risk limits have been {'increased' if going_up else 'tightened'}."
            ),
            data={"old_phase": old_phase, "new_phase": new_phase, "bankroll": bankroll},
        )

        if self.send(alert):
            return alert
        return None

    # ── Phase 5 convenience methods (→ #trades / #portfolio) ────────

    def alert_settlement(
        self, ticker: str, side: str, result: str, pnl: float,
        strategy: str, quantity: int,
    ) -> Optional[Alert]:
        """Alert on market settlement → #trades."""
        emoji = "\u2705" if pnl > 0 else "\u274c"
        alert = Alert(
            alert_type=AlertType.SETTLEMENT,
            priority=AlertPriority.MEDIUM if abs(pnl) < 5.0 else AlertPriority.HIGH,
            title=f"{emoji} Settlement: {ticker}",
            message=(
                f"Result: **{result.upper()}** | {side.upper()} x{quantity}\n"
                f"P&L: **${pnl:+.2f}** | Strategy: {strategy}"
            ),
            data={"ticker": ticker, "side": side, "result": result,
                  "pnl": pnl, "strategy": strategy},
        )
        return alert if self.send(alert) else None

    def alert_obs_settled(
        self, ticker: str, no_price: int, contracts: int,
        guaranteed_profit: float,
    ) -> Optional[Alert]:
        """Alert on obs-settled trade execution → #trades."""
        alert = Alert(
            alert_type=AlertType.OBS_SETTLED,
            priority=AlertPriority.MEDIUM,
            title=f"\U0001f3af Obs-Settled: {ticker}",
            message=(
                f"Bought {contracts} NO @ {no_price}\u00a2\n"
                f"Guaranteed profit: **${guaranteed_profit:.2f}**"
            ),
            data={"ticker": ticker, "no_price": no_price,
                  "contracts": contracts, "profit": guaranteed_profit},
        )
        return alert if self.send(alert) else None

    def alert_daily_summary(
        self, date_str: str, total_pnl: float, trades: int,
        wins: int, losses: int, settlements: int,
        obs_pool_balance: float,
    ) -> Optional[Alert]:
        """Alert with end-of-day summary → #general."""
        emoji = "\U0001f4c8" if total_pnl >= 0 else "\U0001f4c9"
        alert = Alert(
            alert_type=AlertType.DAILY_SUMMARY,
            priority=AlertPriority.LOW,
            title=f"{emoji} Daily Summary: {date_str}",
            message=(
                f"P&L: **${total_pnl:+.2f}** | Trades: {trades}\n"
                f"Wins: {wins} / Losses: {losses}\n"
                f"Settlements: {settlements}\n"
                f"Obs Pool: ${obs_pool_balance:.2f}"
            ),
            data={"date": date_str, "pnl": total_pnl, "trades": trades,
                  "wins": wins, "losses": losses},
        )
        return alert if self.send(alert) else None

    # ── History ──────────────────────────────────────────────────────

    def get_recent_alerts(
        self,
        alert_type: Optional[AlertType] = None,
        limit: int = 50,
    ) -> List[Alert]:
        """Get recent alerts."""
        alerts = self._alert_history

        if alert_type:
            alerts = [a for a in alerts if a.alert_type == alert_type]

        return sorted(alerts, key=lambda a: a.timestamp, reverse=True)[:limit]

    def clear_history(self) -> None:
        """Clear alert history."""
        self._alert_history = []
        self._last_alert_time = {}


# ── Global instance ──────────────────────────────────────────────────

_alert_manager: Optional[AlertManager] = None


def get_alert_manager() -> AlertManager:
    """Get or create the global alert manager instance.

    Reads webhook configuration from environment:
    Multi-channel Discord:
    - DISCORD_WEBHOOK_GENERAL: #general channel
    - DISCORD_WEBHOOK_TRADES: #trades channel
    - DISCORD_WEBHOOK_TRADING_ALERTS: #trading-alerts channel
    - DISCORD_WEBHOOK_PORTFOLIO: #portfolio channel
    - DISCORD_WEBHOOK_SYSTEM_HEALTH: #system-health channel

    Legacy (fallback):
    - DISCORD_WEBHOOK_URL: Single webhook for all alerts
    - SLACK_WEBHOOK_URL: Slack webhook
    - ALERT_WEBHOOK_FORMAT: "discord" or "slack"
    - ALERT_RATE_LIMIT_SECONDS: Min seconds between same alert type
    """
    global _alert_manager
    if _alert_manager is None:
        import os

        # Multi-channel Discord webhooks
        discord_webhooks: Dict[DiscordChannel, str] = {}
        channel_env_map = {
            DiscordChannel.GENERAL: "DISCORD_WEBHOOK_GENERAL",
            DiscordChannel.TRADES: "DISCORD_WEBHOOK_TRADES",
            DiscordChannel.TRADING_ALERTS: "DISCORD_WEBHOOK_TRADING_ALERTS",
            DiscordChannel.PORTFOLIO: "DISCORD_WEBHOOK_PORTFOLIO",
            DiscordChannel.SYSTEM_HEALTH: "DISCORD_WEBHOOK_SYSTEM_HEALTH",
        }

        for channel, env_var in channel_env_map.items():
            url = os.getenv(env_var, "").strip()
            if url:
                discord_webhooks[channel] = url

        # Legacy single webhook fallback
        discord_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip() or None
        slack_url = os.getenv("SLACK_WEBHOOK_URL", "").strip() or None
        webhook_url = discord_url or slack_url or None
        webhook_format = "discord" if discord_url else "slack"

        fmt_override = os.getenv("ALERT_WEBHOOK_FORMAT")
        if fmt_override in ("discord", "slack"):
            webhook_format = fmt_override

        rate_limit = int(os.getenv("ALERT_RATE_LIMIT_SECONDS", "60"))

        _alert_manager = AlertManager(
            webhook_url=webhook_url,
            webhook_format=webhook_format,
            rate_limit_seconds=rate_limit,
            console_enabled=True,
            discord_webhooks=discord_webhooks,
        )

        if discord_webhooks:
            channels = ", ".join(c.value for c in discord_webhooks)
            logger.info(f"Alert manager: {len(discord_webhooks)} Discord channels configured ({channels})")
        elif webhook_url:
            logger.info(f"Alert manager: {webhook_format} single webhook configured")
        else:
            logger.info("Alert manager: console-only (no webhook URL configured)")

    return _alert_manager


# ── Convenience functions ────────────────────────────────────────────

def alert_opportunity(ticker: str, edge: float, side: str, price: int, ev: float) -> Optional[Alert]:
    """Convenience function to alert on opportunity."""
    return get_alert_manager().alert_opportunity(ticker, edge, side, price, ev)


def alert_trade(
    ticker: str,
    action: str,
    side: str,
    quantity: int,
    price: int,
    pnl: Optional[float] = None,
) -> Optional[Alert]:
    """Convenience function to alert on trade."""
    return get_alert_manager().alert_trade(ticker, action, side, quantity, price, pnl)


def alert_risk(violation_type: str, message: str, is_critical: bool = False) -> Optional[Alert]:
    """Convenience function to alert on risk event."""
    return get_alert_manager().alert_risk(violation_type, message, is_critical)
