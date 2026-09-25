# Straddle Arbitrage Detection Logic

## Concept

A **straddle** buys both YES and NO on the same market. Since one side ALWAYS wins (pays $1.00), if you buy both for less than $1.00, you have a guaranteed profit.

```
Total Cost = YES_price + NO_price
Profit = $1.00 - Total Cost

If Total Cost < $1.00 → Guaranteed Profit!
```

---

## Why This Opportunity Exists

### Market Mechanics
1. Kalshi uses a **CLOB (Central Limit Order Book)** model
2. YES and NO are priced independently
3. Market makers may not always keep perfect parity
4. **During volatile periods**, spreads can widen

### When Opportunities Appear
- High volatility (crypto price swings)
- Low liquidity periods (late night, weekends)
- Right before market close (15-min markets)
- After sudden price movements

---

## Target Markets

### 15-Minute Crypto Markets (Best Opportunity)
| Ticker Pattern | Asset | Why Good |
|----------------|-------|----------|
| `KXBTC*` | Bitcoin | High volatility, frequent gaps |
| `KXETH*` | Ethereum | Similar to BTC |
| `KXSOL*` | Solana | Lower liquidity = more opportunities |

### Example Market
```
Market: "Will BTC be higher in 15 minutes?"
Ticker: KXBTC15MIN-28JAN25-H1530

YES Ask: $0.48 (48 cents)
NO Ask: $0.48 (48 cents)
------------------------
Total: $0.96

Profit: $1.00 - $0.96 = $0.04 (4.17%)
```

---

## Detection Algorithm

```python
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional
from decimal import Decimal

@dataclass
class StraddleOpportunity:
    ticker: str
    title: str
    yes_ask: float      # What we'd pay for YES
    no_ask: float       # What we'd pay for NO
    total_cost: float   # yes_ask + no_ask
    profit_dollars: float  # 1.0 - total_cost
    profit_pct: float   # profit_dollars / total_cost
    expires_at: datetime
    time_to_expiry_min: int
    yes_volume: int
    no_volume: int
    confidence: str     # "HIGH", "MEDIUM", "LOW"


class StraddleScanner:
    """
    Scans for straddle arbitrage opportunities.
    """

    # Minimum profit thresholds
    MIN_PROFIT_PCT = 0.03       # 3% minimum
    MIN_PROFIT_DOLLARS = 0.02   # 2 cents minimum per contract

    # Target market patterns
    TARGET_PATTERNS = [
        "KXBTC",   # Bitcoin
        "KXETH",   # Ethereum
        "KXSOL",   # Solana
    ]

    # Time filters
    MIN_TIME_TO_EXPIRY = 2      # At least 2 minutes left
    MAX_TIME_TO_EXPIRY = 60     # Within 60 minutes of expiry

    # Liquidity filters
    MIN_VOLUME = 10             # At least 10 contracts traded

    def scan(self) -> List[StraddleOpportunity]:
        """
        Scan all markets for straddle opportunities.

        Returns:
            List of opportunities sorted by profit_pct descending
        """
        opportunities = []

        for pattern in self.TARGET_PATTERNS:
            # Get active markets matching pattern
            markets = self._get_markets(pattern)

            for market in markets:
                opp = self._evaluate_market(market)
                if opp:
                    opportunities.append(opp)

        # Sort by profit percentage
        opportunities.sort(key=lambda x: x.profit_pct, reverse=True)

        return opportunities

    def _evaluate_market(self, market: dict) -> Optional[StraddleOpportunity]:
        """
        Evaluate a single market for straddle opportunity.
        """
        ticker = market['ticker']
        title = market['title']
        close_time = market['close_time']

        # Check time to expiry
        now = datetime.utcnow()
        time_to_expiry = (close_time - now).total_seconds() / 60

        if time_to_expiry < self.MIN_TIME_TO_EXPIRY:
            return None  # Too close to expiry (execution risk)

        if time_to_expiry > self.MAX_TIME_TO_EXPIRY:
            return None  # Too far out

        # Get order book
        yes_ask = market.get('yes_ask', 100) / 100  # Convert cents to dollars
        no_ask = market.get('no_ask', 100) / 100

        # Check for valid prices
        if yes_ask <= 0 or no_ask <= 0:
            return None

        if yes_ask >= 1 or no_ask >= 1:
            return None

        # Calculate straddle cost
        total_cost = yes_ask + no_ask

        if total_cost >= 1.0:
            return None  # No arbitrage opportunity

        profit_dollars = 1.0 - total_cost
        profit_pct = profit_dollars / total_cost

        # Check profit thresholds
        if profit_pct < self.MIN_PROFIT_PCT:
            return None

        if profit_dollars < self.MIN_PROFIT_DOLLARS:
            return None

        # Check volume/liquidity
        yes_volume = market.get('yes_volume', 0)
        no_volume = market.get('no_volume', 0)

        if yes_volume < self.MIN_VOLUME or no_volume < self.MIN_VOLUME:
            return None

        # Determine confidence level
        confidence = self._calculate_confidence(
            profit_pct=profit_pct,
            time_to_expiry=time_to_expiry,
            volume=min(yes_volume, no_volume)
        )

        return StraddleOpportunity(
            ticker=ticker,
            title=title,
            yes_ask=yes_ask,
            no_ask=no_ask,
            total_cost=total_cost,
            profit_dollars=profit_dollars,
            profit_pct=profit_pct,
            expires_at=close_time,
            time_to_expiry_min=int(time_to_expiry),
            yes_volume=yes_volume,
            no_volume=no_volume,
            confidence=confidence,
        )

    def _calculate_confidence(
        self,
        profit_pct: float,
        time_to_expiry: float,
        volume: int
    ) -> str:
        """
        Calculate confidence level for the opportunity.

        HIGH: Large profit, good time window, high volume
        MEDIUM: Decent profit, adequate time/volume
        LOW: Marginal profit or execution concerns
        """
        score = 0

        # Profit score (0-3)
        if profit_pct >= 0.05:
            score += 3  # 5%+ profit
        elif profit_pct >= 0.04:
            score += 2  # 4%+ profit
        else:
            score += 1  # 3%+ profit

        # Time score (0-2)
        if time_to_expiry >= 10:
            score += 2  # 10+ minutes
        elif time_to_expiry >= 5:
            score += 1  # 5-10 minutes

        # Volume score (0-2)
        if volume >= 100:
            score += 2  # High volume
        elif volume >= 50:
            score += 1  # Medium volume

        # Convert score to confidence
        if score >= 6:
            return "HIGH"
        elif score >= 4:
            return "MEDIUM"
        else:
            return "LOW"
```

---

## Execution Strategy

### Order Placement
```python
def execute_straddle(
    ticker: str,
    num_contracts: int,
    max_slippage: float = 0.01  # 1 cent max slippage
) -> dict:
    """
    Execute a straddle by placing YES and NO orders.

    IMPORTANT: Must execute both sides atomically or risk directional exposure.
    """
    result = {
        "success": False,
        "yes_order": None,
        "no_order": None,
        "total_cost": 0,
        "expected_profit": 0,
    }

    # Get current prices
    orderbook = get_orderbook(ticker)
    yes_ask = orderbook['yes_ask']
    no_ask = orderbook['no_ask']

    # Verify opportunity still exists
    total_cost = (yes_ask + no_ask) / 100
    if total_cost >= 0.99:  # Allow 1 cent buffer
        result["error"] = "Opportunity closed"
        return result

    # Place YES order first (market order)
    yes_order = place_order(
        ticker=ticker,
        side="yes",
        action="buy",
        quantity=num_contracts,
        order_type="market",
    )

    if not yes_order.get("filled"):
        # YES didn't fill - abort
        result["error"] = "YES order failed"
        return result

    result["yes_order"] = yes_order

    # Place NO order immediately
    no_order = place_order(
        ticker=ticker,
        side="no",
        action="buy",
        quantity=num_contracts,
        order_type="market",
    )

    if not no_order.get("filled"):
        # NO didn't fill - we have directional exposure!
        result["error"] = "NO order failed - DIRECTIONAL EXPOSURE"
        result["warning"] = "Consider manually closing YES position"
        return result

    result["no_order"] = no_order

    # Calculate final P&L
    actual_yes_cost = yes_order['fill_price'] * num_contracts / 100
    actual_no_cost = no_order['fill_price'] * num_contracts / 100
    result["total_cost"] = actual_yes_cost + actual_no_cost
    result["expected_profit"] = num_contracts - result["total_cost"]
    result["success"] = True

    return result
```

---

## Risk Management

### Execution Risks
| Risk | Mitigation |
|------|------------|
| **Partial fill** | Use market orders, check liquidity first |
| **Price movement** | Execute quickly, verify spread before each order |
| **One side fails** | Have exit strategy for directional exposure |
| **Fees eat profit** | Ensure profit > fees (7% taker fee on Kalshi) |

### Fee Calculation
```python
def calculate_net_profit(
    yes_price: float,
    no_price: float,
    num_contracts: int,
    fee_rate: float = 0.07  # 7% taker fee
) -> dict:
    """
    Calculate net profit after fees.
    """
    total_cost = (yes_price + no_price) * num_contracts
    gross_profit = num_contracts - total_cost

    # Fee is on the winning side only
    # Worst case: fee on the full payout
    max_fee = num_contracts * fee_rate

    net_profit = gross_profit - max_fee

    return {
        "total_cost": total_cost,
        "gross_profit": gross_profit,
        "max_fee": max_fee,
        "net_profit": net_profit,
        "net_profit_pct": net_profit / total_cost if total_cost > 0 else 0,
        "profitable": net_profit > 0,
    }

# Example:
# YES: $0.48, NO: $0.48, 100 contracts
# Total cost: $96
# Payout: $100
# Gross profit: $4
# Fee (7% of $100): $7
# Net profit: -$3 (LOSS!)

# Need YES + NO < $0.93 to be profitable after 7% fee!
```

### Minimum Profitable Spread
```
With 7% fee:
- Payout: $1.00
- Max fee: $0.07
- Break-even cost: $0.93

Minimum profitable spread: YES + NO < $0.93
Target: YES + NO < $0.90 (for 3%+ net profit)
```

---

## Position Sizing

```python
def calculate_straddle_size(
    bankroll: float,
    profit_pct: float,
    confidence: str
) -> int:
    """
    Calculate number of contracts for straddle.
    """
    # Base allocation by confidence
    base_pct = {
        "HIGH": 0.05,    # 5% of bankroll
        "MEDIUM": 0.03,  # 3% of bankroll
        "LOW": 0.01,     # 1% of bankroll
    }

    allocation = bankroll * base_pct.get(confidence, 0.01)

    # Scale by profit potential
    if profit_pct >= 0.05:
        allocation *= 1.5  # Increase for high profit
    elif profit_pct < 0.04:
        allocation *= 0.75  # Decrease for marginal profit

    # Convert to contracts
    # Each contract pair costs ~$1, so allocation ≈ num_contracts
    num_contracts = int(allocation)

    # Limits
    num_contracts = max(1, num_contracts)  # Minimum 1
    num_contracts = min(100, num_contracts)  # Maximum 100

    return num_contracts

# Example with $1,000 bankroll:
# HIGH confidence, 5% profit → 5% * $1000 * 1.5 = 75 contracts
# MEDIUM confidence, 4% profit → 3% * $1000 = 30 contracts
# LOW confidence, 3% profit → 1% * $1000 * 0.75 = 7 contracts
```

---

## Dashboard Display

### Straddle Opportunities Table
```
┌────────────────────────────────────────────────────────────────────────────┐
│ 🎯 STRADDLE OPPORTUNITIES                                    [Scan Now]    │
├────────────────────────────────────────────────────────────────────────────┤
│ Market                    │ YES   │ NO    │ Total │ Profit │ Exp │ Action │
├───────────────────────────┼───────┼───────┼───────┼────────┼─────┼────────┤
│ BTC higher in 15 min?     │ $0.46 │ $0.46 │ $0.92 │ +8.7%  │ 12m │ [BUY]  │
│ ETH higher in 15 min?     │ $0.47 │ $0.48 │ $0.95 │ +5.3%  │ 8m  │ [BUY]  │
│ SOL higher in 15 min?     │ $0.49 │ $0.47 │ $0.96 │ +4.2%  │ 5m  │ [BUY]  │
└────────────────────────────────────────────────────────────────────────────┘
```

---

## Scheduling

```python
# Run straddle scanner every 30 seconds during market hours
STRADDLE_SCAN_INTERVAL = 30  # seconds

# Only scan during active crypto trading hours
# Crypto trades 24/7 but best opportunities at:
# - US market open (9:30 AM ET)
# - US market close (4:00 PM ET)
# - Crypto volatility events (BTC price swings)

def should_scan_straddles() -> bool:
    """Determine if we should scan for straddles now."""
    now = datetime.now(timezone('US/Eastern'))

    # Skip late night (less volume)
    if now.hour >= 1 and now.hour < 6:
        return False  # 1 AM - 6 AM ET

    return True
```

---

## Summary

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| **Min Profit %** | 3% (gross), 0% net after fees | Below this not worth execution risk |
| **Target Markets** | KXBTC, KXETH, KXSOL | 15-min crypto have best spreads |
| **Time Window** | 2-60 min to expiry | Too close = execution risk, too far = opportunity may close |
| **Max Position** | 100 contracts | Limit exposure |
| **Scan Interval** | 30 seconds | Opportunities are fleeting |
| **Fee Consideration** | 7% on winning side | Must factor into profit calc |

**Key Insight:** With Kalshi's 7% taker fee, you need YES + NO < $0.93 to actually profit. Adjust MIN_PROFIT_PCT accordingly!

---

*Logic designed by Claude (PM) - January 2025*
