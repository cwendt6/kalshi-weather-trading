"""
SQLAlchemy models for database persistence.

Defines tables for markets, prices, trades, positions, forecasts, and news.
"""
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import (
    Column,
    Date,
    DateTime,
    Float,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    """
    A DateTime type that ensures all values are timezone-aware UTC.

    SQLite stores datetimes as naive strings. When loaded, this type
    automatically attaches UTC timezone info so that arithmetic with
    datetime.now(timezone.utc) never raises:
        "can't subtract offset-naive and offset-aware datetimes"

    Also handles legacy 'Z'-suffixed ISO strings in the database that
    Python 3.10's datetime.fromisoformat() cannot parse (fixed in 3.11).

    IMPORTANT: We override result_processor() to bypass SQLAlchemy 2.0's
    base DateTime processor, which calls datetime.fromisoformat() internally
    and crashes on corrupt/non-datetime strings in the DB (e.g., after DB
    corruption). Our process_result_value has try/except protection.
    """

    impl = DateTime
    cache_ok = True

    def result_processor(self, dialect, coltype):
        """Bypass base DateTime result processor to handle corrupt DB data.

        SQLAlchemy 2.0's DateTime.result_processor calls fromisoformat()
        BEFORE our process_result_value gets to run.  If the DB contains
        corrupt strings (e.g., a ticker in a close_time column after DB
        corruption), the base processor raises ValueError.

        By overriding result_processor, we route directly to our own
        process_result_value which has try/except protection.
        """
        def process(value):
            return self.process_result_value(value, dialect)
        return process

    def process_bind_param(self, value, dialect):
        """Ensure datetimes are stored as proper UTC objects, not Z-strings."""
        if value is None:
            return None
        if isinstance(value, str):
            cleaned = value.replace("Z", "+00:00") if value.endswith("Z") else value
            try:
                value = datetime.fromisoformat(cleaned)
            except (ValueError, TypeError):
                return None
        if isinstance(value, datetime) and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value

    def process_result_value(self, value, dialect):
        """Handle both datetime objects and legacy Z-suffixed strings from DB."""
        if value is None:
            return None
        # SQLite may return raw strings if the stored format doesn't match
        # SQLAlchemy's expected pattern (e.g., '2026-02-11T04:59:00Z')
        if isinstance(value, str):
            cleaned = value.replace("Z", "+00:00") if value.endswith("Z") else value
            try:
                value = datetime.fromisoformat(cleaned)
            except (ValueError, TypeError):
                return None
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=timezone.utc)
        return value


class SafeNumeric(TypeDecorator):
    """
    A Numeric type that survives corrupt data in SQLite.

    SQLAlchemy 2.0's base Numeric result processor calls Decimal() on the
    raw SQLite string.  If the DB contains corrupt non-numeric strings
    (e.g., after WAL corruption and recovery), the base processor raises:
        TypeError: must be real number, not str

    This override routes directly to our process_result_value which has
    try/except protection, returning Decimal(0) for corrupt values.
    """

    impl = Numeric
    cache_ok = True

    def __init__(self, precision=12, scale=2, **kwargs):
        super().__init__(precision=precision, scale=scale, **kwargs)

    def result_processor(self, dialect, coltype):
        """Bypass base Numeric result processor to handle corrupt DB data."""
        def process(value):
            return self.process_result_value(value, dialect)
        return process

    def process_result_value(self, value, dialect):
        """Convert DB value to Decimal, returning Decimal(0) for corrupt data."""
        if value is None:
            return None
        if isinstance(value, Decimal):
            return value
        try:
            return Decimal(str(value))
        except Exception:
            return Decimal(0)

    def process_bind_param(self, value, dialect):
        """Store values as proper Decimal."""
        if value is None:
            return None
        try:
            return Decimal(str(value))
        except Exception:
            return Decimal(0)


class Base(DeclarativeBase):
    """Base class for all database models."""

    pass


class MarketDB(Base):
    """Market data storage."""

    __tablename__ = "markets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(100), unique=True, nullable=False, index=True)
    title = Column(String(500), nullable=False)
    category = Column(String(100), nullable=True, index=True)
    status = Column(String(50), nullable=False, index=True)
    close_time = Column(UTCDateTime, nullable=True)
    strike_date = Column(UTCDateTime, nullable=True)
    result = Column(String(10), nullable=True)
    volume = Column(Integer, nullable=True)
    open_interest = Column(Integer, nullable=True)
    created_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        UTCDateTime, nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("idx_markets_status_close_time", "status", "close_time"),
        Index("idx_markets_category_status", "category", "status"),
    )


class PriceDB(Base):
    """Price snapshot storage."""

    __tablename__ = "prices"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(100), nullable=False, index=True)
    timestamp = Column(UTCDateTime, nullable=False, index=True)
    yes_bid = Column(Integer, nullable=True)
    yes_ask = Column(Integer, nullable=True)
    no_bid = Column(Integer, nullable=True)
    no_ask = Column(Integer, nullable=True)
    volume = Column(Integer, nullable=True)
    open_interest = Column(Integer, nullable=True)
    created_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("idx_prices_ticker_timestamp", "ticker", "timestamp"),
        Index("idx_prices_timestamp", "timestamp"),
    )


class TradeDB(Base):
    """Trade execution record."""

    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(String(100), unique=True, nullable=False, index=True)
    fill_id = Column(String(100), unique=True, nullable=True)
    ticker = Column(String(100), nullable=False, index=True)
    side = Column(String(10), nullable=False)
    action = Column(String(10), nullable=False)
    quantity = Column(Integer, nullable=False)
    price = Column(Integer, nullable=False)
    fee = Column(SafeNumeric(10, 2), nullable=True)
    status = Column(String(50), nullable=False, index=True)
    timestamp = Column(UTCDateTime, nullable=False, index=True)
    created_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.now(timezone.utc))

    # Resolution and P&L tracking
    resolved = Column(Integer, nullable=False, default=0)  # 0 = open, 1 = resolved
    outcome = Column(String(10), nullable=True)  # 'win', 'loss', or None
    pnl = Column(SafeNumeric(10, 2), nullable=True)  # Realized P&L in dollars
    pnl_percent = Column(Float, nullable=True)  # P&L as percentage of cost
    market_result = Column(String(10), nullable=True)  # 'yes', 'no' - market resolution
    resolved_at = Column(UTCDateTime, nullable=True)

    # Strategy attribution
    strategy = Column(String(50), nullable=True, index=True)  # 'impossible_scanner', 'signal', 'straddle', etc.

    # Edge at entry time (decimal: 0.08 = 8%)
    edge = Column(Float, nullable=True)

    __table_args__ = (
        Index("idx_trades_ticker_timestamp", "ticker", "timestamp"),
        Index("idx_trades_status_timestamp", "status", "timestamp"),
    )


class PositionDB(Base):
    """Current position tracking."""

    __tablename__ = "positions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(100), unique=True, nullable=False, index=True)
    side = Column(String(10), nullable=False)
    quantity = Column(Integer, nullable=False)
    average_price = Column(Integer, nullable=False)
    market_price = Column(Integer, nullable=True)
    unrealized_pnl = Column(SafeNumeric(10, 2), nullable=True)
    realized_pnl = Column(SafeNumeric(10, 2), nullable=True, default=0)
    created_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        UTCDateTime, nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (Index("idx_positions_ticker_side", "ticker", "side"),)


class ForecastDB(Base):
    """Probability forecast storage.

    Stores every P(event) estimate alongside the features that produced it,
    the market price at forecast time, and (after resolution) the actual outcome.
    This is the core ML training data table — every row becomes one training sample.
    """

    __tablename__ = "forecasts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(100), nullable=False, index=True)
    timestamp = Column(UTCDateTime, nullable=False, index=True)
    probability = Column(Float, nullable=False)  # Model's P(YES) estimate
    confidence = Column(Float, nullable=True)  # Model confidence (forecast quality)
    method = Column(
        String(50), nullable=False
    )  # 'rule_based', 'prob_engine_weather', 'xgboost', etc.
    edge = Column(Float, nullable=True)  # probability - market_probability
    market_probability = Column(Float, nullable=True)  # Market-implied prob at forecast time
    context_data = Column(Text, nullable=True)  # JSON string for additional context

    # === ML Training Data (added for Point 3: feature engineering) ===
    features_json = Column(Text, nullable=True)  # JSON: full feature dict for ML training
    domain = Column(String(30), nullable=True)  # 'weather', 'crypto', 'sports', 'economics'

    # === Resolution Tracking (added for Point 2: correct framing) ===
    # y ∈ {0, 1} — the actual event outcome, filled after market resolves
    resolution = Column(Integer, nullable=True)  # 1 = YES resolved, 0 = NO resolved, NULL = pending
    resolved_at = Column(UTCDateTime, nullable=True)  # When outcome became known

    # === Market Context at Forecast Time (meta-features per Point 3) ===
    spread_cents = Column(Integer, nullable=True)  # yes_ask - yes_bid at forecast time
    volume_at_forecast = Column(Integer, nullable=True)  # Market volume at forecast time
    open_interest_at_forecast = Column(Integer, nullable=True)
    hours_to_expiry = Column(Float, nullable=True)  # Hours until market closes
    market_age_hours = Column(Float, nullable=True)  # Hours since market was created

    # === Calibration (added for Point 7: force calibration) ===
    calibration_bucket = Column(Float, nullable=True)  # Rounded prob for bucketing (0.05, 0.10, ..., 0.95)
    brier_score = Column(Float, nullable=True)  # (probability - resolution)^2, filled after resolution

    created_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("idx_forecasts_ticker_timestamp", "ticker", "timestamp"),
        Index("idx_forecasts_method_timestamp", "method", "timestamp"),
        Index("idx_forecasts_domain", "domain"),
        Index("idx_forecasts_resolution", "resolution"),
        Index("idx_forecasts_calibration", "calibration_bucket", "resolution"),
    )


class NewsDB(Base):
    """News article storage."""

    __tablename__ = "news"

    id = Column(Integer, primary_key=True, autoincrement=True)
    article_id = Column(
        String(200), unique=True, nullable=False, index=True
    )  # URL hash or unique ID
    source = Column(String(100), nullable=False, index=True)  # 'finnhub', 'rss', etc.
    title = Column(String(1000), nullable=False)
    content = Column(Text, nullable=True)
    url = Column(String(1000), nullable=True)
    published_at = Column(UTCDateTime, nullable=False, index=True)
    sentiment_score = Column(Float, nullable=True)
    sentiment_label = Column(String(20), nullable=True)  # 'positive', 'negative', etc.
    keywords = Column(Text, nullable=True)  # Comma-separated keywords
    created_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("idx_news_source_published", "source", "published_at"),
        Index("idx_news_published", "published_at"),
    )


class NewsMarketRelevanceDB(Base):
    """Links news articles to relevant markets."""

    __tablename__ = "news_market_relevance"

    id = Column(Integer, primary_key=True, autoincrement=True)
    news_id = Column(Integer, nullable=False, index=True)
    ticker = Column(String(100), nullable=False, index=True)
    relevance_score = Column(Float, nullable=False)
    matched_keywords = Column(Text, nullable=True)  # JSON array of matched keywords
    created_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("idx_relevance_news_ticker", "news_id", "ticker"),
        Index("idx_relevance_ticker_score", "ticker", "relevance_score"),
    )


class PortfolioSnapshotDB(Base):
    """Portfolio value snapshot for performance tracking."""

    __tablename__ = "portfolio_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(UTCDateTime, nullable=False, unique=True, index=True)
    balance = Column(SafeNumeric(12, 2), nullable=False)
    total_position_value = Column(SafeNumeric(12, 2), nullable=False)
    unrealized_pnl = Column(SafeNumeric(12, 2), nullable=False)
    realized_pnl = Column(SafeNumeric(12, 2), nullable=False)
    total_equity = Column(SafeNumeric(12, 2), nullable=False)
    daily_return = Column(Float, nullable=True)
    created_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (Index("idx_snapshots_timestamp", "timestamp"),)


class PolymarketPriceDB(Base):
    """Polymarket price data for cross-platform comparison."""

    __tablename__ = "polymarket_prices"

    id = Column(Integer, primary_key=True, autoincrement=True)
    condition_id = Column(String(100), nullable=False, index=True)
    ticker = Column(
        String(100), nullable=True, index=True
    )  # Matched Kalshi ticker if available
    title = Column(String(500), nullable=False)
    timestamp = Column(UTCDateTime, nullable=False, index=True)
    yes_price = Column(Float, nullable=True)  # Polymarket uses decimal prices 0-1
    no_price = Column(Float, nullable=True)
    volume = Column(Float, nullable=True)
    liquidity = Column(Float, nullable=True)
    created_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("idx_polymarket_condition_timestamp", "condition_id", "timestamp"),
        Index("idx_polymarket_ticker_timestamp", "ticker", "timestamp"),
    )


class SignalExecutionDB(Base):
    """Signal execution tracking for automated trading."""

    __tablename__ = "signal_executions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    signal_id = Column(String(50), unique=True, nullable=False, index=True)
    ticker = Column(String(100), nullable=False, index=True)
    action = Column(String(20), nullable=False)  # 'buy_yes', 'buy_no', 'sell_yes', 'sell_no'
    side = Column(String(10), nullable=False)  # 'yes' or 'no'
    quantity = Column(Integer, nullable=False)
    price = Column(Integer, nullable=False)  # Entry price in cents
    edge = Column(Float, nullable=False)
    confidence = Column(Float, nullable=True)
    strength = Column(String(20), nullable=True)  # 'strong', 'moderate', 'weak'
    expected_value = Column(Float, nullable=True)
    smart_money_score = Column(Float, nullable=True)

    # Execution details
    executed_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    status = Column(String(20), nullable=False, default="pending", index=True)  # 'pending', 'filled', 'cancelled', 'failed'
    order_id = Column(String(100), nullable=True)  # Kalshi order ID if live
    fill_price = Column(Integer, nullable=True)

    # Resolution and P&L
    resolved_at = Column(UTCDateTime, nullable=True)
    resolution = Column(String(10), nullable=True)  # 'yes', 'no', None if not resolved
    pnl = Column(SafeNumeric(10, 2), nullable=True)  # Updated on resolution
    pnl_percent = Column(Float, nullable=True)

    # Paper vs live
    is_paper = Column(Integer, nullable=False, default=1)  # 1 = paper, 0 = live

    created_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        UTCDateTime, nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("idx_signal_exec_ticker_status", "ticker", "status"),
        Index("idx_signal_exec_executed_at", "executed_at"),
        Index("idx_signal_exec_status_paper", "status", "is_paper"),
    )


class ArbitrageOpportunityDB(Base):
    """Arbitrage opportunity tracking between platforms."""

    __tablename__ = "arbitrage_opportunities"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Market identifiers
    kalshi_ticker = Column(String(100), nullable=False, index=True)
    kalshi_title = Column(String(500), nullable=True)
    poly_condition_id = Column(String(100), nullable=False, index=True)
    poly_title = Column(String(500), nullable=True)

    # Prices at discovery (Kalshi in cents, Poly as 0-1)
    kalshi_yes_price = Column(Integer, nullable=False)
    kalshi_no_price = Column(Integer, nullable=False)
    poly_yes_price = Column(Float, nullable=False)
    poly_no_price = Column(Float, nullable=False)

    # Arbitrage metrics
    spread = Column(Float, nullable=False)  # Raw spread in dollars
    net_profit = Column(Float, nullable=False)  # Profit after fees
    profit_percent = Column(Float, nullable=True)

    # Strategy
    strategy = Column(String(50), nullable=False)  # e.g., "buy_kalshi_yes_poly_no"
    kalshi_side = Column(String(10), nullable=False)
    poly_side = Column(String(10), nullable=False)

    # Confidence
    confidence_score = Column(Float, nullable=False)
    match_reason = Column(String(200), nullable=True)

    # Execution tracking
    status = Column(String(20), nullable=False, default="discovered", index=True)  # discovered, executed, expired, invalid
    executed_at = Column(UTCDateTime, nullable=True)
    actual_profit = Column(Float, nullable=True)

    # Timestamps
    discovered_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    expires_at = Column(UTCDateTime, nullable=True)
    created_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("idx_arb_kalshi_poly", "kalshi_ticker", "poly_condition_id"),
        Index("idx_arb_status_discovered", "status", "discovered_at"),
        Index("idx_arb_profit", "net_profit"),
    )


class BacktestResultDB(Base):
    """Backtest run results storage."""

    __tablename__ = "backtest_results"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Run identification
    run_id = Column(String(50), unique=True, nullable=False, index=True)
    strategy_name = Column(String(100), nullable=False, index=True)

    # Time period
    start_date = Column(UTCDateTime, nullable=False)
    end_date = Column(UTCDateTime, nullable=False)
    duration_days = Column(Integer, nullable=False)

    # Capital
    initial_capital = Column(Float, nullable=False)
    final_equity = Column(Float, nullable=False)

    # Returns
    total_return = Column(Float, nullable=False)
    total_return_pct = Column(Float, nullable=False)
    cagr = Column(Float, nullable=True)

    # Risk metrics
    sharpe_ratio = Column(Float, nullable=True)
    sortino_ratio = Column(Float, nullable=True)
    max_drawdown = Column(Float, nullable=True)
    max_drawdown_pct = Column(Float, nullable=True)

    # Trade statistics
    total_trades = Column(Integer, nullable=False)
    winning_trades = Column(Integer, nullable=False)
    losing_trades = Column(Integer, nullable=False)
    win_rate = Column(Float, nullable=False)
    profit_factor = Column(Float, nullable=True)

    # Parameters (stored as JSON string)
    parameters = Column(Text, nullable=True)

    # Equity curve (stored as JSON string)
    equity_curve_json = Column(Text, nullable=True)

    # Metadata
    created_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    notes = Column(Text, nullable=True)

    __table_args__ = (
        Index("idx_backtest_strategy_date", "strategy_name", "created_at"),
        Index("idx_backtest_return", "total_return_pct"),
    )


class NewsArticleDB(Base):
    """Stored news article from email newsletters."""

    __tablename__ = "news_articles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    email_id = Column(String(200), unique=True, nullable=False, index=True)
    source = Column(String(100), nullable=False, index=True)
    subject = Column(String(1000), nullable=True)
    snippet = Column(String(2000), nullable=True)
    body_text = Column(Text, nullable=True)

    # Extracted features (populated by LLM one-time parse)
    sentiment_score = Column(Float, nullable=True)  # -1 to 1
    entities = Column(Text, nullable=True)  # JSON: List of mentioned entities
    market_relevance = Column(Text, nullable=True)  # JSON: ticker -> relevance
    key_facts = Column(Text, nullable=True)  # JSON: Extracted bullet points

    # Metadata
    received_at = Column(UTCDateTime, nullable=True)
    processed_at = Column(UTCDateTime, nullable=True)
    created_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("idx_news_source_received", "source", "received_at"),
        Index("idx_news_received", "received_at"),
    )


class CalibrationSummaryDB(Base):
    """Calibration curve snapshot for model evaluation.

    Periodically computed: buckets all resolved forecasts by predicted probability
    and checks actual resolution rate per bucket. A perfectly calibrated model
    should have: when it says 70%, events resolve YES ~70% of the time.

    This is Point 7: Force calibration (mandatory).
    """

    __tablename__ = "calibration_summaries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    computed_at = Column(UTCDateTime, nullable=False, index=True)

    # Which model/method this calibration covers
    method = Column(String(50), nullable=False, index=True)  # 'prob_engine_weather', 'all', etc.
    domain = Column(String(30), nullable=True)  # 'weather', 'crypto', 'sports', 'economics', or NULL for all

    # Calibration bucket (e.g., 0.70 = forecasts in 0.65-0.75 range)
    bucket = Column(Float, nullable=False)  # Center of bucket (0.05 to 0.95)
    bucket_count = Column(Integer, nullable=False)  # Number of forecasts in this bucket
    actual_resolution_rate = Column(Float, nullable=True)  # % that actually resolved YES
    predicted_mean = Column(Float, nullable=True)  # Average predicted probability in bucket
    brier_score = Column(Float, nullable=True)  # Mean (pred - outcome)^2 for this bucket

    # Overall stats (only populated for bucket=0, the "summary row")
    total_forecasts = Column(Integer, nullable=True)
    total_resolved = Column(Integer, nullable=True)
    overall_brier_score = Column(Float, nullable=True)  # Mean Brier across all forecasts
    overall_log_loss = Column(Float, nullable=True)  # Mean log loss across all forecasts

    created_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("idx_calibration_method_bucket", "method", "bucket"),
        Index("idx_calibration_computed", "computed_at"),
    )


class LongshotPositionDB(Base):
    """Longshot position tracking with exit ladder."""

    __tablename__ = "longshot_positions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(100), nullable=False, index=True)
    title = Column(String(500), nullable=True)
    side = Column(String(10), nullable=False)  # 'yes' or 'no' — must be explicit

    # Entry details
    entry_price = Column(Float, nullable=False)  # Price per contract (0-1)
    entry_quantity = Column(Integer, nullable=False)
    entry_time = Column(UTCDateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    entry_order_id = Column(String(100), nullable=True)
    confidence = Column(String(20), nullable=False)  # 'high', 'medium', 'low'

    # Ladder orders (JSON: list of {level, target_price, quantity, status, order_id, filled_price, filled_at})
    ladder_orders = Column(Text, nullable=True)

    # Current state
    remaining_quantity = Column(Integer, nullable=False)
    realized_pnl = Column(SafeNumeric(10, 2), nullable=True, default=0)

    # Resolution
    resolved = Column(Integer, nullable=False, default=0)  # 0 = open, 1 = resolved
    resolution = Column(String(10), nullable=True)  # 'yes', 'no', or None
    resolved_at = Column(UTCDateTime, nullable=True)
    final_pnl = Column(SafeNumeric(10, 2), nullable=True)

    # Metadata
    created_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        UTCDateTime, nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    __table_args__ = (
        Index("idx_longshot_ticker_resolved", "ticker", "resolved"),
        Index("idx_longshot_entry_time", "entry_time"),
        Index("idx_longshot_confidence", "confidence"),
    )


class ForecastSnapshotDB(Base):
    """Forecast snapshot for audit trail — logs each forecast point-in-time."""

    __tablename__ = "forecast_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    city = Column(String(50), nullable=False)
    market_date = Column(Date, nullable=False)
    forecast_temp = Column(Float, nullable=False)
    nws_confidence = Column(String(20), nullable=True)
    source = Column(String(30), nullable=True)  # "nws", "openweather", "mock"
    hours_to_close = Column(Float, nullable=True)
    snapshot_time = Column(UTCDateTime, nullable=False, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("idx_forecast_snap_city_date", "city", "market_date"),
        Index("idx_forecast_snap_time", "snapshot_time"),
    )


class SettlementDB(Base):
    """Settlement record — actual observed value when a market resolves."""

    __tablename__ = "settlements"

    id = Column(Integer, primary_key=True, autoincrement=True)
    city = Column(String(50), nullable=False)
    market_date = Column(Date, nullable=False)
    settlement_temp = Column(Float, nullable=False)
    settlement_source = Column(String(30), nullable=True)
    recorded_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("idx_settlement_city_date", "city", "market_date", unique=True),
    )


class EdgeRealizationDB(Base):
    """Edge realization tracking — compares predicted edge at entry to actual outcome.

    Populated after trade settlement. This is the core diagnostic for whether
    the model's predicted edge translates into real returns.
    """

    __tablename__ = "edge_realizations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(100), nullable=False, index=True)
    trade_id = Column(String(100), nullable=True)  # Links to TradeDB.order_id
    entry_date = Column(UTCDateTime, nullable=True)
    settlement_date = Column(UTCDateTime, nullable=True)
    predicted_edge = Column(Float, nullable=True)  # Edge at time of entry (decimal)
    entry_price = Column(Integer, nullable=True)  # Price paid (cents)
    side = Column(String(10), nullable=True)  # "yes" or "no"
    settlement_outcome = Column(Integer, nullable=True)  # 1=won, 0=lost
    actual_return_pct = Column(Float, nullable=True)  # Actual % return on the trade
    edge_leakage = Column(Float, nullable=True)  # predicted_edge - actual_return_pct
    city = Column(String(50), nullable=True)
    bracket_role = Column(String(50), nullable=True)  # "far_no", "forecast_yes", etc.
    created_at = Column(UTCDateTime, nullable=False, default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        Index("idx_edge_real_city_settlement", "city", "settlement_date"),
        Index("idx_edge_real_ticker", "ticker"),
    )
