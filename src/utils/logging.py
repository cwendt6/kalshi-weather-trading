"""
Logging configuration using loguru.

Provides structured logging with file rotation, JSON output, and trade audit trail.
"""
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from loguru import logger

from config.settings import settings

if TYPE_CHECKING:
    from loguru import Logger


def setup_logging(
    log_level: Optional[str] = None,
    log_file: Optional[Path] = None,
    json_output: bool = False,
) -> None:
    """
    Configure loguru logger with rotation, retention, and structured output.

    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL).
                  Defaults to settings.log_level.
        log_file: Path to log file. Defaults to settings.log_file.
        json_output: Enable JSON structured logging. Default: False.
    """
    level = log_level or settings.log_level
    file_path = log_file or settings.log_file

    # Remove default handler
    logger.remove()

    # Console handler with colorization
    log_format = (
        "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
        "<level>{message}</level>"
    )

    if json_output:
        # Structured JSON output for production
        logger.add(
            sys.stderr,
            format="{message}",
            level=level,
            serialize=True,  # JSON format
        )
    else:
        # Human-readable format for development
        logger.add(
            sys.stderr,
            format=log_format,
            level=level,
            colorize=True,
        )

    # File handler with rotation and retention
    file_path.parent.mkdir(parents=True, exist_ok=True)
    logger.add(
        str(file_path),
        format=log_format,
        level=level,
        rotation="100 MB",  # Rotate when file reaches 100 MB
        retention="30 days",  # Keep logs for 30 days
        compression="zip",  # Compress rotated logs
        enqueue=True,  # Thread-safe logging
    )

    logger.info(
        f"Logging initialized: level={level}, file={file_path}, json={json_output}"
    )


def setup_trade_logger(log_dir: Optional[Path] = None) -> Any:
    """
    Create a dedicated logger for trade audit trail.

    All trades, orders, fills, and position changes are logged here
    for compliance and debugging.

    Args:
        log_dir: Directory for trade logs. Defaults to logs/trades/

    Returns:
        Logger instance configured for trade audit trail.
    """
    if log_dir is None:
        log_dir = Path("logs/trades")

    log_dir.mkdir(parents=True, exist_ok=True)
    trade_log_path = log_dir / "trades.log"

    # Create a separate logger for trades
    trade_logger = logger.bind(logger_name="trade_audit")

    # Add file handler specifically for trades
    trade_logger.add(
        str(trade_log_path),
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
            "{extra[logger_name]} | {message}"
        ),
        level="INFO",
        rotation="50 MB",
        retention="1 year",  # Keep trade logs for 1 year for audit
        compression="zip",
        enqueue=True,
        filter=lambda record: record["extra"].get("logger_name") == "trade_audit",
    )

    logger.info(f"Trade audit logger initialized: {trade_log_path}")
    return trade_logger


# Initialize logging on module import
setup_logging()

# Create trade logger
trade_logger = setup_trade_logger()


# Convenience functions for structured logging
def log_market_data(
    ticker: str,
    bid: float,
    ask: float,
    volume: int,
    **extra: object,
) -> None:
    """Log market data snapshot."""
    logger.info(
        "Market data",
        ticker=ticker,
        bid=bid,
        ask=ask,
        volume=volume,
        **extra,
    )


def log_trade(
    action: str,
    ticker: str,
    side: str,
    quantity: int,
    price: float,
    order_id: Optional[str] = None,
    strategy: Optional[str] = None,
    reason: Optional[str] = None,
    pnl: Optional[float] = None,
    balance: Optional[float] = None,
    **extra: object,
) -> None:
    """Log trade execution to audit trail with structured fields.

    Args:
        action: "BUY" or "SELL"
        ticker: Market ticker
        side: "yes" or "no"
        quantity: Number of contracts
        price: Price in cents
        order_id: Unique order identifier
        strategy: Strategy that generated the signal (e.g., "weather", "crypto")
        reason: Entry/exit reason (e.g., "momentum_entry", "stop_loss")
        pnl: Net profit/loss for exits (dollars)
        balance: Account balance after trade (dollars)
    """
    parts = [
        f"TRADE | {action} | {ticker} | {side} | qty={quantity} | {price}c",
    ]
    if strategy:
        parts.append(f"strategy={strategy}")
    if reason:
        parts.append(f"reason={reason}")
    if pnl is not None:
        parts.append(f"pnl=${pnl:+.2f}")
    if balance is not None:
        parts.append(f"bal=${balance:.2f}")
    if order_id:
        parts.append(f"id={order_id}")

    trade_logger.info(
        " | ".join(parts),
        action=action,
        ticker=ticker,
        side=side,
        quantity=quantity,
        price=price,
        order_id=order_id,
        strategy=strategy,
        reason=reason,
        pnl=pnl,
        balance=balance,
        **extra,
    )


def log_risk_event(
    event_type: str,
    message: str,
    **extra: object,
) -> None:
    """Log risk management events (limit hits, pauses, etc.)."""
    logger.warning(
        f"RISK EVENT | {event_type} | {message}",
        event_type=event_type,
        message=message,
        **extra,
    )


def log_error(
    error_type: str,
    message: str,
    exception: Optional[Exception] = None,
    **extra: object,
) -> None:
    """Log errors with context."""
    if exception:
        logger.exception(
            f"ERROR | {error_type} | {message}",
            error_type=error_type,
            message=message,
            **extra,
        )
    else:
        logger.error(
            f"ERROR | {error_type} | {message}",
            error_type=error_type,
            message=message,
            **extra,
        )
