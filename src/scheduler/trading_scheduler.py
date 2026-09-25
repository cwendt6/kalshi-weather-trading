"""
Trading scheduler for automated signal execution.

Runs an autonomous trading loop that:
1. Generates trading signals
2. Filters to executable signals
3. Executes approved signals
4. Logs all activity
"""
import asyncio
import signal
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
import json
import os

from src.execution.signal_executor import SignalExecutor, get_signal_executor
from src.strategy.signal_generator import SignalGenerator, SignalStrength, get_signal_generator
from src.strategy.forecast_generator import get_forecast_generator
from src.execution.risk_manager import get_risk_manager
from src.utils.logging import logger


class SchedulerStatus(Enum):
    """Scheduler status states."""

    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    ERROR = "error"


@dataclass
class SchedulerStats:
    """Statistics for the trading scheduler."""

    started_at: Optional[datetime] = None
    stopped_at: Optional[datetime] = None
    cycles_completed: int = 0
    signals_generated: int = 0
    signals_executed: int = 0
    signals_failed: int = 0
    last_cycle_at: Optional[datetime] = None
    last_error: Optional[str] = None
    total_pnl: float = 0.0


@dataclass
class SchedulerConfig:
    """Configuration for the trading scheduler - AGGRESSIVE settings."""

    interval_seconds: int = 60  # Signal generation interval
    min_edge: float = 0.02  # 2% minimum edge (was 3%)
    min_confidence: float = 0.40  # 40% confidence (was 50%)
    max_positions: int = 100  # 100 positions (was 50)
    min_signal_strength: str = "moderate"  # minimum strength: 'weak', 'moderate', 'strong'
    paper_trading: bool = True  # Paper trading mode
    auto_execute: bool = True  # Auto-execute approved signals
    notify_on_trade: bool = True  # Send notifications on trade

    def to_dict(self) -> Dict[str, Any]:
        """Convert config to dictionary."""
        return {
            "interval_seconds": self.interval_seconds,
            "min_edge": self.min_edge,
            "min_confidence": self.min_confidence,
            "max_positions": self.max_positions,
            "min_signal_strength": self.min_signal_strength,
            "paper_trading": self.paper_trading,
            "auto_execute": self.auto_execute,
            "notify_on_trade": self.notify_on_trade,
        }


class TradingScheduler:
    """
    Automated trading scheduler.

    Runs a continuous loop that generates and executes trading signals
    based on configured parameters.
    """

    # Status file for persistence
    STATUS_FILE = "data/scheduler_status.json"

    def __init__(
        self,
        config: Optional[SchedulerConfig] = None,
    ) -> None:
        """
        Initialize the trading scheduler.

        Args:
            config: Scheduler configuration. Uses defaults if None.
        """
        self.config = config or SchedulerConfig()
        self.status = SchedulerStatus.STOPPED
        self.stats = SchedulerStats()

        # Components
        self.signal_generator = get_signal_generator(
            min_edge=self.config.min_edge,
            min_confidence=self.config.min_confidence,
        )
        self.signal_executor = get_signal_executor(
            paper_trading=self.config.paper_trading
        )
        self.risk_manager = get_risk_manager()

        # Control flags
        self._shutdown_requested = False
        self._pause_requested = False
        self._task: Optional[asyncio.Task] = None

        logger.info(
            "Trading scheduler initialized",
            interval=self.config.interval_seconds,
            min_edge=self.config.min_edge,
            paper_trading=self.config.paper_trading,
        )

    def _setup_signal_handlers(self) -> None:
        """Setup signal handlers for graceful shutdown."""

        def signal_handler(signum: int, frame: Any) -> None:
            logger.info("Scheduler shutdown signal received", signal=signum)
            self._shutdown_requested = True

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

    def _get_min_strength(self) -> SignalStrength:
        """Get minimum signal strength from config."""
        strength_map = {
            "weak": SignalStrength.WEAK,
            "moderate": SignalStrength.MODERATE,
            "strong": SignalStrength.STRONG,
        }
        return strength_map.get(
            self.config.min_signal_strength, SignalStrength.MODERATE
        )

    async def _run_cycle(self) -> None:
        """Run one trading cycle."""
        cycle_start = datetime.now(timezone.utc)

        try:
            # Check if trading is paused globally
            if self.risk_manager.is_trading_paused():
                logger.debug("Trading is globally paused, skipping cycle")
                return

            # Generate forecasts first (populates the forecasts table)
            try:
                forecast_gen = get_forecast_generator()
                forecast_result = await forecast_gen.generate_forecasts()
                if forecast_result.forecasts_created > 0:
                    logger.info(
                        "Forecasts generated",
                        count=forecast_result.forecasts_created,
                        impossible=forecast_result.impossible_found,
                        straddles=forecast_result.straddle_found,
                        btc=forecast_result.btc_signals,
                    )
            except Exception as e:
                logger.warning(f"Forecast generation failed: {e}")

            # Generate signals (reads from forecasts table)
            signals = self.signal_generator.generate_signals()
            self.stats.signals_generated += len(signals)

            if not signals:
                logger.debug("No signals generated this cycle")
                return

            # Filter to executable signals
            min_strength = self._get_min_strength()
            executable = [
                s for s in signals
                if s.risk_approved
                and s.strength.value >= min_strength.value
                and s.recommended_contracts > 0
            ]

            logger.info(
                "Cycle signals",
                total=len(signals),
                executable=len(executable),
                min_strength=min_strength.value,
            )

            if not executable:
                return

            # Execute signals if auto_execute is enabled
            if self.config.auto_execute:
                results = self.signal_executor.execute_signals(
                    executable, max_concurrent=self.config.max_positions
                )

                for result in results:
                    if result.success:
                        self.stats.signals_executed += 1
                        logger.info(
                            "Signal executed",
                            signal_id=result.signal.signal_id,
                            ticker=result.signal.ticker,
                            edge=f"{result.signal.edge*100:.1f}%",
                        )
                    else:
                        self.stats.signals_failed += 1
                        logger.warning(
                            "Signal execution failed",
                            signal_id=result.signal.signal_id,
                            error=result.message,
                        )
            else:
                logger.info(
                    "Auto-execute disabled, skipping execution",
                    signals=len(executable),
                )

            # Update stats
            self.stats.cycles_completed += 1
            self.stats.last_cycle_at = datetime.now(timezone.utc)

        except Exception as e:
            self.stats.last_error = str(e)
            logger.error("Cycle failed", error=str(e))

    async def _run_loop(self) -> None:
        """Main scheduler loop."""
        self.status = SchedulerStatus.RUNNING
        self.stats.started_at = datetime.now(timezone.utc)

        logger.info(
            "Scheduler loop started",
            interval=self.config.interval_seconds,
            paper=self.config.paper_trading,
        )

        while not self._shutdown_requested:
            # Check for pause
            if self._pause_requested:
                self.status = SchedulerStatus.PAUSED
                await asyncio.sleep(1)
                continue

            self.status = SchedulerStatus.RUNNING

            # Run trading cycle
            await self._run_cycle()

            # Wait for next cycle
            await asyncio.sleep(self.config.interval_seconds)

        # Cleanup
        self.status = SchedulerStatus.STOPPING
        logger.info("Scheduler loop stopping")

    async def start(self) -> None:
        """Start the trading scheduler."""
        if self.status == SchedulerStatus.RUNNING:
            logger.warning("Scheduler already running")
            return

        self.status = SchedulerStatus.STARTING
        self._shutdown_requested = False
        self._pause_requested = False
        self._setup_signal_handlers()

        logger.info("Starting trading scheduler...")

        # Run the main loop
        await self._run_loop()

        # Mark as stopped
        self.status = SchedulerStatus.STOPPED
        self.stats.stopped_at = datetime.now(timezone.utc)

        # Save final status
        self._save_status()

        logger.info(
            "Scheduler stopped",
            cycles=self.stats.cycles_completed,
            executed=self.stats.signals_executed,
            failed=self.stats.signals_failed,
        )

    def start_background(self) -> asyncio.Task:
        """Start scheduler as background task."""
        if self._task and not self._task.done():
            logger.warning("Scheduler task already running")
            return self._task

        self._task = asyncio.create_task(self.start())
        return self._task

    def stop(self) -> None:
        """Request scheduler stop."""
        logger.info("Stop requested")
        self._shutdown_requested = True

    def pause(self) -> None:
        """Pause the scheduler."""
        logger.info("Pause requested")
        self._pause_requested = True
        self.status = SchedulerStatus.PAUSED

    def resume(self) -> None:
        """Resume the scheduler."""
        logger.info("Resume requested")
        self._pause_requested = False

    def get_status(self) -> Dict[str, Any]:
        """Get current scheduler status."""
        runtime = None
        if self.stats.started_at:
            end_time = self.stats.stopped_at or datetime.now(timezone.utc)
            runtime = (end_time - self.stats.started_at).total_seconds()

        return {
            "status": self.status.value,
            "config": self.config.to_dict(),
            "stats": {
                "started_at": self.stats.started_at.isoformat() if self.stats.started_at else None,
                "stopped_at": self.stats.stopped_at.isoformat() if self.stats.stopped_at else None,
                "runtime_seconds": runtime,
                "cycles_completed": self.stats.cycles_completed,
                "signals_generated": self.stats.signals_generated,
                "signals_executed": self.stats.signals_executed,
                "signals_failed": self.stats.signals_failed,
                "last_cycle_at": self.stats.last_cycle_at.isoformat() if self.stats.last_cycle_at else None,
                "last_error": self.stats.last_error,
            },
        }

    def _save_status(self) -> None:
        """Save status to file for persistence."""
        try:
            os.makedirs(os.path.dirname(self.STATUS_FILE), exist_ok=True)
            with open(self.STATUS_FILE, "w") as f:
                json.dump(self.get_status(), f, indent=2)
        except Exception as e:
            logger.error("Failed to save scheduler status", error=str(e))

    def _load_status(self) -> Optional[Dict[str, Any]]:
        """Load status from file."""
        try:
            if os.path.exists(self.STATUS_FILE):
                with open(self.STATUS_FILE, "r") as f:
                    return json.load(f)
        except Exception as e:
            logger.error("Failed to load scheduler status", error=str(e))
        return None


# Global instance
_scheduler: Optional[TradingScheduler] = None


def get_scheduler(
    config: Optional[SchedulerConfig] = None,
    reset: bool = False,
) -> TradingScheduler:
    """Get or create the global scheduler instance."""
    global _scheduler
    if _scheduler is None or reset:
        _scheduler = TradingScheduler(config=config)
    return _scheduler


async def start_scheduler(
    interval: int = 60,
    min_edge: float = 0.05,
    paper_trading: bool = True,
    auto_execute: bool = True,
) -> None:
    """
    Convenience function to start the trading scheduler.

    Args:
        interval: Signal generation interval in seconds.
        min_edge: Minimum edge threshold.
        paper_trading: Whether to use paper trading mode.
        auto_execute: Whether to auto-execute signals.
    """
    config = SchedulerConfig(
        interval_seconds=interval,
        min_edge=min_edge,
        paper_trading=paper_trading,
        auto_execute=auto_execute,
    )
    scheduler = get_scheduler(config=config, reset=True)
    await scheduler.start()


def stop_scheduler() -> None:
    """Stop the global scheduler."""
    if _scheduler:
        _scheduler.stop()
