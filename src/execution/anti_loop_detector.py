"""
Anti-loop detector: prevents buy/sell cycling on the same ticker.

Problem: The system was getting into loops where it would buy a position
(e.g., Seattle snow monthly), sell it at a loss, then immediately re-buy
the same ticker because the signal was still strong. This burns cash on
fees and creates unprofitable churn.

Solution: Track exits per ticker with cooldown periods. After selling a
position, enforce a cooldown before re-entering. Blacklist tickers that
have had multiple consecutive losses.
"""

import os
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

from src.utils.logging import logger


class AntiLoopDetector:
    """
    Prevents buy/sell cycling on the same ticker within short timeframes.

    Usage:
        detector = AntiLoopDetector(cooldown_minutes=60, max_consecutive_losses=3)

        # Before entering a trade:
        can_enter, reason = detector.can_re_enter("KXHIGHNY-26FEB12-B36.5")
        if not can_enter:
            logger.info(f"Skipping {ticker}: {reason}")

        # After exiting a position:
        detector.record_exit("KXHIGHNY-26FEB12-B36.5", "yes", net_pnl=-2.50)
    """

    def __init__(
        self,
        cooldown_minutes: Optional[int] = None,
        max_consecutive_losses: Optional[int] = None,
    ):
        self.cooldown_minutes = cooldown_minutes or int(
            os.environ.get("WEATHER_ANTI_LOOP_COOLDOWN_MIN", "60")
        )
        self.max_consecutive_losses = max_consecutive_losses or int(
            os.environ.get("WEATHER_ANTI_LOOP_MAX_LOSSES", "3")
        )

        # ticker -> last exit timestamp
        self._exit_timestamps: Dict[str, datetime] = {}

        # ticker -> consecutive loss count (resets on profitable exit)
        self._loss_counts: Dict[str, int] = {}

        # ticker -> total buy+sell count this session (for churn detection)
        self._session_trade_counts: Dict[str, int] = {}

        # ticker -> cumulative PnL this session
        self._session_pnl: Dict[str, float] = {}

        # ticker -> edge at last exit (for edge-increase re-entry requirement)
        self._exit_edge: Dict[str, float] = {}

        # Minimum edge increase required to re-enter after exit (prevents fee-burning loops)
        self.min_edge_increase_for_reentry = float(
            os.environ.get("WEATHER_ANTI_LOOP_MIN_EDGE_INCREASE", "0.05")
        )

        logger.info(
            f"AntiLoopDetector initialized: cooldown={self.cooldown_minutes}min, "
            f"max_losses={self.max_consecutive_losses}, "
            f"min_edge_increase={self.min_edge_increase_for_reentry:.0%}"
        )

    def record_exit(
        self, ticker: str, side: str, net_pnl: float, exit_edge: float = 0.0
    ) -> None:
        """
        Record that we exited a position.

        Args:
            ticker: Market ticker (e.g., "KXHIGHNY-26FEB12-B36.5")
            side: "yes" or "no"
            net_pnl: Net profit/loss in dollars (negative = loss)
            exit_edge: The edge at time of exit (for re-entry threshold)
        """
        now = datetime.now(timezone.utc)
        self._exit_timestamps[ticker] = now
        self._exit_edge[ticker] = exit_edge

        # Track consecutive losses
        if net_pnl < 0:
            self._loss_counts[ticker] = self._loss_counts.get(ticker, 0) + 1
            logger.info(
                f"[ANTI-LOOP] Exit recorded: {ticker} {side} "
                f"PnL=${net_pnl:+.2f} (loss #{self._loss_counts[ticker]})"
            )
        else:
            # Profitable exit resets the loss counter
            old_count = self._loss_counts.get(ticker, 0)
            self._loss_counts[ticker] = 0
            logger.info(
                f"[ANTI-LOOP] Exit recorded: {ticker} {side} "
                f"PnL=${net_pnl:+.2f} (profitable, reset loss count from {old_count})"
            )

        # Track session totals
        self._session_trade_counts[ticker] = (
            self._session_trade_counts.get(ticker, 0) + 1
        )
        self._session_pnl[ticker] = self._session_pnl.get(ticker, 0.0) + net_pnl

        # Log if this ticker is becoming problematic
        if self._loss_counts.get(ticker, 0) >= self.max_consecutive_losses:
            logger.warning(
                f"[ANTI-LOOP] BLACKLISTED {ticker}: "
                f"{self._loss_counts[ticker]} consecutive losses, "
                f"session PnL=${self._session_pnl.get(ticker, 0):.2f}"
            )

    def record_entry(self, ticker: str, side: str) -> None:
        """
        Record that we entered a position (for churn tracking).

        Args:
            ticker: Market ticker
            side: "yes" or "no"
        """
        self._session_trade_counts[ticker] = (
            self._session_trade_counts.get(ticker, 0) + 1
        )

    def can_re_enter(
        self, ticker: str, current_edge: float = 0.0
    ) -> Tuple[bool, str]:
        """
        Check if we can re-enter a position on this ticker.

        Args:
            ticker: Market ticker
            current_edge: The current edge for the new trade opportunity.
                          Used to check if edge has meaningfully increased
                          since last exit (prevents fee-burning buy/sell loops).

        Returns:
            (allowed: bool, reason: str)
            If allowed is False, reason explains why.
        """
        # Check blacklist first (fastest rejection)
        if self.is_blacklisted(ticker):
            count = self._loss_counts.get(ticker, 0)
            return (
                False,
                f"blacklisted ({count} consecutive losses)",
            )

        # Check cooldown period
        last_exit = self._exit_timestamps.get(ticker)
        if last_exit is not None:
            now = datetime.now(timezone.utc)
            elapsed = now - last_exit
            cooldown = timedelta(minutes=self.cooldown_minutes)

            if elapsed < cooldown:
                remaining = cooldown - elapsed
                mins_remaining = remaining.total_seconds() / 60.0

                # Exception: allow re-entry during cooldown if edge has spiked
                # significantly above the edge at exit
                prev_edge = self._exit_edge.get(ticker, 0.0)
                edge_increase = current_edge - prev_edge
                if edge_increase >= self.min_edge_increase_for_reentry:
                    logger.info(
                        f"[ANTI-LOOP] Allowing early re-entry for {ticker}: "
                        f"edge spiked {prev_edge:.1%} → {current_edge:.1%} "
                        f"(+{edge_increase:.1%} >= {self.min_edge_increase_for_reentry:.0%} threshold)"
                    )
                    return (True, "")

                return (
                    False,
                    f"cooldown ({mins_remaining:.0f}min remaining, "
                    f"need +{self.min_edge_increase_for_reentry:.0%} edge increase to override)",
                )

        # Check for excessive churn (>6 trades on same ticker in one session)
        session_count = self._session_trade_counts.get(ticker, 0)
        if session_count >= 6:
            return (
                False,
                f"excessive churn ({session_count} trades this session)",
            )

        return (True, "")

    def is_blacklisted(self, ticker: str) -> bool:
        """
        Check if a ticker is blacklisted due to repeated losses.

        Returns True if consecutive losses >= max_consecutive_losses.
        """
        return (
            self._loss_counts.get(ticker, 0) >= self.max_consecutive_losses
        )

    def get_ticker_stats(self, ticker: str) -> Dict:
        """Get tracking stats for a specific ticker."""
        return {
            "ticker": ticker,
            "consecutive_losses": self._loss_counts.get(ticker, 0),
            "session_trades": self._session_trade_counts.get(ticker, 0),
            "session_pnl": self._session_pnl.get(ticker, 0.0),
            "last_exit": self._exit_timestamps.get(ticker),
            "blacklisted": self.is_blacklisted(ticker),
        }

    def get_summary(self) -> Dict:
        """Get summary stats across all tracked tickers."""
        blacklisted = [t for t in self._loss_counts if self.is_blacklisted(t)]
        on_cooldown = []
        now = datetime.now(timezone.utc)
        cooldown_delta = timedelta(minutes=self.cooldown_minutes)

        for ticker, last_exit in self._exit_timestamps.items():
            if now - last_exit < cooldown_delta:
                on_cooldown.append(ticker)

        return {
            "total_tracked_tickers": len(self._exit_timestamps),
            "blacklisted_count": len(blacklisted),
            "blacklisted_tickers": blacklisted,
            "on_cooldown_count": len(on_cooldown),
            "on_cooldown_tickers": on_cooldown,
            "total_session_pnl": sum(self._session_pnl.values()),
        }

    def reset_ticker(self, ticker: str) -> None:
        """Manually reset tracking for a specific ticker (e.g., after manual review)."""
        self._exit_timestamps.pop(ticker, None)
        self._loss_counts.pop(ticker, None)
        self._session_trade_counts.pop(ticker, None)
        self._session_pnl.pop(ticker, None)
        logger.info(f"[ANTI-LOOP] Reset tracking for {ticker}")

    def reset_all(self) -> None:
        """Reset all tracking state (e.g., at start of new trading day)."""
        count = len(self._exit_timestamps)
        self._exit_timestamps.clear()
        self._loss_counts.clear()
        self._session_trade_counts.clear()
        self._session_pnl.clear()
        logger.info(f"[ANTI-LOOP] Reset all tracking ({count} tickers cleared)")
