"""
Edge calculator for trading opportunities.

Compares model probability forecasts to market prices to identify
profitable trading opportunities. Accounts for fees, liquidity,
and cross-platform arbitrage with Polymarket.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from src.analysis.forecaster import Forecast, get_default_forecaster
from src.data.database import get_db_session
from src.data.models import ForecastDB, MarketDB, PolymarketPriceDB, PriceDB
from src.utils.logging import logger


class OpportunityType(Enum):
    """Types of trading opportunities."""

    EDGE = "edge"  # Model disagrees with market
    ARBITRAGE = "arbitrage"  # Cross-platform price difference
    COMBINED = "combined"  # Both edge and arbitrage


@dataclass
class TradingOpportunity:
    """A potential trading opportunity."""

    ticker: str
    market_title: str
    opportunity_type: OpportunityType

    # Model vs Market
    model_probability: float  # Our forecast (0-1)
    market_probability: float  # Kalshi implied probability (0-1)
    edge: float  # model_probability - market_probability

    # Expected value
    expected_value: float  # EV per contract after fees
    ev_pct: float  # EV as percentage of risk

    # Trade recommendation
    side: str  # "yes" or "no"
    action: str  # "buy" or "sell"
    price: int  # Recommended entry price in cents

    # Market metrics
    volume: int  # 24h volume
    liquidity_score: float  # Liquidity indicator (0-1)
    opportunity_score: float  # edge * liquidity ranking

    # Polymarket comparison (if available)
    polymarket_price: Optional[float] = None
    polymarket_edge: Optional[float] = None
    is_arbitrage: bool = False

    # Metadata
    confidence: float = 0.5
    timestamp: datetime = datetime.now(timezone.utc)
    reasoning: Optional[str] = None


class EdgeCalculator:
    """
    Calculates trading edge and identifies opportunities.

    Compares model forecasts to market prices, accounting for:
    - Kalshi fees (2% winner fee)
    - Bid/ask spread
    - Liquidity constraints
    - Cross-platform arbitrage with Polymarket
    """

    # Kalshi fee structure
    WINNER_FEE_RATE = 0.02  # 2% fee on winning trades
    MAKER_REBATE = 0.0  # No maker rebate currently

    # Thresholds - AGGRESSIVE for high volume
    MIN_EDGE_THRESHOLD = 0.02  # Minimum 2% edge (was 3%)
    MIN_ARBITRAGE_THRESHOLD = 0.01  # Minimum 1% for arbitrage
    MIN_LIQUIDITY_SCORE = 0.1  # Minimum liquidity to consider

    # Dynamic minimum edge by price tier
    MIN_EDGE_BY_TIER = {
        "longshot": 0.015,  # 1.5% — fees are tiny relative to payout
        "low": 0.02,        # 2% — standard
        "mid": 0.025,       # 2.5% — spread matters more
        "high": 0.035,      # 3.5% — fees are brutal, need bigger edge
    }

    # Volume normalization (for liquidity score)
    HIGH_VOLUME_THRESHOLD = 10000  # $10k daily volume = max liquidity

    def __init__(
        self,
        min_edge: float = 0.02,
        min_arbitrage: float = 0.01,
        fee_rate: float = 0.02,
    ) -> None:
        """
        Initialize edge calculator.

        Args:
            min_edge: Minimum edge threshold for opportunities.
            min_arbitrage: Minimum edge for cross-platform arbitrage.
            fee_rate: Kalshi winner fee rate.
        """
        self.min_edge = min_edge
        self.min_arbitrage = min_arbitrage
        self.fee_rate = fee_rate

        logger.info(
            "Edge calculator initialized",
            min_edge=min_edge,
            min_arbitrage=min_arbitrage,
            fee_rate=fee_rate,
        )

    def price_to_probability(self, price_cents: int) -> float:
        """
        Convert market price in cents to implied probability.

        Args:
            price_cents: Price in cents (1-99).

        Returns:
            Implied probability (0.01 to 0.99).
        """
        return price_cents / 100.0

    def probability_to_price(self, probability: float) -> int:
        """
        Convert probability to price in cents.

        Args:
            probability: Probability (0.0 to 1.0).

        Returns:
            Price in cents (1-99).
        """
        return max(1, min(99, int(probability * 100)))

    def calculate_expected_value(
        self,
        model_prob: float,
        entry_price: int,
        side: str,
    ) -> tuple[float, float]:
        """
        Calculate expected value per contract.

        Args:
            model_prob: Model's probability for YES outcome.
            entry_price: Entry price in cents (YES price for YES, NO price for NO).
            side: "yes" or "no".

        Returns:
            Tuple of (EV in cents, EV as percentage of risk).
        """
        if side == "yes":
            # Buying YES: pay entry_price, win 100 if YES
            win_amount = 100 - entry_price
            win_after_fee = win_amount * (1 - self.fee_rate)
            lose_amount = entry_price

            ev = (model_prob * win_after_fee) - ((1 - model_prob) * lose_amount)
            risk = entry_price
        else:
            # Buying NO: pay entry_price (= no_ask), win 100 if NO
            no_price = entry_price  # Already the NO cost
            win_amount = 100 - no_price
            win_after_fee = win_amount * (1 - self.fee_rate)
            lose_amount = no_price
            no_prob = 1 - model_prob

            ev = (no_prob * win_after_fee) - ((1 - no_prob) * lose_amount)
            risk = no_price

        ev_pct = ev / risk if risk > 0 else 0.0

        return ev, ev_pct

    def calculate_edge(self, model_prob: float, market_prob: float) -> float:
        """
        Calculate edge (model vs market disagreement).

        Positive edge means we think YES is underpriced.
        Negative edge means we think NO is underpriced (YES overpriced).

        Args:
            model_prob: Model's YES probability.
            market_prob: Market's implied YES probability.

        Returns:
            Edge as decimal (-1.0 to 1.0).
        """
        return model_prob - market_prob

    def calculate_liquidity_score(self, volume: int, spread: int) -> float:
        """
        Calculate liquidity score based on volume and spread.

        Args:
            volume: 24-hour volume in contracts or dollars.
            spread: Bid/ask spread in cents.

        Returns:
            Liquidity score (0.0 to 1.0).
        """
        # Volume component: 0-0.6 based on volume
        volume_score = min(1.0, volume / self.HIGH_VOLUME_THRESHOLD) * 0.6

        # Spread component: 0-0.4 based on tight spread
        # Spread of 1 cent = perfect, 10+ cents = poor
        spread_score = max(0.0, 1.0 - (spread - 1) / 9) * 0.4

        return volume_score + spread_score

    def get_min_edge(self, entry_price: int) -> float:
        """Get minimum required edge based on entry price tier."""
        if entry_price <= 15:
            return self.MIN_EDGE_BY_TIER["longshot"]
        elif entry_price <= 35:
            return self.MIN_EDGE_BY_TIER["low"]
        elif entry_price <= 65:
            return self.MIN_EDGE_BY_TIER["mid"]
        else:
            return self.MIN_EDGE_BY_TIER["high"]

    def calculate_true_edge(
        self,
        model_prob: float,
        yes_bid: int,
        yes_ask: int,
        no_bid: Optional[int] = None,
        no_ask: Optional[int] = None,
    ) -> Dict[str, float]:
        """
        Calculate fee-and-spread-adjusted edge for both sides.

        Returns dict with:
            yes_true_edge: Edge after spread + fees for YES side
            no_true_edge: Edge after spread + fees for NO side
            spread_cost: Spread as probability points
            best_side: "yes", "no", or None
        """
        spread = yes_ask - yes_bid
        spread_cost = spread / 100.0

        # YES side: we buy at ask
        yes_entry_prob = yes_ask / 100.0
        yes_gross_edge = model_prob - yes_entry_prob
        yes_fee_cost = self.fee_rate * (1 - yes_entry_prob)  # Fee on winnings
        yes_true_edge = yes_gross_edge - yes_fee_cost

        # NO side: we buy at no_ask (or estimate)
        if no_ask and no_ask > 0:
            no_entry_price = no_ask
        elif yes_bid and yes_bid > 0:
            no_entry_price = 100 - yes_bid
        else:
            no_entry_price = 100 - yes_ask  # Worst case

        no_entry_prob = no_entry_price / 100.0
        no_model_prob = 1 - model_prob
        no_gross_edge = no_model_prob - no_entry_prob
        no_fee_cost = self.fee_rate * (1 - no_entry_prob)
        no_true_edge = no_gross_edge - no_fee_cost

        # Determine best side using tier-aware min edge
        yes_min_edge = self.get_min_edge(yes_ask)
        no_min_edge = self.get_min_edge(no_entry_price)

        best_side: Optional[str] = None
        if yes_true_edge >= yes_min_edge:
            best_side = "yes"
        if no_true_edge >= no_min_edge and (best_side is None or no_true_edge > yes_true_edge):
            best_side = "no"

        return {
            "yes_true_edge": yes_true_edge,
            "no_true_edge": no_true_edge,
            "spread_cost": spread_cost,
            "best_side": best_side,
        }

    def _get_latest_price(self, ticker: str) -> Optional[Dict[str, Any]]:
        """Get latest price data for a market."""
        with next(get_db_session()) as session:
            price = (
                session.query(PriceDB)
                .filter(PriceDB.ticker == ticker)
                .order_by(PriceDB.timestamp.desc())
                .first()
            )

            if not price:
                return None

            return {
                "yes_bid": price.yes_bid,
                "yes_ask": price.yes_ask,
                "no_bid": price.no_bid,
                "no_ask": price.no_ask,
                "volume": price.volume or 0,
                "timestamp": price.timestamp,
            }

    def _get_polymarket_price(self, ticker: str) -> Optional[Dict[str, Any]]:
        """Get latest Polymarket price for comparison."""
        with next(get_db_session()) as session:
            # Look for matched Polymarket market
            poly_price = (
                session.query(PolymarketPriceDB)
                .filter(PolymarketPriceDB.ticker == ticker)
                .order_by(PolymarketPriceDB.timestamp.desc())
                .first()
            )

            if not poly_price:
                return None

            return {
                "yes_price": poly_price.yes_price,
                "no_price": poly_price.no_price,
                "volume": poly_price.volume or 0,
                "timestamp": poly_price.timestamp,
            }

    def _get_latest_forecast(self, ticker: str) -> Optional[Forecast]:
        """Get latest forecast for a market."""
        with next(get_db_session()) as session:
            forecast_record = (
                session.query(ForecastDB)
                .filter(ForecastDB.ticker == ticker)
                .order_by(ForecastDB.created_at.desc())
                .first()
            )

            if not forecast_record:
                return None

            # Handle SQLAlchemy column type - cast to datetime
            ts_value = forecast_record.timestamp
            forecast_timestamp: datetime = (
                ts_value if isinstance(ts_value, datetime) else datetime.now(timezone.utc)
            )

            return Forecast(
                market_ticker=str(forecast_record.ticker),
                probability_yes=float(forecast_record.probability or 0.5),
                probability_no=1.0 - float(forecast_record.probability or 0.5),
                confidence=float(forecast_record.confidence or 0.5),
                factors={},
                method=str(forecast_record.method),
                timestamp=forecast_timestamp,
            )

    def evaluate_market(
        self,
        ticker: str,
        market_title: str = "",
        forecast: Optional[Forecast] = None,
    ) -> Optional[TradingOpportunity]:
        """
        Evaluate a single market for trading opportunity.

        Args:
            ticker: Market ticker.
            market_title: Market title/question.
            forecast: Pre-computed forecast (optional).

        Returns:
            TradingOpportunity if edge exceeds threshold, None otherwise.
        """
        # Get latest price
        price_data = self._get_latest_price(ticker)
        if not price_data:
            logger.debug("No price data for market", ticker=ticker)
            return None

        yes_ask = price_data["yes_ask"]
        yes_bid = price_data["yes_bid"]
        no_bid = price_data.get("no_bid")
        no_ask = price_data.get("no_ask")

        if yes_ask is None or yes_bid is None:
            return None

        # Get or generate forecast
        if forecast is None:
            forecast = self._get_latest_forecast(ticker)

        if forecast is None:
            # Generate new forecast
            forecaster = get_default_forecaster()
            # Use mid price as current price
            mid_price = (yes_bid + yes_ask) // 2
            forecast = forecaster.forecast(
                market_ticker=ticker,
                market_title=market_title,
                current_price=mid_price,
            )

        # Calculate market probability (use ask for buying)
        market_prob = self.price_to_probability(yes_ask)
        model_prob = forecast.probability_yes

        # Calculate edge
        edge = self.calculate_edge(model_prob, market_prob)

        # Use true edge (spread + fee adjusted) to pick the best side
        true_edge_result = self.calculate_true_edge(
            model_prob=model_prob,
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
        )

        best_side = true_edge_result["best_side"]

        # Determine side and action using spread-aware edge
        if best_side == "yes":
            side = "yes"
            action = "buy"
            entry_price = yes_ask
        elif best_side == "no":
            side = "no"
            action = "buy"
            # Use actual NO ask price, not derived from YES
            if no_ask and no_ask > 0:
                entry_price = no_ask
            elif yes_bid and yes_bid > 0:
                entry_price = 100 - yes_bid  # Conservative fallback
            else:
                return None  # Can't determine NO price
        else:
            # No profitable side after spread + fees
            return None

        # Calculate expected value
        ev, ev_pct = self.calculate_expected_value(model_prob, entry_price, side)

        # Skip if EV is negative after fees
        if ev <= 0:
            return None

        # Calculate liquidity score
        spread = yes_ask - yes_bid
        volume = price_data.get("volume", 0) or 0
        liquidity_score = self.calculate_liquidity_score(volume, spread)

        # Check Polymarket for arbitrage
        poly_data = self._get_polymarket_price(ticker)
        polymarket_price = None
        polymarket_edge = None
        is_arbitrage = False

        if poly_data and poly_data.get("yes_price") is not None:
            polymarket_price = poly_data["yes_price"]
            # Calculate edge vs Polymarket
            kalshi_mid = (yes_bid + yes_ask) / 200.0  # Convert to 0-1
            polymarket_edge = polymarket_price - kalshi_mid

            # Flag arbitrage if significant difference
            if abs(polymarket_edge) >= self.min_arbitrage:
                is_arbitrage = True

        # Determine opportunity type
        if is_arbitrage and abs(edge) >= self.min_edge:
            opp_type = OpportunityType.COMBINED
        elif is_arbitrage:
            opp_type = OpportunityType.ARBITRAGE
        else:
            opp_type = OpportunityType.EDGE

        # Calculate opportunity score (directional edge magnitude * liquidity).
        # edge is YES-oriented (positive=YES underpriced, negative=NO underpriced).
        # abs() is correct here: it gives the edge magnitude for the chosen side.
        opportunity_score = abs(edge) * liquidity_score

        # Build reasoning
        reasoning_parts = [f"Model: {model_prob:.1%} vs Market: {market_prob:.1%}"]
        if edge > 0:
            reasoning_parts.append(f"YES underpriced by {edge:.1%}")
        else:
            reasoning_parts.append(f"NO underpriced by {-edge:.1%}")
        if is_arbitrage:
            reasoning_parts.append(
                f"Polymarket: {polymarket_price:.1%} (diff: {polymarket_edge:+.1%})"
            )
        reasoning = ". ".join(reasoning_parts)

        return TradingOpportunity(
            ticker=ticker,
            market_title=market_title,
            opportunity_type=opp_type,
            model_probability=model_prob,
            market_probability=market_prob,
            edge=edge,
            expected_value=ev,
            ev_pct=ev_pct,
            side=side,
            action=action,
            price=entry_price,
            volume=volume,
            liquidity_score=liquidity_score,
            opportunity_score=opportunity_score,
            polymarket_price=polymarket_price,
            polymarket_edge=polymarket_edge,
            is_arbitrage=is_arbitrage,
            confidence=forecast.confidence,
            timestamp=datetime.now(timezone.utc),
            reasoning=reasoning,
        )

    def scan_markets(
        self,
        category: Optional[str] = None,
        limit: int = 100,
    ) -> List[TradingOpportunity]:
        """
        Scan markets for trading opportunities.

        Args:
            category: Optional category filter.
            limit: Maximum markets to scan.

        Returns:
            List of opportunities sorted by opportunity_score.
        """
        opportunities = []

        with next(get_db_session()) as session:
            # Get active markets
            query = session.query(MarketDB).filter(MarketDB.status == "active")

            if category:
                query = query.filter(MarketDB.category == category)

            markets = query.limit(limit).all()

        for market in markets:
            try:
                opportunity = self.evaluate_market(
                    ticker=str(market.ticker),
                    market_title=str(market.title),
                )
                if opportunity:
                    opportunities.append(opportunity)
            except Exception as e:
                logger.warning(
                    "Failed to evaluate market",
                    ticker=market.ticker,
                    error=str(e),
                )

        # Sort by opportunity score (highest first)
        opportunities.sort(key=lambda x: x.opportunity_score, reverse=True)

        logger.info(
            "Market scan complete",
            markets_scanned=len(markets),
            opportunities_found=len(opportunities),
        )

        return opportunities

    def get_arbitrage_opportunities(
        self, limit: int = 50
    ) -> List[TradingOpportunity]:
        """
        Find cross-platform arbitrage opportunities.

        Looks for markets where Kalshi and Polymarket prices differ
        significantly.

        Args:
            limit: Maximum opportunities to return.

        Returns:
            List of arbitrage opportunities.
        """
        opportunities = []

        with next(get_db_session()) as session:
            # Get markets with Polymarket matches
            poly_markets = (
                session.query(PolymarketPriceDB)
                .filter(PolymarketPriceDB.ticker.isnot(None))
                .order_by(PolymarketPriceDB.timestamp.desc())
                .limit(limit * 2)  # Get more to filter
                .all()
            )

            # Dedupe by ticker
            seen_tickers: set[str] = set()
            for poly in poly_markets:
                ticker = str(poly.ticker) if poly.ticker else None
                if not ticker or ticker in seen_tickers:
                    continue
                seen_tickers.add(ticker)

                opportunity = self.evaluate_market(ticker=ticker)
                if opportunity and opportunity.is_arbitrage:
                    opportunities.append(opportunity)

        # Sort by arbitrage edge
        opportunities.sort(
            key=lambda x: abs(x.polymarket_edge or 0), reverse=True
        )

        return opportunities[:limit]

    def get_top_opportunities(
        self,
        min_confidence: float = 0.5,
        max_results: int = 10,
    ) -> List[TradingOpportunity]:
        """
        Get top trading opportunities.

        Args:
            min_confidence: Minimum forecast confidence.
            max_results: Maximum results to return.

        Returns:
            Top opportunities by opportunity_score.
        """
        all_opportunities = self.scan_markets()

        # Filter by confidence
        filtered = [
            opp for opp in all_opportunities if opp.confidence >= min_confidence
        ]

        return filtered[:max_results]

    def calculate_portfolio_edge(
        self, opportunities: List[TradingOpportunity]
    ) -> Dict[str, Any]:
        """
        Calculate aggregate portfolio metrics for opportunities.

        Args:
            opportunities: List of trading opportunities.

        Returns:
            Portfolio-level metrics.
        """
        if not opportunities:
            return {
                "total_opportunities": 0,
                "avg_edge": 0.0,
                "avg_ev_pct": 0.0,
                "total_ev": 0.0,
                "edge_opportunities": 0,
                "arbitrage_opportunities": 0,
                "combined_opportunities": 0,
            }

        total_edge = sum(abs(o.edge) for o in opportunities)
        total_ev = sum(o.expected_value for o in opportunities)
        total_ev_pct = sum(o.ev_pct for o in opportunities)

        edge_count = sum(
            1 for o in opportunities if o.opportunity_type == OpportunityType.EDGE
        )
        arb_count = sum(
            1 for o in opportunities if o.opportunity_type == OpportunityType.ARBITRAGE
        )
        combined_count = sum(
            1 for o in opportunities if o.opportunity_type == OpportunityType.COMBINED
        )

        return {
            "total_opportunities": len(opportunities),
            "avg_edge": total_edge / len(opportunities),
            "avg_ev_pct": total_ev_pct / len(opportunities),
            "total_ev": total_ev,
            "edge_opportunities": edge_count,
            "arbitrage_opportunities": arb_count,
            "combined_opportunities": combined_count,
            "top_opportunity": opportunities[0].ticker if opportunities else None,
            "top_edge": opportunities[0].edge if opportunities else 0.0,
        }


# Global instance
_calculator: Optional[EdgeCalculator] = None


def get_edge_calculator() -> EdgeCalculator:
    """Get or create the global edge calculator instance."""
    global _calculator
    if _calculator is None:
        _calculator = EdgeCalculator()
    return _calculator


def scan_for_opportunities(
    category: Optional[str] = None, limit: int = 100
) -> List[TradingOpportunity]:
    """Convenience function to scan markets for opportunities."""
    return get_edge_calculator().scan_markets(category=category, limit=limit)


def evaluate_market(ticker: str) -> Optional[TradingOpportunity]:
    """Convenience function to evaluate a single market."""
    return get_edge_calculator().evaluate_market(ticker=ticker)
