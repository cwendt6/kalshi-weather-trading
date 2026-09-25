"""
Model Health Monitor - Detect drift vs bad luck.

Thresholds:
- Brier Score: Warning > 0.28, Critical > 0.35
- Win Rate: Warning < 48%, Critical < 40%
- Loss Streak: Warning 5-7, Critical 8+

Auto-Actions:
- Warning: Reduce position sizes 50%
- Critical: Pause trading, alert user
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Dict, List, Optional, Tuple

from math import sqrt

from src.data.database import get_db_session
from src.data.models import EdgeRealizationDB, PortfolioSnapshotDB, SignalExecutionDB, ForecastDB, TradeDB
from src.utils.logging import logger
from src.utils.probability_utils import norm_cdf as _norm_cdf


def _mean(values: list) -> float:
    """Simple mean (replaces np.mean)."""
    return sum(values) / len(values) if values else 0.0


def _brier_score(predictions: list, outcomes: list) -> float:
    """Brier score: mean squared error of probability predictions."""
    return _mean([(p - o) ** 2 for p, o in zip(predictions, outcomes)])


class HealthStatus(Enum):
    """Model health status levels."""
    EXCELLENT = "excellent"  # Brier < 0.20, Win rate > 55%
    GOOD = "good"           # Brier < 0.25, Win rate > 52%
    WARNING = "warning"     # Brier < 0.30, Win rate > 45%
    CRITICAL = "critical"   # Brier >= 0.30 or Win rate <= 45%


@dataclass
class ModelHealthThresholds:
    """Thresholds for model health assessment."""
    # Brier score thresholds (lower is better)
    BRIER_EXCELLENT: float = 0.20
    BRIER_GOOD: float = 0.25
    BRIER_WARNING: float = 0.28
    BRIER_CRITICAL: float = 0.35

    # Win rate thresholds
    WIN_RATE_EXCELLENT: float = 0.55
    WIN_RATE_GOOD: float = 0.52
    WIN_RATE_WARNING: float = 0.48
    WIN_RATE_CRITICAL: float = 0.40

    # Minimum trades for statistical significance
    MIN_TRADES_BASIC: int = 30
    MIN_TRADES_SIGNIFICANT: int = 50
    MIN_TRADES_STRONG: int = 100

    # Consecutive loss thresholds
    WARNING_CONSECUTIVE_LOSSES: int = 5
    MAX_CONSECUTIVE_LOSSES: int = 8

    # Recovery thresholds
    RECOVERY_MIN_TRADES: int = 20
    RECOVERY_BRIER_TARGET: float = 0.25
    RECOVERY_WIN_RATE_TARGET: float = 0.52


@dataclass
class HealthReport:
    """Detailed model health report."""
    status: HealthStatus
    brier_score: Optional[float]
    win_rate: Optional[float]
    total_trades: int
    winning_trades: int
    losing_trades: int
    current_streak: Tuple[str, int]  # ('win' or 'loss', count)
    is_statistically_significant: bool
    position_size_multiplier: float
    should_pause: bool
    alerts: List[str]
    timestamp: datetime


class ModelHealthMonitor:
    """
    Monitors model health and detects drift vs bad luck.

    Calculates:
    - Brier score for calibration
    - Rolling win rate
    - Consecutive loss streaks
    - Statistical significance

    Auto-actions:
    - Warning state: Reduce positions 50%
    - Critical state: Pause trading
    """

    def __init__(
        self,
        thresholds: Optional[ModelHealthThresholds] = None,
    ) -> None:
        """
        Initialize the Model Health Monitor.

        Args:
            thresholds: Custom thresholds for health assessment.
        """
        self.thresholds = thresholds or ModelHealthThresholds()
        self._paused = False
        self._pause_reason: Optional[str] = None
        self._warning_mode = False

        logger.info("ModelHealthMonitor initialized")

    def calculate_brier_score(
        self,
        window: int = 50,
    ) -> Optional[float]:
        """
        Calculate Brier score over last N resolved trades.

        Brier score = mean((predicted_prob - actual_outcome)^2)
        - 0.00 = perfect predictions
        - 0.25 = random guessing (50/50)
        - > 0.25 = worse than random

        Args:
            window: Number of recent trades to evaluate.

        Returns:
            Brier score or None if insufficient data.
        """
        trades = self._get_resolved_trades(window)

        if len(trades) < self.thresholds.MIN_TRADES_BASIC:
            return None

        predictions = []
        outcomes = []

        for trade in trades:
            # Get predicted probability (from confidence or edge)
            predicted_prob = self._get_predicted_probability(trade)
            if predicted_prob is None:
                continue

            # Get actual outcome (1 if trade was profitable, 0 otherwise)
            actual = 1 if self._trade_was_profitable(trade) else 0

            predictions.append(predicted_prob)
            outcomes.append(actual)

        if len(predictions) < self.thresholds.MIN_TRADES_BASIC:
            return None

        # Brier score = mean squared error
        brier = _brier_score(predictions, outcomes)

        return round(brier, 4)

    def calculate_rolling_win_rate(
        self,
        window: int = 50,
    ) -> Optional[float]:
        """
        Calculate win rate over last N trades.

        Args:
            window: Number of recent trades to evaluate.

        Returns:
            Win rate (0-1) or None if insufficient data.
        """
        trades = self._get_resolved_trades(window)

        if len(trades) == 0:
            return None

        wins = sum(1 for t in trades if self._trade_was_profitable(t))
        win_rate = wins / len(trades)

        return round(win_rate, 4)

    def get_current_streak(self) -> Tuple[str, int]:
        """
        Get current win/loss streak.

        Returns:
            Tuple of ('win' or 'loss', streak count).
        """
        trades = self._get_resolved_trades(limit=50)

        if not trades:
            return ('none', 0)

        streak_type = None
        streak_count = 0

        for trade in trades:  # Most recent first
            is_win = self._trade_was_profitable(trade)
            current_type = 'win' if is_win else 'loss'

            if streak_type is None:
                streak_type = current_type
                streak_count = 1
            elif current_type == streak_type:
                streak_count += 1
            else:
                break

        return (streak_type or 'none', streak_count)

    def is_statistically_significant(
        self,
        win_rate: float,
        n_trades: int,
        alpha: float = 0.05,
    ) -> bool:
        """
        Test if model performance is statistically significant.

        Uses z-test against null hypothesis of 50% win rate.

        Args:
            win_rate: Observed win rate.
            n_trades: Number of trades.
            alpha: Significance level.

        Returns:
            True if result is statistically significant.
        """
        if n_trades < self.thresholds.MIN_TRADES_BASIC:
            return False

        # Z-test against 50% null hypothesis
        z = (win_rate - 0.50) / sqrt(0.25 / n_trades)

        # Two-tailed test
        p_value = 2 * (1 - _norm_cdf(abs(z)))

        return p_value < alpha

    def is_model_broken(
        self,
        win_rate: float,
        n_trades: int,
        alpha: float = 0.05,
    ) -> bool:
        """
        Test if model is statistically worse than random.

        Args:
            win_rate: Observed win rate.
            n_trades: Number of trades.
            alpha: Significance level.

        Returns:
            True if model should be paused (statistically broken).
        """
        if n_trades < self.thresholds.MIN_TRADES_BASIC:
            return False

        # Z-test against 50% null hypothesis (one-tailed for underperformance)
        z = (win_rate - 0.50) / sqrt(0.25 / n_trades)
        p_value = _norm_cdf(z)

        return p_value < alpha

    def get_health_status(self) -> HealthStatus:
        """
        Get overall model health status.

        Returns:
            HealthStatus enum value.
        """
        brier = self.calculate_brier_score()
        win_rate = self.calculate_rolling_win_rate()
        streak_type, streak_count = self.get_current_streak()

        # Check for critical conditions
        if streak_type == 'loss' and streak_count >= self.thresholds.MAX_CONSECUTIVE_LOSSES:
            return HealthStatus.CRITICAL

        if brier is not None and brier >= self.thresholds.BRIER_CRITICAL:
            return HealthStatus.CRITICAL

        if win_rate is not None and win_rate <= self.thresholds.WIN_RATE_CRITICAL:
            return HealthStatus.CRITICAL

        # Check for warning conditions
        if streak_type == 'loss' and streak_count >= self.thresholds.WARNING_CONSECUTIVE_LOSSES:
            return HealthStatus.WARNING

        if brier is not None and brier >= self.thresholds.BRIER_WARNING:
            return HealthStatus.WARNING

        if win_rate is not None and win_rate <= self.thresholds.WIN_RATE_WARNING:
            return HealthStatus.WARNING

        # Check for good conditions
        if brier is not None and brier <= self.thresholds.BRIER_GOOD:
            if win_rate is not None and win_rate >= self.thresholds.WIN_RATE_GOOD:
                return HealthStatus.GOOD

        # Check for excellent conditions
        if brier is not None and brier <= self.thresholds.BRIER_EXCELLENT:
            if win_rate is not None and win_rate >= self.thresholds.WIN_RATE_EXCELLENT:
                return HealthStatus.EXCELLENT

        return HealthStatus.GOOD

    def should_pause_trading(self) -> bool:
        """
        Check if trading should be auto-paused.

        Returns:
            True if trading should pause.
        """
        # Don't pause until we have enough data to make a reliable judgment.
        # With <30 resolved trades, health metrics are too noisy to trust.
        trades = self._get_resolved_trades(limit=100)
        if len(trades) < self.thresholds.MIN_TRADES_BASIC:
            return False

        status = self.get_health_status()
        return status == HealthStatus.CRITICAL

    def get_position_size_multiplier(self) -> float:
        """
        Get position size multiplier based on health.

        Returns:
            1.0 for normal, 0.5 for warning, 0.0 for critical.
        """
        status = self.get_health_status()

        if status == HealthStatus.CRITICAL:
            return 0.0
        elif status == HealthStatus.WARNING:
            return 0.5
        elif status == HealthStatus.EXCELLENT:
            return 1.25  # Slight increase for excellent performance
        else:
            return 1.0

    def generate_health_report(self) -> HealthReport:
        """
        Generate comprehensive health report.

        Returns:
            HealthReport with all metrics and recommendations.
        """
        brier = self.calculate_brier_score()
        win_rate = self.calculate_rolling_win_rate()
        streak = self.get_current_streak()
        status = self.get_health_status()

        trades = self._get_resolved_trades(limit=100)
        total = len(trades)
        wins = sum(1 for t in trades if self._trade_was_profitable(t))
        losses = total - wins

        is_sig = False
        if win_rate is not None:
            is_sig = self.is_statistically_significant(win_rate, total)

        alerts = self._generate_alerts(brier, win_rate, streak, total)

        return HealthReport(
            status=status,
            brier_score=brier,
            win_rate=win_rate,
            total_trades=total,
            winning_trades=wins,
            losing_trades=losses,
            current_streak=streak,
            is_statistically_significant=is_sig,
            position_size_multiplier=self.get_position_size_multiplier(),
            should_pause=self.should_pause_trading(),
            alerts=alerts,
            timestamp=datetime.now(timezone.utc),
        )

    def _generate_alerts(
        self,
        brier: Optional[float],
        win_rate: Optional[float],
        streak: Tuple[str, int],
        total_trades: int,
    ) -> List[str]:
        """Generate alert messages based on current metrics."""
        alerts = []

        if total_trades < self.thresholds.MIN_TRADES_BASIC:
            alerts.append(f"Insufficient data: {total_trades}/{self.thresholds.MIN_TRADES_BASIC} trades")

        if brier is not None:
            if brier >= self.thresholds.BRIER_CRITICAL:
                alerts.append(f"CRITICAL: Brier score {brier:.3f} >= {self.thresholds.BRIER_CRITICAL}")
            elif brier >= self.thresholds.BRIER_WARNING:
                alerts.append(f"WARNING: Brier score {brier:.3f} >= {self.thresholds.BRIER_WARNING}")

        if win_rate is not None:
            if win_rate <= self.thresholds.WIN_RATE_CRITICAL:
                alerts.append(f"CRITICAL: Win rate {win_rate:.1%} <= {self.thresholds.WIN_RATE_CRITICAL:.0%}")
            elif win_rate <= self.thresholds.WIN_RATE_WARNING:
                alerts.append(f"WARNING: Win rate {win_rate:.1%} <= {self.thresholds.WIN_RATE_WARNING:.0%}")

        streak_type, streak_count = streak
        if streak_type == 'loss':
            if streak_count >= self.thresholds.MAX_CONSECUTIVE_LOSSES:
                alerts.append(f"CRITICAL: {streak_count} consecutive losses")
            elif streak_count >= self.thresholds.WARNING_CONSECUTIVE_LOSSES:
                alerts.append(f"WARNING: {streak_count} consecutive losses")

        return alerts

    def _get_resolved_trades(self, limit: int = 50) -> List:
        """Get resolved trades from database."""
        with next(get_db_session()) as session:
            try:
                # Try SignalExecutionDB first (preferred)
                trades = (
                    session.query(SignalExecutionDB)
                    .filter(SignalExecutionDB.resolution.isnot(None))
                    .order_by(SignalExecutionDB.resolved_at.desc())
                    .limit(limit)
                    .all()
                )
                if trades:
                    return trades
            except Exception:
                pass

            # Fallback to TradeDB
            try:
                return (
                    session.query(TradeDB)
                    .filter(TradeDB.status == "filled")
                    .order_by(TradeDB.timestamp.desc())
                    .limit(limit)
                    .all()
                )
            except Exception:
                return []

    def _get_predicted_probability(self, trade) -> Optional[float]:
        """Extract predicted probability from trade."""
        # For SignalExecutionDB
        if hasattr(trade, 'confidence') and trade.confidence:
            return float(trade.confidence)
        if hasattr(trade, 'edge') and trade.edge:
            # Convert edge to probability estimate
            # edge = (our_prob - market_prob) / market_prob
            # If we don't have market_prob, estimate from price
            if hasattr(trade, 'price') and trade.price:
                market_prob = trade.price / 100.0
                return market_prob * (1 + trade.edge)
        return 0.5  # Default to 50% if unknown

    def _trade_was_profitable(self, trade) -> bool:
        """Determine if a trade was profitable."""
        # For SignalExecutionDB
        if hasattr(trade, 'pnl') and trade.pnl is not None:
            return float(trade.pnl) > 0
        if hasattr(trade, 'resolution') and hasattr(trade, 'side'):
            # Win if resolution matches our side
            return trade.resolution == trade.side
        # Default to checking P&L
        return False

    def check_strategy_drift(self) -> List[str]:
        """
        Check for signs of strategy drift — advisory only, never pauses trading.

        Returns:
            List of drift alert strings (empty if no drift detected).
            Alerts prefixed with "CRITICAL:" indicate severe drift.
        """
        drift_alerts: List[str] = []

        try:
            # (a) Rolling 14-day win rate below 48%
            win_rate = self.calculate_rolling_win_rate(window=50)
            if win_rate is not None:
                if win_rate < 0.40:
                    drift_alerts.append(
                        f"CRITICAL: 14-day win rate {win_rate:.0%} below 40% — model may be broken"
                    )
                elif win_rate < 0.48:
                    drift_alerts.append(
                        f"14-day win rate {win_rate:.0%} below 48% — edge may be eroding"
                    )

            # (b) Edge leakage > 5% (from edge realization data)
            try:
                with next(get_db_session()) as session:
                    cutoff = datetime.now(timezone.utc) - timedelta(days=14)
                    realizations = (
                        session.query(EdgeRealizationDB)
                        .filter(EdgeRealizationDB.settlement_date >= cutoff)
                        .all()
                    )
                    if len(realizations) >= 10:
                        leakages = [
                            float(r.edge_leakage)
                            for r in realizations
                            if r.edge_leakage is not None
                        ]
                        if leakages:
                            avg_leakage = sum(leakages) / len(leakages)
                            if avg_leakage > 0.10:
                                drift_alerts.append(
                                    f"CRITICAL: Edge leakage {avg_leakage:.1%} — "
                                    f"model severely overconfident, tighten thresholds"
                                )
                            elif avg_leakage > 0.05:
                                drift_alerts.append(
                                    f"Edge leakage {avg_leakage:.1%} — "
                                    f"model overconfident, tighten thresholds"
                                )
            except Exception as e:
                logger.debug(f"Edge leakage drift check failed: {e}")

            # (c) Obs-settled trade frequency declining
            try:
                with next(get_db_session()) as session:
                    now = datetime.now(timezone.utc)
                    week_ago = now - timedelta(days=7)
                    two_weeks_ago = now - timedelta(days=14)

                    current_obs = (
                        session.query(TradeDB)
                        .filter(
                            TradeDB.timestamp >= week_ago,
                            TradeDB.strategy.isnot(None),
                            TradeDB.strategy.contains("obs"),
                        )
                        .count()
                    )
                    previous_obs = (
                        session.query(TradeDB)
                        .filter(
                            TradeDB.timestamp >= two_weeks_ago,
                            TradeDB.timestamp < week_ago,
                            TradeDB.strategy.isnot(None),
                            TradeDB.strategy.contains("obs"),
                        )
                        .count()
                    )

                    if previous_obs > 0 and current_obs < previous_obs * 0.5:
                        decline_pct = 1.0 - (current_obs / previous_obs)
                        drift_alerts.append(
                            f"Obs-settled opportunities down {decline_pct:.0%} — "
                            f"market may be getting faster"
                        )
            except Exception as e:
                logger.debug(f"Obs-settled drift check failed: {e}")

            # (d) Daily trade count anomaly — running but not finding opportunities
            try:
                with next(get_db_session()) as session:
                    now = datetime.now(timezone.utc)
                    week_ago = now - timedelta(days=7)

                    week_trades = (
                        session.query(TradeDB)
                        .filter(TradeDB.timestamp >= week_ago)
                        .count()
                    )
                    avg_daily = week_trades / 7.0

                    if avg_daily < 2.0:
                        # Check if bankroll hasn't decreased (bot is running, just
                        # not finding opportunities — not a bankroll depletion issue)
                        two_weeks_ago = now - timedelta(days=14)
                        old_snapshot = (
                            session.query(PortfolioSnapshotDB)
                            .filter(PortfolioSnapshotDB.timestamp >= two_weeks_ago)
                            .order_by(PortfolioSnapshotDB.timestamp.asc())
                            .first()
                        )
                        recent_snapshot = (
                            session.query(PortfolioSnapshotDB)
                            .order_by(PortfolioSnapshotDB.timestamp.desc())
                            .first()
                        )

                        bankroll_stable = True
                        if old_snapshot and recent_snapshot:
                            old_eq = float(old_snapshot.total_equity or 0)
                            new_eq = float(recent_snapshot.total_equity or 0)
                            if old_eq > 0 and new_eq < old_eq * 0.90:
                                bankroll_stable = False  # Bankroll dropped >10%, skip alert

                        if bankroll_stable:
                            drift_alerts.append(
                                f"Low trade frequency ({avg_daily:.1f}/day) — "
                                f"check edge thresholds or market conditions"
                            )
            except Exception as e:
                logger.debug(f"Trade frequency drift check failed: {e}")

        except Exception as e:
            logger.warning(f"Strategy drift check failed: {e}")

        return drift_alerts

    def pause_trading(self, reason: str) -> None:
        """Manually pause trading."""
        self._paused = True
        self._pause_reason = reason
        logger.warning(f"Trading paused: {reason}")

    def resume_trading(self) -> None:
        """Resume trading after pause."""
        self._paused = False
        self._pause_reason = None
        logger.info("Trading resumed")

    @property
    def is_paused(self) -> bool:
        """Check if trading is paused."""
        return self._paused or self.should_pause_trading()


# Singleton instance
_health_monitor: Optional[ModelHealthMonitor] = None


def get_health_monitor(
    thresholds: Optional[ModelHealthThresholds] = None,
) -> ModelHealthMonitor:
    """Get or create the health monitor singleton."""
    global _health_monitor
    if _health_monitor is None:
        _health_monitor = ModelHealthMonitor(thresholds=thresholds)
    return _health_monitor


def get_health_report() -> HealthReport:
    """Convenience function to get current health report."""
    monitor = get_health_monitor()
    return monitor.generate_health_report()


def calculate_brier_score(
    predictions: List[float],
    outcomes: List[int],
) -> float:
    """
    Calculate Brier score for a set of predictions.

    Args:
        predictions: List of predicted probabilities (0-1).
        outcomes: List of actual outcomes (0 or 1).

    Returns:
        Brier score (0 = perfect, 0.25 = random, 1 = always wrong).
    """
    return _brier_score(predictions, outcomes)


def consecutive_loss_probability(win_rate: float, streak: int) -> float:
    """
    Calculate probability of seeing N consecutive losses.

    Args:
        win_rate: Expected win rate.
        streak: Number of consecutive losses.

    Returns:
        Probability of this streak occurring.
    """
    loss_rate = 1 - win_rate
    return loss_rate ** streak
