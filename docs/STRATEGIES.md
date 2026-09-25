# Kalshi Trading System - Strategy Documentation

## Overview

This document describes all trading strategies implemented in the Kalshi Trading System. Each strategy has different characteristics, risk profiles, and expected returns.

**Important:** All strategies must account for Kalshi's 2% winner fee on the $1.00 payout.

---

## Strategy Summary

| Strategy | Risk Level | Expected Return | Frequency | Status |
|----------|------------|-----------------|-----------|--------|
| Impossible Scanner | Low | 2-4% per trade | 10-50/day | ✅ Active |
| Weather Strategy | Medium | 5-15% per trade | 2-10/day | ⚠️ Needs Testing |
| Straddle Arbitrage | Very Low | 2-4% per trade | 1-5/day | ⚠️ Needs Testing |
| ML Signal Generator | Medium | 5-20% per trade | 5-20/day | ⚠️ Needs ML Data |
| Longshot Hunter | High | 5-50x returns | 1-3/day | 🔬 Experimental |

---

## 1. Impossible Scanner (swisstony Strategy)

### Overview
Based on the legendary @swisstony who made **$3.7M in 5 months** with 22,000+ trades.

The strategy targets markets with 95%+ probability outcomes where you can buy NO contracts at 95-96¢ and collect $1.00 when the "impossible" event doesn't happen.

### How It Works

1. **Scan** for markets where:
   - YES price ≤ 5¢ (probability ≤ 5%)
   - NO price between 95-96¢
   - Market hasn't expired

2. **Identify "impossible" events:**
   - Sports games already finished
   - Players injured/suspended
   - Weather physically impossible (e.g., NYC at 150°F)
   - Mathematical impossibilities (team eliminated from playoffs)
   - Past tense language ("yesterday", "already won")

3. **Execute** by buying NO at 95-96¢

4. **Collect** $1.00 when the event resolves to NO (minus 2% fee = 98¢)

### Fee Impact (Critical!)

```
NO Price | Gross Profit | 2% Fee | Net Profit | Profitable?
---------|--------------|--------|------------|------------
96¢      | 4¢           | 2¢     | 2¢         | ✅ YES
97¢      | 3¢           | 2¢     | 1¢         | ⚠️ Marginal
98¢      | 2¢           | 2¢     | 0¢         | ❌ Break-even
99¢      | 1¢           | 2¢     | -1¢        | ❌ LOSS
100¢     | 0¢           | 2¢     | -2¢        | ❌ LOSS
```

**Rule:** Never buy NO above 96¢!

### Position Sizing

| Confidence Level | Position Size | When to Use |
|------------------|---------------|-------------|
| HIGH (99%+)      | $50           | Past events, physical impossibilities |
| MEDIUM (97-99%)  | $25           | Unlikely sports outcomes |
| LOW (95-97%)     | $10           | Possible but improbable |

### Target Markets

- **Sports:** `KXNFL*`, `KXNBA*`, `KXMLB*`, `KXNHL*`, `KXNCAA*`
- **Weather:** `KXTEMP*` with extreme thresholds
- **Politics:** Past events or mathematical impossibilities
- **Crypto:** Extreme price movements (BTC to $1M today)

### Expected Performance

- **Win Rate:** 95-99%
- **Average Net Profit:** 2-3¢ per contract
- **Trade Frequency:** 10-50 trades/day
- **Annual Return:** 50-150% (with compounding)

### Risks

1. **Black Swan Events:** Extremely unlikely events DO happen occasionally
2. **Liquidity:** May not be able to fill large orders
3. **Fee Changes:** If Kalshi increases fees, strategy becomes less profitable
4. **Market Mistakes:** What looks "impossible" might not be

### Code Location
`src/strategy/impossible_scanner.py`

---

## 2. Weather Strategy

### Overview
Exploits the fact that Kalshi temperature markets settle based on National Weather Service (NWS) data. By using NWS forecasts as "ground truth," we can identify mispricings.

### How It Works

1. **Fetch NWS forecasts** for supported cities (NYC, Chicago, Miami, Austin)

2. **Match to Kalshi markets** with temperature thresholds

3. **Calculate our probability:**
   - If NWS says 75°F high, and market asks "Will it be above 70°F?", probability is HIGH
   - Account for NWS forecast uncertainty (±3-8°F depending on forecast window)

4. **Generate signals** when our probability differs from market by >10%

### Example

```
Market: "NYC high temperature above 45°F on Feb 10?"
NWS Forecast: 52°F high (high confidence)
Our Probability: 90% (forecast is 7°F above threshold)
Market Price: 70¢ (implies 70% probability)
Edge: 20%!
Action: BUY YES at 70¢
```

### Confidence Adjustments

| Forecast Window | NWS Uncertainty | Confidence Multiplier |
|-----------------|-----------------|----------------------|
| 1 day           | ±3°F            | 100%                 |
| 2-3 days        | ±5°F            | 85%                  |
| 4+ days         | ±8°F            | 70%                  |

### Supported Cities

| City | Kalshi Ticker | NWS Station |
|------|---------------|-------------|
| NYC | KXTEMP-NYC-* | KJFK (JFK Airport) |
| Chicago | KXTEMP-CHICAGO-* | KORD (O'Hare) |
| Miami | KXTEMP-MIAMI-* | KMIA (Miami Intl) |
| Austin | KXTEMP-AUSTIN-* | KAUS (Austin-Bergstrom) |

### Expected Performance

- **Win Rate:** 60-70%
- **Average Profit:** 5-15% per winning trade
- **Trade Frequency:** 2-10 trades/day
- **Edge:** 10-20% when signals fire

### Risks

1. **NWS Errors:** Forecasts can be wrong, especially for edge cases
2. **Timing:** Forecasts update throughout the day, market may adjust
3. **Limited Markets:** Only a few cities currently supported by Kalshi
4. **Fee Impact:** Must account for 2% winner fee

### Code Location
`src/strategy/weather_strategy.py`

---

## 3. Straddle Arbitrage

### Overview
A risk-free arbitrage strategy that buys both YES and NO on the same market when their combined price is less than $1.00.

Since one side ALWAYS wins (paying $1.00), if the total cost is <$1.00, profit is guaranteed.

### How It Works

1. **Scan** for markets where:
   - YES ask + NO ask < $0.97 (accounting for fees)
   - Market expires within 1 hour (15-min crypto markets ideal)
   - Sufficient liquidity on both sides

2. **Buy both sides:**
   - Example: YES at 48¢ + NO at 48¢ = 96¢ total cost
   - One side WILL pay $1.00
   - Gross profit: 4¢ per contract
   - Net profit after 2% fee: 2¢ per contract

3. **Wait for settlement** - no directional risk!

### Example Trade

```
Market: "Will BTC be above $100,000 at 3:15 PM?"
YES Price: 48¢
NO Price: 48¢
Total Cost: 96¢

Outcome A: BTC is above $100K
  → YES pays $1.00, minus 2¢ fee = 98¢
  → Net: 98¢ - 96¢ = +2¢ profit

Outcome B: BTC is below $100K
  → NO pays $1.00, minus 2¢ fee = 98¢
  → Net: 98¢ - 96¢ = +2¢ profit

Result: Guaranteed 2¢ profit regardless of outcome!
```

### Target Markets

- `KXBTC*` - Bitcoin 15-minute price markets
- `KXETH*` - Ethereum 15-minute price markets
- `KXSOL*` - Solana 15-minute price markets
- `KXQUICK*` - Quick-settle markets

### Fee Calculations

```
Total Cost | Gross Profit | Fee (2% of $1) | Net Profit | Profitable?
-----------|--------------|----------------|------------|------------
96¢        | 4¢           | 2¢             | 2¢         | ✅ YES
97¢        | 3¢           | 2¢             | 1¢         | ⚠️ Marginal
98¢        | 2¢           | 2¢             | 0¢         | ❌ Break-even
99¢        | 1¢           | 2¢             | -1¢        | ❌ LOSS
```

**Rule:** Only execute straddles when YES + NO ≤ 96¢!

### Expected Performance

- **Win Rate:** 100% (guaranteed)
- **Net Profit:** 2-3¢ per contract
- **Trade Frequency:** 1-5 trades/day (opportunities are rare)
- **Capital Required:** ~$1-10 per straddle

### Risks

1. **Execution Risk:** One side may not fill, leaving you with directional exposure
2. **Liquidity:** May not find both sides at good prices
3. **Rare Opportunities:** True arbitrage is quickly exploited by others
4. **Fee Changes:** If fees increase, fewer opportunities exist

### Code Location
`src/strategy/straddle_arbitrage.py`

---

## 4. ML Signal Generator

### Overview
Uses machine learning models to predict market outcomes and generate trading signals when the model disagrees with market prices.

### How It Works

1. **Features analyzed:**
   - Historical price patterns
   - Volume and open interest
   - Time to expiration
   - Market category
   - Smart money indicators

2. **ML Prediction:**
   - Model outputs probability for YES outcome
   - Compared to market implied probability (YES price / 100)

3. **Signal Generation:**
   - If model probability > market price by >5%: BUY YES
   - If model probability < market price by >5%: BUY NO
   - Apply confidence and risk filters

4. **Position Sizing:**
   - Kelly criterion for optimal bet size
   - Capped at maximum position limits

### Smart Money Analysis

The system analyzes "smart money" patterns:

| Indicator | What It Means |
|-----------|---------------|
| Volume Trend | Is trading activity increasing? |
| Large Trades | Are whales entering positions? |
| Price Momentum | Direction of recent price moves |
| Consensus | Are large traders agreeing on direction? |

### Signal Strength Levels

| Strength | Edge | Confidence | Smart Money | Action |
|----------|------|------------|-------------|--------|
| STRONG   | >10% | >70%       | >0.6        | Full position |
| MODERATE | >5%  | >50%       | >0.3        | Half position |
| WEAK     | >2%  | >40%       | >0.2        | Quarter position |
| HOLD     | <2%  | <40%       | -           | No trade |

### Expected Performance

- **Win Rate:** 55-65% (depends on model quality)
- **Average Profit:** 5-20% per winning trade
- **Trade Frequency:** 5-20 trades/day
- **Requires:** 500+ resolved markets for training

### Risks

1. **Model Accuracy:** ML predictions may be wrong
2. **Overfitting:** Model may work on historical data but fail on new data
3. **Data Quality:** Bad data = bad predictions
4. **Market Regime Changes:** Patterns may change over time

### Code Location
`src/strategy/signal_generator.py`

---

## 5. Longshot Hunter (Experimental)

### Overview
Targets low-probability, high-reward opportunities where small positions can generate large returns.

### How It Works

1. **Find underpriced longshots:**
   - YES price between 1-10¢
   - Our analysis suggests probability is 2-5x higher than market implies

2. **Small positions:**
   - Risk only 1-2% of bankroll per trade
   - Expect most to lose, but winners pay 10-50x

3. **Focus on:**
   - Political upsets
   - Sports underdogs
   - Unexpected weather events

### Example

```
Market: "Will underdog team win championship?"
Market Price: 3¢ (implies 3% probability)
Our Analysis: 8% probability (2.7x higher than market)
Position: $10 (risking $10 to potentially win $330)

If win: +$330 (33x return)
If lose: -$10
Expected Value: (0.08 × $330) - (0.92 × $10) = $26.40 - $9.20 = +$17.20
```

### Expected Performance

- **Win Rate:** 5-15%
- **Average Winner:** 10-50x return
- **Trade Frequency:** 1-3 trades/day
- **Risk:** High (most trades lose)

### Risks

1. **High Variance:** Long losing streaks are expected
2. **Probability Estimation:** Hard to estimate true probability of rare events
3. **Bankroll Management:** Must size positions correctly
4. **Emotional Discipline:** Don't chase losses

### Code Location
`src/strategy/longshot_hunter.py`

---

## Strategy Comparison

### By Risk/Return Profile

```
                      Expected Return
                           ↑
                           │
        Longshot ●         │
        Hunter             │
                           │
              ML Signals ● │
                           │      ● Weather
                           │
                           │
    Straddle ●─────────────┼───────── ● Impossible
    Arbitrage              │             Scanner
                           │
                           └─────────────────────→ Risk Level
                         LOW              HIGH
```

### Recommended Allocation

| Strategy | Allocation | Rationale |
|----------|------------|-----------|
| Impossible Scanner | 45% | Consistent, low-risk profits |
| Weather Strategy | 20% | Good edge, limited opportunities |
| Straddle Arbitrage | 15% | Risk-free but rare |
| ML Signals | 15% | Higher returns, higher risk |
| Longshot Hunter | 5% | Small allocation for big upside |

---

## Getting Started

### 1. Start with Impossible Scanner
- Lowest risk
- Easiest to understand
- Good for building confidence

### 2. Add Weather Strategy
- Requires understanding NWS data
- Limited to specific cities
- Higher potential returns

### 3. Experiment with Straddles
- Perfect for learning market mechanics
- Zero directional risk
- Teaches fee impact

### 4. Train ML Models
- Needs 500+ resolved trades for training
- Higher complexity
- Best returns if model is accurate

### 5. Small Longshot Positions
- Only after other strategies are profitable
- Very small position sizes
- For excitement, not core returns

---

## Important Reminders

### Fee Awareness
**Every strategy must account for Kalshi's 2% winner fee.**

- Never buy NO above 96¢ in impossible scanner
- Never execute straddles with total cost > 96¢
- Always calculate NET profit, not gross

### Position Sizing
- Never risk more than 5% of bankroll on a single trade
- Diversify across strategies and markets
- Keep cash reserve for opportunities

### Risk Management
- Set daily loss limits (5% of bankroll)
- Auto-pause on consecutive losses
- Review and adjust regularly

---

## Quick Reference

### File Locations
```
src/strategy/
├── impossible_scanner.py   # swisstony strategy
├── weather_strategy.py     # NWS-based weather trades
├── straddle_arbitrage.py   # Risk-free arbitrage
├── signal_generator.py     # ML pipeline
├── longshot_hunter.py      # High-risk/high-reward
├── arbitrage_detector.py   # General arbitrage detection
└── btc_ta_signals.py       # Bitcoin technical analysis
```

### Key Constants
```python
KALSHI_WINNER_FEE = 0.02    # 2% fee on winning trades
MAX_NO_PRICE = 0.96          # Never buy NO above 96¢
MIN_STRADDLE_PROFIT = 0.04   # Minimum 4% gross for straddles
MIN_EDGE_THRESHOLD = 0.05    # 5% edge for ML signals
```

### Dashboard
Access at: `http://localhost:8501`

---

*Last Updated: February 2026*
