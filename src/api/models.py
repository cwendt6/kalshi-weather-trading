"""
Pydantic models for Kalshi API responses.

Models represent the structure of data returned by the Kalshi API.
"""
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


class Market(BaseModel):
    """Kalshi market model."""

    ticker: str = Field(..., description="Market ticker symbol")
    title: str = Field(..., description="Market question/title")
    status: str = Field(..., description="Market status (active, closed, settled)")
    yes_bid: Optional[int] = Field(None, description="Best YES bid price in cents")
    yes_ask: Optional[int] = Field(None, description="Best YES ask price in cents")
    no_bid: Optional[int] = Field(None, description="Best NO bid price in cents")
    no_ask: Optional[int] = Field(None, description="Best NO ask price in cents")
    # Kalshi API may return volume as 'volume', 'volume_24h', or 'total_volume'
    volume: Optional[int] = Field(
        None,
        description="Total trading volume",
        validation_alias=AliasChoices("volume", "volume_24h", "total_volume"),
    )
    open_interest: Optional[int] = Field(
        None, description="Open interest (active contracts)"
    )
    close_time: Optional[datetime] = Field(
        None, description="Market close/expiration time"
    )
    category: Optional[str] = Field(None, description="Market category")
    strike_date: Optional[datetime] = Field(None, description="Strike/resolution date")
    result: Optional[str] = Field(None, description="Market result (yes/no)")

    # ── Fields needed for threshold extraction (time decay strategy) ──
    # The Kalshi API returns these but they were previously dropped by the model.
    # floor_strike contains the "Price to beat" value (e.g., 66906.05 for BTC).
    floor_strike: Optional[float] = Field(None, description="Lower strike/threshold price")
    cap_strike: Optional[float] = Field(None, description="Upper strike/cap price")
    custom_strike: Optional[str] = Field(None, description="Custom strike value")
    subtitle: Optional[str] = Field(None, description="Market subtitle (often has threshold)")
    yes_sub_title: Optional[str] = Field(None, description="YES side description")
    no_sub_title: Optional[str] = Field(None, description="NO side description")
    rules_primary: Optional[str] = Field(None, description="Primary market rules text")
    event_title: Optional[str] = Field(None, description="Parent event title")
    # Additional price fields
    yes_price: Optional[int] = Field(None, description="Last YES price in cents")
    last_price: Optional[int] = Field(None, description="Last traded price in cents")

    model_config = ConfigDict(
        populate_by_name=True,  # Allow population by field name and alias
        extra="allow",          # Preserve ALL API fields (future-proof)
    )


class OrderbookEntry(BaseModel):
    """Single orderbook entry (bid or ask)."""

    price: int = Field(..., description="Price in cents (1-99)")
    quantity: int = Field(..., description="Number of contracts at this price")


class Orderbook(BaseModel):
    """Market orderbook with bids and asks."""

    ticker: str = Field(..., description="Market ticker symbol")
    yes_bids: List[OrderbookEntry] = Field(
        default_factory=list, description="YES side bids"
    )
    yes_asks: List[OrderbookEntry] = Field(
        default_factory=list, description="YES side asks"
    )
    no_bids: List[OrderbookEntry] = Field(
        default_factory=list, description="NO side bids"
    )
    no_asks: List[OrderbookEntry] = Field(
        default_factory=list, description="NO side asks"
    )


class PriceSnapshot(BaseModel):
    """Historical price snapshot."""

    timestamp: datetime = Field(..., description="Snapshot timestamp")
    yes_bid: Optional[int] = Field(None, description="YES bid price in cents")
    yes_ask: Optional[int] = Field(None, description="YES ask price in cents")
    no_bid: Optional[int] = Field(None, description="NO bid price in cents")
    no_ask: Optional[int] = Field(None, description="NO ask price in cents")
    volume: Optional[int] = Field(None, description="Volume at this timestamp")


class MarketHistory(BaseModel):
    """Historical market data."""

    ticker: str = Field(..., description="Market ticker symbol")
    snapshots: List[PriceSnapshot] = Field(
        default_factory=list, description="Historical price snapshots"
    )


class Position(BaseModel):
    """Trading position from Kalshi API.

    Kalshi v2 returns market_positions with fields like:
      ticker, position, market_exposure, total_traded, realized_pnl, etc.
    We derive side/quantity/average_price from these raw fields.
    """

    ticker: str = Field(..., description="Market ticker")
    position: int = Field(0, description="Net contract position (positive=yes, negative=no)")
    total_traded: int = Field(0, description="Total contracts traded")
    market_exposure: int = Field(0, description="Market exposure in cents")
    market_exposure_dollars: Optional[str] = Field(None, description="Market exposure in dollars")
    realized_pnl: int = Field(0, description="Realized PnL in cents")
    realized_pnl_dollars: Optional[str] = Field(None, description="Realized PnL in dollars")
    resting_orders_count: int = Field(0, description="Resting orders count")
    fees_paid: int = Field(0, description="Fees paid in cents")
    total_traded_dollars: Optional[str] = Field(None)
    fees_paid_dollars: Optional[str] = Field(None)
    last_updated_ts: Optional[str] = Field(None)
    position_fp: Optional[str] = Field(None)

    model_config = {"extra": "allow"}

    @property
    def side(self) -> str:
        """Derive side from position sign: positive = yes, negative = no."""
        return "yes" if self.position >= 0 else "no"

    @property
    def quantity(self) -> int:
        """Absolute number of contracts held."""
        return abs(self.position)

    @property
    def average_price(self) -> int:
        """Derive average price in cents from exposure / quantity."""
        if self.quantity == 0:
            return 0
        return abs(self.market_exposure) // self.quantity


class Order(BaseModel):
    """Trading order from Kalshi API.

    Kalshi v2 returns: type (not order_type), initial_count (not quantity),
    fill_count (not filled_quantity), created_time (not created_at), etc.
    """

    order_id: str = Field(..., description="Unique order ID")
    ticker: str = Field(..., description="Market ticker")
    side: str = Field(..., description="Order side (yes/no)")
    action: str = Field(..., description="Order action (buy/sell)")

    # Kalshi returns "type", not "order_type"
    type: str = Field("limit", description="Order type (limit/market)")

    # Kalshi returns count fields, not "quantity"
    initial_count: int = Field(0, description="Original number of contracts")
    fill_count: int = Field(0, description="Filled contracts")
    remaining_count: int = Field(0, description="Remaining unfilled contracts")

    # Price fields
    yes_price: Optional[int] = Field(None, description="Yes price in cents")
    no_price: Optional[int] = Field(None, description="No price in cents")

    status: str = Field("pending", description="Order status")

    # Kalshi returns "created_time" / "last_update_time", not "created_at"
    created_time: Optional[datetime] = Field(None, description="Order creation time")
    last_update_time: Optional[datetime] = Field(None, description="Last update time")

    model_config = {"extra": "allow"}

    # Backwards-compatible properties for existing code
    @property
    def order_type(self) -> str:
        return self.type

    @property
    def quantity(self) -> int:
        return self.initial_count

    @property
    def filled_quantity(self) -> int:
        return self.fill_count

    @property
    def price(self) -> Optional[int]:
        return self.yes_price or self.no_price

    @property
    def created_at(self) -> Optional[datetime]:
        return self.created_time


class Fill(BaseModel):
    """Trade fill."""

    fill_id: str = Field(..., description="Unique fill ID")
    order_id: str = Field(..., description="Order ID")
    ticker: str = Field(..., description="Market ticker")
    side: str = Field(..., description="Fill side (yes/no)")
    action: str = Field(..., description="Fill action (buy/sell)")
    quantity: int = Field(..., description="Filled contracts")
    price: int = Field(..., description="Fill price in cents")
    timestamp: datetime = Field(..., description="Fill timestamp")
    fee: Optional[float] = Field(None, description="Trading fee in dollars")


class Balance(BaseModel):
    """Account balance."""

    balance: float = Field(..., description="Available balance in cents")
    portfolio_value: Optional[float] = Field(None, description="Total portfolio value in cents (cash + positions at market value)")
    payout: Optional[float] = Field(None, description="Pending payout in dollars")
