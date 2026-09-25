"""
Position sizing using Kelly Criterion.

Calculates optimal bet sizes to maximize long-term growth while
managing risk. Supports fractional Kelly for more conservative sizing.

Bankroll configuration loaded from config/bankroll.json
"""
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.data.database import get_db_session
from src.data.models import PortfolioSnapshotDB, PositionDB
from src.utils.logging import logger


# Bankroll configuration path
BANKROLL_CONFIG_PATH = Path("config/bankroll.json")


def load_bankroll_config() -> Dict[str, Any]:
    """Load bankroll configuration from file."""
    if BANKROLL_CONFIG_PATH.exists():
        try:
            with open(BANKROLL_CONFIG_PATH) as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load bankroll config: {e}")
    return {
        "bankroll": 1000.0,
        "mode": "paper",
        "max_single_bet_pct": 0.05,
        "max_position": 200.0,
    }


def get_bankroll() -> float:
    """Get current bankroll from config."""
    config = load_bankroll_config()
    return config.get("bankroll", 1000.0)


def save_bankroll_config(config: Dict[str, Any]) -> bool:
    """Save bankroll configuration to file."""
    try:
        BANKROLL_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(BANKROLL_CONFIG_PATH, "w") as f:
            json.dump(config, f, indent=2)
        return True
    except Exception as e:
        logger.error(f"Failed to save bankroll config: {e}")
        return False


@dataclass
class PositionSize:
    """Recommended position size for a trade."""

    ticker: str
    side: str  # "yes" or "no"

    # Position sizing
    kelly_fraction: float  # Full Kelly fraction (0-1)
    recommended_fraction: float  # Adjusted Kelly (e.g., half-Kelly)
    recommended_contracts: int  # Number of contracts to buy
    recommended_dollars: float  # Dollar amount to risk

    # Constraints applied
    max_position_pct: float  # Max position as % of bankroll
    existing_position: int  # Existing contracts in this market
    capped_by_max_position: bool  # True if capped by max position
    capped_by_bankroll: bool  # True if capped by available funds

    # Input parameters
    edge: float  # Expected edge (model - market probability)
    probability: float  # Estimated win probability
    odds: float  # Payout odds (decimal)
    bankroll: float  # Current bankroll

    # Metadata
    confidence: float = 0.5
    timestamp: datetime = datetime.now(timezone.utc)


class PositionSizer:
    """
    Kelly Criterion position sizing for prediction market trading.

    The Kelly Criterion formula:
        f* = (bp - q) / b
    where:
        f* = fraction of bankroll to bet
        b = odds received on bet (decimal odds - 1)
        p = probability of winning
        q = probability of losing (1 - p)

    For binary prediction markets where you pay X cents to win 100:
        b = (100 - X) / X  (the odds)
        f* = (bp - q) / b = (b * p - (1-p)) / b

    This implementation includes:
    - Fractional Kelly (default half-Kelly)
    - Maximum position limits
    - Account for existing positions
    """

    # Default constraints — position/exposure limits disabled for testing phase.
    # Kelly fraction still enforces mathematical sizing; guardrails come later.
    DEFAULT_KELLY_FRACTION = 0.5  # Half-Kelly by default (more conservative)
    MAX_POSITION_PCT = 0.15  # 15% max per single market position
    MAX_TOTAL_EXPOSURE_PCT = 0.75  # 75% max total capital at risk
    MIN_POSITION_DOLLARS = 1.0  # Minimum position size

    def __init__(
        self,
        kelly_fraction: float = 0.5,
        max_position_pct: float = 1.00,
        max_total_exposure_pct: float = 1.00,
        min_position_dollars: float = 1.0,
        kalshi_client: Optional[Any] = None,
        paper_trading: bool = True,
    ) -> None:
        """
        Initialize position sizer.

        Args:
            kelly_fraction: Fraction of Kelly to use (0.5 = half-Kelly).
            max_position_pct: Maximum position as % of bankroll.
            max_total_exposure_pct: Maximum total exposure as % of bankroll.
            min_position_dollars: Minimum position size in dollars.
            kalshi_client: Optional Kalshi API client for live balance fetching.
            paper_trading: Whether running in paper trading mode.
        """
        self.kelly_fraction = kelly_fraction
        self.max_position_pct = max_position_pct
        self.max_total_exposure_pct = max_total_exposure_pct
        self.min_position_dollars = min_position_dollars
        self.kalshi_client = kalshi_client
        self.paper_trading = paper_trading
        self._config_bankroll = load_bankroll_config().get("bankroll", 1000.0)
        self._allocation_lock = __import__("threading").Lock()

        logger.info(
            "Position sizer initialized",
            kelly_fraction=kelly_fraction,
            max_position_pct=max_position_pct,
            max_total_exposure_pct=max_total_exposure_pct,
        )

    def calculate_kelly_fraction(
        self,
        win_probability: float,
        entry_price: int,
        side: str,
    ) -> float:
        """
        Calculate the Kelly Criterion fraction.

        Args:
            win_probability: Probability of winning (0-1).
            entry_price: Entry price in cents (1-99).
            side: "yes" or "no".

        Returns:
            Kelly fraction (0 to 1+, capped at 1).
        """
        # For YES side at price X cents:
        # Win: 100 - X (profit)
        # Lose: X (loss)
        # Odds b = (100 - X) / X

        # For NO side (buying NO at implied price 100 - X):
        # Win: X (profit)
        # Lose: 100 - X (loss)

        if side == "yes":
            # Buying YES at entry_price
            profit_if_win = 100 - entry_price
            loss_if_lose = entry_price
            p = win_probability
        else:
            # Buying NO (implied price is 100 - yes_price)
            no_price = 100 - entry_price
            profit_if_win = 100 - no_price  # = entry_price
            loss_if_lose = no_price
            p = 1 - win_probability  # P(NO) = 1 - P(YES)

        if loss_if_lose == 0:
            return 0.0

        # Odds (decimal - 1): how much we win per dollar risked
        b = profit_if_win / loss_if_lose
        q = 1 - p

        # Kelly formula: f* = (bp - q) / b
        kelly = (b * p - q) / b if b > 0 else 0.0

        # Clamp to valid range (0 to 1)
        # Negative Kelly means don't bet
        return max(0.0, min(1.0, kelly))

    def calculate_kelly_from_edge(
        self,
        edge: float,
        market_probability: float,
    ) -> float:
        """
        Calculate Kelly fraction from edge and market probability.

        Simplified formula when you have edge directly:
            f* = edge / (1 - market_prob)  for YES
            f* = -edge / market_prob  for NO

        Args:
            edge: Model probability - Market probability.
            market_probability: Market implied probability.

        Returns:
            Kelly fraction.
        """
        if edge > 0:
            # Positive edge = buy YES
            # Simplified: f* = edge / odds_against
            if market_probability >= 1.0:
                return 0.0
            kelly = edge / (1 - market_probability)
        else:
            # Negative edge = buy NO
            if market_probability <= 0.0:
                return 0.0
            kelly = (-edge) / market_probability

        return max(0.0, min(1.0, kelly))

    def get_current_bankroll(self) -> float:
        """
        Get current bankroll.

        Priority:
        1. Kalshi API account balance (if live trading and client available)
        2. Latest portfolio snapshot from database
        3. Config file default
        """
        # Try Kalshi API first (live trading)
        if not self.paper_trading and self.kalshi_client is not None:
            try:
                balance = self._fetch_kalshi_balance()
                if balance is not None and balance > 0:
                    return balance
            except Exception as e:
                logger.warning(f"Failed to fetch Kalshi balance: {e}")

        # Fall back to database snapshot
        with next(get_db_session()) as session:
            snapshot = (
                session.query(PortfolioSnapshotDB)
                .order_by(PortfolioSnapshotDB.timestamp.desc())
                .first()
            )

            if snapshot and snapshot.balance:
                return float(snapshot.balance)

        # Last resort: config
        return self._config_bankroll

    def _fetch_kalshi_balance(self) -> Optional[float]:
        """Fetch account balance from Kalshi API."""
        import asyncio
        try:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop and loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future = pool.submit(
                        asyncio.run,
                        self.kalshi_client.get_balance(),
                    )
                    balance_info = future.result(timeout=10)
            else:
                balance_info = asyncio.run(self.kalshi_client.get_balance())

            if balance_info and hasattr(balance_info, "balance"):
                return float(balance_info.balance)
            return None
        except Exception:
            return None

    def get_existing_position(self, ticker: str) -> int:
        """Get existing position quantity for a market."""
        with next(get_db_session()) as session:
            position = session.query(PositionDB).filter_by(ticker=ticker).first()

            if position:
                return int(position.quantity)

            return 0

    def get_total_exposure(self) -> float:
        """Get total portfolio exposure at CURRENT market value."""
        with next(get_db_session()) as session:
            positions = session.query(PositionDB).filter(
                PositionDB.quantity > 0
            ).all()

            total = 0.0
            for pos in positions:
                if pos.quantity and pos.quantity > 0:
                    # Use market price if available, fall back to entry price
                    price = float(pos.market_price or pos.average_price or 50)
                    total += float(pos.quantity) * price / 100.0

            return total

    def calculate_position_size(
        self,
        ticker: str,
        win_probability: float,
        entry_price: int,
        side: str,
        edge: float,
        confidence: float = 0.5,
        bankroll: Optional[float] = None,
    ) -> PositionSize:
        """
        Calculate recommended position size (thread-safe).

        Args:
            ticker: Market ticker.
            win_probability: Probability of winning (0-1).
            entry_price: Entry price in cents.
            side: "yes" or "no".
            edge: Expected edge (model - market).
            confidence: Forecast confidence (0-1).
            bankroll: Override bankroll (uses DB if None).

        Returns:
            PositionSize with recommendation.
        """
        with self._allocation_lock:
            return self._calculate_position_size_unsafe(
                ticker, win_probability, entry_price, side, edge, confidence, bankroll,
            )

    def _calculate_position_size_unsafe(
        self,
        ticker: str,
        win_probability: float,
        entry_price: int,
        side: str,
        edge: float,
        confidence: float = 0.5,
        bankroll: Optional[float] = None,
    ) -> PositionSize:
        """Internal position size calculation (NOT thread-safe)."""
        # Get bankroll
        if bankroll is None:
            bankroll = self.get_current_bankroll()

        # Calculate full Kelly
        kelly_full = self.calculate_kelly_fraction(win_probability, entry_price, side)

        # Apply fractional Kelly
        kelly_adjusted = kelly_full * self.kelly_fraction

        # Apply confidence adjustment
        # Lower confidence = more conservative
        kelly_adjusted *= confidence

        # Calculate position in dollars
        position_dollars = bankroll * kelly_adjusted

        # Check max position constraint
        max_position_dollars = bankroll * self.max_position_pct
        capped_by_max_position = position_dollars > max_position_dollars

        if capped_by_max_position:
            position_dollars = max_position_dollars

        # Account for existing position
        existing_qty = self.get_existing_position(ticker)
        existing_value = existing_qty * entry_price / 100.0

        # Reduce new position by existing exposure
        available_for_position = max_position_dollars - existing_value
        capped_by_bankroll = position_dollars > available_for_position

        if available_for_position < self.min_position_dollars:
            # Already at max position
            position_dollars = 0.0
        elif capped_by_bankroll:
            position_dollars = available_for_position

        # Check total exposure constraint
        total_exposure = self.get_total_exposure()
        max_total_exposure = bankroll * self.max_total_exposure_pct
        remaining_exposure = max_total_exposure - total_exposure

        if position_dollars > remaining_exposure:
            position_dollars = max(0.0, remaining_exposure)
            capped_by_bankroll = True

        # Convert to contracts
        # Each contract costs entry_price cents
        if side == "yes":
            contract_price = entry_price / 100.0  # Convert cents to dollars
        else:
            contract_price = (100 - entry_price) / 100.0

        if contract_price > 0:
            num_contracts = int(position_dollars / contract_price)
        else:
            num_contracts = 0

        # Calculate odds for reference
        if side == "yes":
            profit_if_win = 100 - entry_price
            odds = profit_if_win / entry_price if entry_price > 0 else 0
        else:
            no_price = 100 - entry_price
            odds = (100 - no_price) / no_price if no_price > 0 else 0

        return PositionSize(
            ticker=ticker,
            side=side,
            kelly_fraction=kelly_full,
            recommended_fraction=kelly_adjusted,
            recommended_contracts=num_contracts,
            recommended_dollars=position_dollars,
            max_position_pct=self.max_position_pct,
            existing_position=existing_qty,
            capped_by_max_position=capped_by_max_position,
            capped_by_bankroll=capped_by_bankroll,
            edge=edge,
            probability=win_probability,
            odds=odds,
            bankroll=bankroll,
            confidence=confidence,
            timestamp=datetime.now(timezone.utc),
        )

    def calculate_batch(
        self,
        opportunities: List[Dict[str, Any]],
        bankroll: Optional[float] = None,
    ) -> List[PositionSize]:
        """
        Calculate position sizes for multiple opportunities (thread-safe).

        Args:
            opportunities: List of dicts with ticker, probability, price, side, edge.
            bankroll: Override bankroll.

        Returns:
            List of PositionSize recommendations.
        """
        with self._allocation_lock:
            return self._calculate_batch_unsafe(opportunities, bankroll)

    def _calculate_batch_unsafe(
        self,
        opportunities: List[Dict[str, Any]],
        bankroll: Optional[float] = None,
    ) -> List[PositionSize]:
        """Internal batch calculation (NOT thread-safe)."""
        if bankroll is None:
            bankroll = self.get_current_bankroll()

        results = []
        total_allocated = 0.0

        for opp in opportunities:
            try:
                # Reduce effective bankroll by previous allocations
                effective_bankroll = bankroll - total_allocated

                if effective_bankroll <= 0:
                    break

                size = self._calculate_position_size_unsafe(
                    ticker=opp["ticker"],
                    win_probability=opp["probability"],
                    entry_price=opp["price"],
                    side=opp["side"],
                    edge=opp["edge"],
                    confidence=opp.get("confidence", 0.5),
                    bankroll=effective_bankroll,
                )

                if size.recommended_contracts > 0:
                    results.append(size)
                    total_allocated += size.recommended_dollars

            except Exception as e:
                logger.warning(
                    "Failed to calculate position size",
                    ticker=opp.get("ticker"),
                    error=str(e),
                )

        return results

    def get_sizing_stats(self) -> Dict[str, Any]:
        """Get position sizing statistics."""
        bankroll = self.get_current_bankroll()
        total_exposure = self.get_total_exposure()

        return {
            "current_bankroll": bankroll,
            "total_exposure": total_exposure,
            "exposure_pct": total_exposure / bankroll if bankroll > 0 else 0,
            "max_position_pct": self.max_position_pct,
            "max_total_exposure_pct": self.max_total_exposure_pct,
            "kelly_fraction": self.kelly_fraction,
            "remaining_capacity": max(
                0, bankroll * self.max_total_exposure_pct - total_exposure
            ),
        }


# Global instance
_sizer: Optional[PositionSizer] = None


def get_position_sizer() -> PositionSizer:
    """Get or create the global position sizer instance."""
    global _sizer
    if _sizer is None:
        _sizer = PositionSizer()
    return _sizer


def calculate_position(
    ticker: str,
    win_probability: float,
    entry_price: int,
    side: str,
    edge: float,
    confidence: float = 0.5,
) -> PositionSize:
    """Convenience function to calculate a single position size."""
    return get_position_sizer().calculate_position_size(
        ticker=ticker,
        win_probability=win_probability,
        entry_price=entry_price,
        side=side,
        edge=edge,
        confidence=confidence,
    )
