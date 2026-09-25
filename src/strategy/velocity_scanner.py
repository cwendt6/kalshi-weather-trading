"""
Velocity Scanner — find fast-moving markets and ride momentum.

Scans PriceDB snapshots to detect rapid price movement, then generates
entry signals for markets with strong directional momentum. Pairs with
momentum-based exits in PositionReEvaluator for a complete scalp cycle:

  Enter on velocity → Exit on reversal/stall

Position sizing adapts to velocity tier:
  - EXPLOSIVE (10c+/30min): small positions (high risk, quick scalp)
  - FAST (5c+/30min): moderate positions
  - MODERATE (2c+/30min): larger positions (steadier trend)
"""

import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from src.data.database import get_db_session
from src.data.models import MarketDB, PositionDB, PriceDB, TradeDB
from src.utils.fees import KALSHI_WINNER_FEE_RATE
from src.utils.logging import logger


class VelocityTier(Enum):
    """Classification of price movement speed."""

    EXPLOSIVE = "explosive"  # 10c+ in 30 min
    FAST = "fast"            # 5c+ in 30 min
    MODERATE = "moderate"    # 2c+ in 30 min
    SLOW = "slow"            # <2c in 30 min


@dataclass
class VelocityOpportunity:
    """A market showing significant price velocity."""

    ticker: str
    title: str
    tier: VelocityTier
    direction: str  # "up" or "down"

    # Velocity metrics
    velocity_30min: float  # cents per minute over last 30 min
    velocity_1h: float     # cents per hour over last hour
    velocity_4h: float     # cents per hour over last 4 hours
    acceleration: float    # change in velocity (positive = speeding up)
    volume_ratio: float    # current volume vs average (>1 = above average)
    momentum_score: float  # weighted 0-1 score

    # Price data
    current_price: int     # current yes_bid in cents
    price_30min_ago: int   # yes_bid 30 min ago
    price_1h_ago: int      # yes_bid 1 hour ago

    # Entry signal
    suggested_side: str    # "yes" or "no"
    suggested_quantity: int
    suggested_price: int   # entry price in cents

    # Market metadata
    close_time: Optional[datetime] = None
    category: str = "unknown"


@dataclass
class VelocityScanResult:
    """Result of a velocity scan cycle."""

    markets_scanned: int
    fast_movers_found: int
    opportunities: List[VelocityOpportunity]
    errors: int


class VelocityScanner:
    """
    Scans markets for rapid price movement and generates entry signals.

    Uses PriceDB snapshots to calculate velocity (rate of price change),
    acceleration (change in velocity), and volume ratios. Markets are
    classified into velocity tiers and scored for momentum quality.
    """

    # Velocity thresholds (cents moved in 30 minutes)
    EXPLOSIVE_THRESHOLD = int(os.getenv("VELOCITY_EXPLOSIVE_CENTS", "10"))
    FAST_THRESHOLD = int(os.getenv("VELOCITY_FAST_CENTS", "5"))
    MODERATE_THRESHOLD = int(os.getenv("VELOCITY_MODERATE_CENTS", "2"))

    # Scan settings
    MIN_SNAPSHOTS = int(os.getenv("VELOCITY_MIN_SNAPSHOTS", "3"))
    SCAN_INTERVAL = int(os.getenv("VELOCITY_SCAN_INTERVAL", "120"))
    MAX_ENTRIES_PER_CYCLE = int(os.getenv("VELOCITY_MAX_ENTRIES", "20"))

    # Market filters
    MIN_HOURS_TO_EXPIRY = float(os.getenv("VELOCITY_MIN_HOURS_EXPIRY", "0.25"))
    MAX_HOURS_TO_EXPIRY = float(os.getenv("VELOCITY_MAX_HOURS_EXPIRY", "72"))

    # Position sizing by tier (contracts)
    TIER_SIZES = {
        VelocityTier.EXPLOSIVE: (10, 20),   # min, max contracts
        VelocityTier.FAST: (30, 50),
        VelocityTier.MODERATE: (50, 100),
    }

    def __init__(self, paper_trading: bool = True) -> None:
        """Initialize velocity scanner."""
        self.paper_trading = paper_trading
        self._stats: Dict[str, Any] = {
            "total_scans": 0,
            "total_opportunities": 0,
            "trades_generated": 0,
        }

        logger.info(
            "VelocityScanner initialized",
            explosive=f"{self.EXPLOSIVE_THRESHOLD}c",
            fast=f"{self.FAST_THRESHOLD}c",
            moderate=f"{self.MODERATE_THRESHOLD}c",
            paper=paper_trading,
        )

    def _calculate_velocity(self, ticker: str) -> Optional[Dict[str, Any]]:
        """
        Calculate price velocity for a market from PriceDB snapshots.

        Queries the last 4 hours of snapshots and computes velocity
        over 30min, 1h, and 4h windows plus acceleration and volume.

        Returns None if insufficient data.
        """
        now = datetime.now(timezone.utc)
        cutoff_4h = now - timedelta(hours=4)

        try:
            with next(get_db_session()) as session:
                snapshots = (
                    session.query(PriceDB)
                    .filter(
                        PriceDB.ticker == ticker,
                        PriceDB.timestamp >= cutoff_4h,
                    )
                    .order_by(PriceDB.timestamp.asc())
                    .all()
                )

                if len(snapshots) < self.MIN_SNAPSHOTS:
                    return None

                # Extract data within session to avoid DetachedInstanceError
                snapshot_data = []
                for s in snapshots:
                    ts = s.timestamp
                    if ts and ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    snapshot_data.append({
                        "timestamp": ts,
                        "yes_bid": s.yes_bid or 0,
                        "volume": s.volume or 0,
                    })

        except Exception as e:
            logger.debug(f"Velocity calculation failed for {ticker}: {e}")
            return None

        if not snapshot_data:
            return None

        # Current price
        current = snapshot_data[-1]
        current_price = current["yes_bid"]
        if current_price <= 0:
            return None

        # Find snapshots closest to time windows
        snap_30min = self._find_snapshot_near(snapshot_data, now - timedelta(minutes=30))
        snap_1h = self._find_snapshot_near(snapshot_data, now - timedelta(hours=1))
        snap_4h = snapshot_data[0]  # oldest = ~4h ago

        # 30-min velocity (cents per minute)
        if snap_30min and snap_30min["yes_bid"] > 0:
            price_30min = snap_30min["yes_bid"]
            delta_30min = current_price - price_30min
            time_diff_min = max(1, (current["timestamp"] - snap_30min["timestamp"]).total_seconds() / 60)
            velocity_30min = delta_30min / time_diff_min
        else:
            return None  # Need at least 30min data

        # 1-hour velocity (cents per hour)
        if snap_1h and snap_1h["yes_bid"] > 0:
            price_1h = snap_1h["yes_bid"]
            delta_1h = current_price - price_1h
            time_diff_hr = max(0.1, (current["timestamp"] - snap_1h["timestamp"]).total_seconds() / 3600)
            velocity_1h = delta_1h / time_diff_hr
        else:
            price_1h = price_30min
            velocity_1h = velocity_30min * 60  # extrapolate

        # 4-hour velocity (cents per hour)
        if snap_4h["yes_bid"] > 0:
            price_4h = snap_4h["yes_bid"]
            delta_4h = current_price - price_4h
            time_diff_4h = max(0.1, (current["timestamp"] - snap_4h["timestamp"]).total_seconds() / 3600)
            velocity_4h = delta_4h / time_diff_4h
        else:
            price_4h = price_1h
            velocity_4h = velocity_1h

        # Acceleration: is velocity increasing or decreasing?
        # Compare recent velocity (30min) to longer-term velocity (1h)
        acceleration = velocity_30min * 60 - velocity_1h  # both in c/hr

        # Volume ratio: recent vs average
        recent_volumes = [s["volume"] for s in snapshot_data[-3:] if s["volume"] > 0]
        all_volumes = [s["volume"] for s in snapshot_data if s["volume"] > 0]
        if recent_volumes and all_volumes:
            avg_recent = sum(recent_volumes) / len(recent_volumes)
            avg_all = sum(all_volumes) / len(all_volumes)
            volume_ratio = avg_recent / avg_all if avg_all > 0 else 1.0
        else:
            volume_ratio = 1.0

        # Direction
        abs_delta_30 = abs(current_price - (snap_30min["yes_bid"] if snap_30min else current_price))
        direction = "up" if velocity_30min > 0 else "down"

        return {
            "velocity_30min": velocity_30min,
            "velocity_1h": velocity_1h,
            "velocity_4h": velocity_4h,
            "acceleration": acceleration,
            "volume_ratio": volume_ratio,
            "direction": direction,
            "current_price": current_price,
            "price_30min_ago": snap_30min["yes_bid"] if snap_30min else current_price,
            "price_1h_ago": price_1h,
            "abs_move_30min": abs_delta_30,
        }

    @staticmethod
    def _find_snapshot_near(
        snapshots: List[Dict[str, Any]], target_time: datetime
    ) -> Optional[Dict[str, Any]]:
        """Find the snapshot closest to target_time."""
        if not snapshots:
            return None

        best = None
        best_diff = float("inf")
        for s in snapshots:
            ts = s["timestamp"]
            if ts is None:
                continue
            diff = abs((ts - target_time).total_seconds())
            if diff < best_diff:
                best_diff = diff
                best = s

        return best

    def _classify_velocity(self, velocity_30min: float, velocity_1h: float) -> VelocityTier:
        """Classify velocity into a tier based on absolute movement in 30 min."""
        abs_move_30min = abs(velocity_30min) * 30  # total cents in 30 min

        if abs_move_30min >= self.EXPLOSIVE_THRESHOLD:
            return VelocityTier.EXPLOSIVE
        elif abs_move_30min >= self.FAST_THRESHOLD:
            return VelocityTier.FAST
        elif abs_move_30min >= self.MODERATE_THRESHOLD:
            return VelocityTier.MODERATE
        else:
            return VelocityTier.SLOW

    def _calculate_momentum_score(
        self, velocity: float, acceleration: float, volume_ratio: float
    ) -> float:
        """
        Calculate a weighted momentum score from 0 to 1.

        Components:
        - Velocity magnitude (40% weight)
        - Acceleration (30% weight) — positive = speeding up
        - Volume ratio (30% weight) — above average = confirmation
        """
        # Velocity score: normalize to 0-1 (10c/30min = 1.0)
        abs_vel_30min = abs(velocity) * 30  # total cents moved in 30 min
        vel_score = min(1.0, abs_vel_30min / 10.0)

        # Acceleration score: positive acceleration is good
        # Normalize: 5c/hr acceleration = 1.0
        accel_score = min(1.0, max(0.0, acceleration / 5.0)) if velocity > 0 else \
                      min(1.0, max(0.0, -acceleration / 5.0)) if velocity < 0 else 0.0

        # Volume score: above average is good
        vol_score = min(1.0, max(0.0, (volume_ratio - 0.5) / 1.5))

        return vel_score * 0.4 + accel_score * 0.3 + vol_score * 0.3

    def scan_markets(self) -> VelocityScanResult:
        """
        Scan all active markets for velocity opportunities.

        Returns a VelocityScanResult with fast-moving markets sorted
        by tier (EXPLOSIVE first) then momentum score.
        """
        self._stats["total_scans"] += 1
        opportunities: List[VelocityOpportunity] = []
        errors = 0
        markets_scanned = 0

        try:
            # Get active markets with recent price data
            with next(get_db_session()) as session:
                now = datetime.now(timezone.utc)
                cutoff = now - timedelta(hours=4)

                # Find tickers with recent snapshots (efficient subquery)
                from sqlalchemy import func, distinct
                active_tickers = (
                    session.query(distinct(PriceDB.ticker))
                    .filter(PriceDB.timestamp >= cutoff)
                    .all()
                )

                ticker_list = [t[0] for t in active_tickers]

                # Get market metadata for these tickers
                market_map: Dict[str, Dict[str, Any]] = {}
                if ticker_list:
                    markets = (
                        session.query(MarketDB)
                        .filter(
                            MarketDB.ticker.in_(ticker_list),
                            MarketDB.status == "active",
                        )
                        .all()
                    )
                    for m in markets:
                        close_time = m.close_time
                        if close_time and close_time.tzinfo is None:
                            close_time = close_time.replace(tzinfo=timezone.utc)
                        market_map[m.ticker] = {
                            "title": str(m.title or m.ticker),
                            "category": str(m.category or "unknown"),
                            "close_time": close_time,
                        }

        except Exception as e:
            logger.error(f"Velocity scanner market query failed: {e}")
            return VelocityScanResult(
                markets_scanned=0,
                fast_movers_found=0,
                opportunities=[],
                errors=1,
            )

        # Calculate velocity for each ticker
        now = datetime.now(timezone.utc)
        for ticker in ticker_list:
            markets_scanned += 1

            try:
                velocity = self._calculate_velocity(ticker)
                if velocity is None:
                    continue

                tier = self._classify_velocity(
                    velocity["velocity_30min"], velocity["velocity_1h"]
                )
                if tier == VelocityTier.SLOW:
                    continue

                # Check market metadata
                meta = market_map.get(ticker)
                if meta is None:
                    continue

                # Check expiry window
                close_time = meta.get("close_time")
                if close_time:
                    hours_to_expiry = (close_time - now).total_seconds() / 3600
                    if hours_to_expiry < self.MIN_HOURS_TO_EXPIRY:
                        continue
                    if hours_to_expiry > self.MAX_HOURS_TO_EXPIRY:
                        continue

                # Check if already positioned
                if self._already_positioned(ticker):
                    continue

                # Calculate momentum score
                score = self._calculate_momentum_score(
                    velocity["velocity_30min"],
                    velocity["acceleration"],
                    velocity["volume_ratio"],
                )

                # Generate entry signal
                entry = self._generate_entry_signal(
                    ticker, tier, velocity, meta
                )
                if entry is None:
                    continue

                opp = VelocityOpportunity(
                    ticker=ticker,
                    title=meta["title"],
                    tier=tier,
                    direction=velocity["direction"],
                    velocity_30min=velocity["velocity_30min"],
                    velocity_1h=velocity["velocity_1h"],
                    velocity_4h=velocity["velocity_4h"],
                    acceleration=velocity["acceleration"],
                    volume_ratio=velocity["volume_ratio"],
                    momentum_score=score,
                    current_price=velocity["current_price"],
                    price_30min_ago=velocity["price_30min_ago"],
                    price_1h_ago=velocity["price_1h_ago"],
                    suggested_side=entry["side"],
                    suggested_quantity=entry["quantity"],
                    suggested_price=entry["price"],
                    close_time=close_time,
                    category=meta["category"],
                )
                opportunities.append(opp)

            except Exception as e:
                logger.debug(f"Velocity scan error for {ticker}: {e}")
                errors += 1

        # Sort: tier priority (EXPLOSIVE > FAST > MODERATE), then by score
        tier_order = {
            VelocityTier.EXPLOSIVE: 0,
            VelocityTier.FAST: 1,
            VelocityTier.MODERATE: 2,
        }
        opportunities.sort(
            key=lambda o: (tier_order.get(o.tier, 3), -o.momentum_score)
        )

        # Limit to max entries per cycle
        opportunities = opportunities[: self.MAX_ENTRIES_PER_CYCLE]

        self._stats["total_opportunities"] += len(opportunities)

        if opportunities:
            logger.info(
                f"Velocity scan: {len(opportunities)} fast movers found",
                scanned=markets_scanned,
                explosive=sum(1 for o in opportunities if o.tier == VelocityTier.EXPLOSIVE),
                fast=sum(1 for o in opportunities if o.tier == VelocityTier.FAST),
                moderate=sum(1 for o in opportunities if o.tier == VelocityTier.MODERATE),
            )

        return VelocityScanResult(
            markets_scanned=markets_scanned,
            fast_movers_found=len(opportunities),
            opportunities=opportunities,
            errors=errors,
        )

    def _generate_entry_signal(
        self,
        ticker: str,
        tier: VelocityTier,
        velocity: Dict[str, Any],
        meta: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """
        Generate an entry signal based on velocity direction and tier.

        - Upward momentum → buy YES (ride the trend)
        - Downward momentum → buy NO (ride the trend)
        - Position size adapts to tier (smaller for explosive, larger for moderate)
        """
        direction = velocity["direction"]
        current_price = velocity["current_price"]

        # Determine side
        if direction == "up":
            side = "yes"
            entry_price = current_price  # buy YES at current price
        else:
            side = "no"
            entry_price = 100 - current_price  # NO cost = 100 - YES price

        # Skip if price is at extremes (can't profit)
        if entry_price <= 2 or entry_price >= 98:
            return None

        # Position size by tier
        size_range = self.TIER_SIZES.get(tier, (10, 20))
        # Higher momentum score = closer to max
        score = self._calculate_momentum_score(
            velocity["velocity_30min"],
            velocity["acceleration"],
            velocity["volume_ratio"],
        )
        quantity = int(size_range[0] + (size_range[1] - size_range[0]) * score)
        quantity = max(1, quantity)

        return {
            "side": side,
            "quantity": quantity,
            "price": entry_price,
        }

    def _already_positioned(self, ticker: str) -> bool:
        """
        Check if we already have an open position or recent trade on this ticker.

        Checks both PositionDB (qty > 0) and TradeDB (buy in last 30 min)
        to prevent duplicate entries.
        """
        try:
            with next(get_db_session()) as session:
                # Check open position
                position = (
                    session.query(PositionDB)
                    .filter(
                        PositionDB.ticker == ticker,
                        PositionDB.quantity > 0,
                    )
                    .first()
                )
                if position:
                    return True

                # Check recent trades (last 30 min)
                cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
                recent_trade = (
                    session.query(TradeDB)
                    .filter(
                        TradeDB.ticker == ticker,
                        TradeDB.action == "buy",
                        TradeDB.timestamp >= cutoff,
                    )
                    .first()
                )
                return recent_trade is not None

        except Exception as e:
            logger.debug(f"Position check failed for {ticker}: {e}")
            return True  # Err on side of caution

    def get_stats(self) -> Dict[str, Any]:
        """Get scanner statistics."""
        return dict(self._stats)


# ─── Global instance ────────────────────────────────────────────────────

_velocity_scanner: Optional[VelocityScanner] = None


def get_velocity_scanner(paper_trading: bool = True) -> VelocityScanner:
    """Get or create the global velocity scanner instance."""
    global _velocity_scanner
    if _velocity_scanner is None:
        _velocity_scanner = VelocityScanner(paper_trading=paper_trading)
    return _velocity_scanner
