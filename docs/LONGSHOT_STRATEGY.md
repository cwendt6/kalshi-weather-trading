# Longshot Hunter Strategy Guide

## Definition
A **longshot** is any market priced under $0.30 where we believe the true probability is significantly higher, offering 50-100%+ potential returns.

---

## Entry Criteria

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| **Max Entry Price** | $0.30 | Ensures asymmetric upside |
| **Min Expected Value** | Price ≤ 60% of our estimated probability | e.g., buy at $0.05 if we think true prob is $0.08+ |
| **Min Edge** | 3x potential return | If we buy at $0.05, target is at least $0.15 |

### Example Opportunity
- Market price: $0.05
- Our estimate: $0.20 true probability
- Edge: 4x (buy $0.05, fair value $0.20)
- Action: BUY YES

---

## Position Sizing (Percentage-Based)

**Philosophy:** Think in % of bankroll, not fixed dollars. A $200 loss on $1k bankroll (20%) is devastating, but on $20k bankroll (1%) is fine.

| Confidence Level | % of Bankroll | Example ($5k) | Example ($20k) | Notes |
|------------------|---------------|---------------|----------------|-------|
| **High Confidence** | 2-4% | $100-200 | $400-800 | Strong edge, multiple signals |
| **Medium Confidence** | 1-2% | $50-100 | $200-400 | Good edge, some uncertainty |
| **Low Confidence** | 0.5-1% | $25-50 | $100-200 | Speculative, but +EV |

### Hard Limits
| Rule | Value | Rationale |
|------|-------|-----------|
| **Min Position** | $3 | Kalshi minimum |
| **Max Position** | $200 | Absolute cap regardless of bankroll |
| **Max Single Bet %** | 5% of bankroll | Never risk >5% on one trade |
| **Max Longshot Exposure** | 15% of bankroll | All longshots combined |

### Edge-Based Scaling
Higher edge = can risk more (within limits):

| Edge (Our Prob / Market Prob) | Position Multiplier |
|-------------------------------|---------------------|
| 1.5x - 2x | 1.0x base position |
| 2x - 3x | 1.25x base position |
| 3x - 5x | 1.5x base position |
| 5x+ | 2.0x base position (rare, verify!) |

**Key Rule:** Position size allows for scaling out as market moves up.

---

## Exit Strategy (Laddered Sells)

**Order Type:** LIMIT ORDERS (confirmed by user - precise pricing preferred over speed)

When entering a longshot position, immediately place limit sell orders:

| Price Level | % of Position | Rationale |
|-------------|---------------|-----------|
| **2x entry** | 25% | Lock in profit, reduce risk |
| **3x entry** | 25% | Strong return, keep upside |
| **4x entry** | 25% | Excellent return |
| **Hold to expiry** | 25% | Let it ride for max payout |

### Example: $50 position at $0.05 (1000 contracts)
- Sell 250 @ $0.10 (+$12.50 profit)
- Sell 250 @ $0.15 (+$25.00 profit)
- Sell 250 @ $0.20 (+$37.50 profit)
- Hold 250 for potential $1.00 resolution (+$237.50 max)

**Total if all hit:** $312.50 profit on $50 risk (625% return)
**If only first two levels hit:** $37.50 profit (75% return)

---

## Portfolio Rules

| Rule | Value | Rationale |
|------|-------|-----------|
| **Max Active Longshots** | Unlimited | If edge exists, take it |
| **Max Total Longshot Exposure** | 30% of bankroll | Risk management |
| **Min Diversification** | No single longshot > 10% of longshot portfolio | Spread risk |

---

## Multi-Option Markets (NO Bets on Favorites)

Special opportunity: Markets with 3+ options where the favorite is overpriced.

### Example: "Who will win the election?"
- Candidate A: 75% ($0.75)
- Candidate B: 20% ($0.20)
- Candidate C: 5% ($0.05)

If we believe Candidate A's true probability is 65%, buying NO on A at $0.25 has edge:
- Cost: $0.25
- Expected value: $0.35 (65% chance of NOT A)
- Edge: 40%

**Key insight:** Favorites in multi-option markets often have inflated prices due to name recognition.

---

## Tracking & Metrics

### Per-Longshot Tracking
- Entry price, date, position size
- Limit orders placed
- Partial fills
- Final resolution
- Realized P&L

### Portfolio Metrics
| Metric | Target | Description |
|--------|--------|-------------|
| **Hit Rate** | 10-20% | % of longshots that pay out |
| **Avg Winner ROI** | 300%+ | Winners should be big |
| **Portfolio ROI** | 50%+ annually | Net of all wins/losses |
| **Largest Win** | Track | One big winner covers many losses |

### Break-Even Math
If we place 20 longshots at $10 each ($200 total):
- Need 1-2 winners at 10x+ to break even
- Example: 2 winners at $100 each = $200 = break even
- Any additional winners = pure profit

---

## Signal Strength Adjustments

| Signal Type | Longshot Multiplier |
|-------------|---------------------|
| **ML Model + Smart Money Agree** | 1.5x position |
| **ML Model Only** | 1.0x position |
| **Smart Money Only** | 0.75x position |
| **Arbitrage Detected** | 2.0x position (near risk-free) |

---

## Risk Controls

1. **No averaging down** - If a longshot drops further, don't add
2. **Time decay awareness** - Longshots near expiry with no movement = cut
3. **Correlation check** - Don't have multiple longshots on same underlying event
4. **Liquidity check** - Ensure we can exit if needed

---

## Implementation Notes for Claude Code

```python
class LongshotCriteria:
    MAX_ENTRY_PRICE = 0.30  # 30 cents
    MIN_EDGE_MULTIPLIER = 1.5  # Our estimate must be 1.5x market price
    MAX_POSITION_HIGH_CONF = 50.00
    MAX_POSITION_MED_CONF = 25.00
    MAX_POSITION_LOW_CONF = 10.00

    # Exit ladder (% of position at each level)
    EXIT_LADDER = [
        (2.0, 0.25),  # 2x entry, sell 25%
        (3.0, 0.25),  # 3x entry, sell 25%
        (4.0, 0.25),  # 4x entry, sell 25%
        (None, 0.25), # Hold to expiry
    ]

    # Portfolio limits
    MAX_LONGSHOT_EXPOSURE_PCT = 0.30  # 30% of bankroll
    MAX_SINGLE_LONGSHOT_PCT = 0.10    # 10% of longshot portfolio
```

---

*Last Updated: January 2025*
*Author: Claude (PM) + User Input*
