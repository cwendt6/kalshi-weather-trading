# Kalshi Fee Guide

## Understanding Kalshi's Fee Structure

This guide explains how Kalshi fees work and how they impact your trading profitability.

---

## The Basics

### Kalshi charges a **2% fee on winnings**.

- Fee applies only when you WIN a trade
- Fee is calculated on the **payout**, not your profit
- Losers pay nothing (beyond losing their stake)

### Fee Calculation

```
Fee = Payout × 0.02
Fee = $1.00 × 0.02 = $0.02 per contract
```

Every winning trade costs you **2 cents per contract**.

---

## Why This Matters

### The Impossible Scanner Problem

Our original impossible scanner strategy was **buying NO at 98-100¢**.

Let's see why this doesn't work:

```
Scenario: Buy NO at 99¢
If we WIN (event doesn't happen):
  Payout: $1.00
  Fee: $0.02 (2% of $1.00)
  Net payout: $0.98
  Cost: $0.99
  Net Profit: $0.98 - $0.99 = -$0.01 ❌ LOSS!

Even though we "won" the trade, we lost money!
```

### The Corrected Approach

We now only buy NO at **96¢ or less**:

```
Scenario: Buy NO at 96¢
If we WIN:
  Payout: $1.00
  Fee: $0.02
  Net payout: $0.98
  Cost: $0.96
  Net Profit: $0.98 - $0.96 = $0.02 ✅ PROFIT!
```

---

## Complete Fee Table

### Buying NO Contracts

| NO Price | Cost | Gross Profit | Fee | Net Profit | Result |
|----------|------|--------------|-----|------------|--------|
| 90¢ | $0.90 | 10¢ | 2¢ | **8¢** | ✅ Great |
| 91¢ | $0.91 | 9¢ | 2¢ | **7¢** | ✅ Great |
| 92¢ | $0.92 | 8¢ | 2¢ | **6¢** | ✅ Great |
| 93¢ | $0.93 | 7¢ | 2¢ | **5¢** | ✅ Good |
| 94¢ | $0.94 | 6¢ | 2¢ | **4¢** | ✅ Good |
| 95¢ | $0.95 | 5¢ | 2¢ | **3¢** | ✅ OK |
| **96¢** | $0.96 | 4¢ | 2¢ | **2¢** | ✅ Minimum |
| 97¢ | $0.97 | 3¢ | 2¢ | **1¢** | ⚠️ Marginal |
| 98¢ | $0.98 | 2¢ | 2¢ | **0¢** | ❌ Break-even |
| 99¢ | $0.99 | 1¢ | 2¢ | **-1¢** | ❌ LOSS |
| 100¢ | $1.00 | 0¢ | 2¢ | **-2¢** | ❌ LOSS |

### Buying YES Contracts

| YES Price | Cost | Gross Profit | Fee | Net Profit | Result |
|-----------|------|--------------|-----|------------|--------|
| 50¢ | $0.50 | 50¢ | 2¢ | **48¢** | ✅ Great |
| 60¢ | $0.60 | 40¢ | 2¢ | **38¢** | ✅ Good |
| 70¢ | $0.70 | 30¢ | 2¢ | **28¢** | ✅ OK |
| 80¢ | $0.80 | 20¢ | 2¢ | **18¢** | ✅ OK |
| 90¢ | $0.90 | 10¢ | 2¢ | **8¢** | ⚠️ Low margin |

---

## Straddle Arbitrage Fees

In straddle arbitrage, you buy BOTH sides. One side wins, one loses.

### Fee Impact on Straddles

```
Scenario: Buy YES at 48¢ + NO at 48¢ = 96¢ total

If YES wins:
  YES payout: $1.00 - 2¢ fee = $0.98
  NO payout: $0.00 (lost)
  Total return: $0.98
  Total cost: $0.96
  Net profit: +$0.02 ✅

If NO wins:
  Same math, same result: +$0.02 ✅
```

### Straddle Profitability Table

| Total Cost (YES + NO) | Gross Profit | Fee | Net Profit | Profitable? |
|-----------------------|--------------|-----|------------|-------------|
| 94¢ | 6¢ | 2¢ | **4¢** | ✅ Great |
| 95¢ | 5¢ | 2¢ | **3¢** | ✅ Good |
| **96¢** | 4¢ | 2¢ | **2¢** | ✅ Minimum |
| 97¢ | 3¢ | 2¢ | **1¢** | ⚠️ Marginal |
| 98¢ | 2¢ | 2¢ | **0¢** | ❌ Break-even |
| 99¢ | 1¢ | 2¢ | **-1¢** | ❌ LOSS |

**Rule:** Only execute straddles when total cost ≤ 96¢

---

## Break-Even Analysis

### What Win Rate Do You Need?

For any trade, you need to calculate the break-even win rate:

```
Break-Even Win Rate = Cost / Net Payout
                    = Cost / (Payout - Fee)
                    = Cost / ($1.00 - $0.02)
                    = Cost / $0.98
```

| Buy Price | Break-Even Win Rate | Notes |
|-----------|---------------------|-------|
| 50¢ | 51.0% | Standard trade |
| 60¢ | 61.2% | Need 61%+ win rate |
| 70¢ | 71.4% | Need 71%+ win rate |
| 80¢ | 81.6% | Need 82%+ win rate |
| 90¢ | 91.8% | Need 92%+ win rate |
| 95¢ | 96.9% | Need 97%+ win rate |
| 96¢ | 98.0% | Need 98%+ win rate |
| 97¢ | 99.0% | Need 99%+ win rate |
| 98¢ | 100.0% | Must NEVER lose |
| 99¢ | 101.0% | **IMPOSSIBLE** |

### Key Insight

At 99¢ NO price, you need a 101% win rate to break even. This is impossible!

This is why our original impossible scanner was losing money despite a 52% win rate - we were taking trades that couldn't mathematically profit.

---

## Strategy-Specific Fee Considerations

### 1. Impossible Scanner

**Old Settings (Unprofitable):**
- Max NO price: 99¢
- Result: Losing money on every trade

**New Settings (Profitable):**
- Max NO price: 96¢
- Required win rate: 98%+
- Expected win rate: 99%+
- Net profit: 2-4¢ per contract

### 2. Weather Strategy

- Edge threshold: 10% minimum
- This provides buffer for fees
- At 10% edge, 2% fee still leaves 8% net edge

### 3. ML Signal Generator

- Minimum edge: 5% (accounts for 2% fee)
- Net edge after fees: 3%+
- Position sizing already factors in fees

### 4. Straddle Arbitrage

- Only execute when YES + NO ≤ 96¢
- Guarantees 2¢+ net profit per contract
- No win rate concern (always wins)

---

## Fee Tracking in the Dashboard

### P&L Display

The dashboard now shows:

| Column | Description |
|--------|-------------|
| Gross P&L | Profit before fees |
| Fees Paid | Total fees on winning trades |
| Net P&L | Profit after fees (what matters!) |

### Trade Details

Each trade shows:
- Entry price
- Exit price (if resolved)
- Gross profit/loss
- Fee amount
- **Net profit/loss**

---

## Common Fee Mistakes

### Mistake 1: Ignoring Fees in Backtests
❌ "My backtest shows 60% win rate at 90¢ = profitable!"
✅ Always calculate: `(Win% × Net Payout) - (Loss% × Cost)`

### Mistake 2: Trading at 99¢
❌ "It's almost guaranteed money!"
✅ At 99¢, even 100% win rate = $0 profit

### Mistake 3: Celebrating Gross Profit
❌ "I made $100 gross profit today!"
✅ Check net: $100 gross - $X fees = actual profit

### Mistake 4: Not Tracking Fees
❌ "I don't know how much I've paid in fees"
✅ Track fees to understand true performance

---

## Fee Calculator

Use this formula to check any trade:

```python
def calculate_trade_profit(buy_price: float, win_probability: float) -> dict:
    """
    Calculate expected profit for a trade.

    Args:
        buy_price: Cost per contract in dollars (e.g., 0.96)
        win_probability: Probability of winning (e.g., 0.99)

    Returns:
        Dict with profit calculations
    """
    KALSHI_FEE = 0.02

    gross_payout_if_win = 1.00
    net_payout_if_win = gross_payout_if_win - KALSHI_FEE  # 0.98

    gross_profit_if_win = gross_payout_if_win - buy_price
    net_profit_if_win = net_payout_if_win - buy_price
    loss_if_lose = buy_price

    expected_value = (win_probability * net_profit_if_win) - ((1 - win_probability) * loss_if_lose)
    break_even_win_rate = buy_price / net_payout_if_win

    return {
        "buy_price": buy_price,
        "gross_profit_if_win": gross_profit_if_win,
        "fee": KALSHI_FEE,
        "net_profit_if_win": net_profit_if_win,
        "loss_if_lose": loss_if_lose,
        "expected_value": expected_value,
        "break_even_win_rate": break_even_win_rate,
        "profitable": expected_value > 0,
    }

# Example usage:
result = calculate_trade_profit(buy_price=0.96, win_probability=0.99)
print(f"Expected Value: ${result['expected_value']:.4f}")
# Output: Expected Value: $0.0102 (profitable!)

result = calculate_trade_profit(buy_price=0.99, win_probability=0.99)
print(f"Expected Value: ${result['expected_value']:.4f}")
# Output: Expected Value: -$0.0099 (losing money!)
```

---

## Quick Reference Card

### Never Trade When:
- NO price > 96¢
- YES + NO > 96¢ (for straddles)
- Expected net profit < 0

### Always Check:
- Gross profit
- Fee amount (2¢ per winning contract)
- Net profit
- Break-even win rate

### Code Constants:
```python
KALSHI_WINNER_FEE = 0.02      # 2% fee
MAX_NO_PRICE = 0.96            # Never buy above this
MIN_NET_PROFIT = 0.02          # Minimum 2¢ net profit
NET_PAYOUT = 0.98              # $1.00 - $0.02 fee
```

---

## Summary

1. **Kalshi charges 2% on winnings** ($0.02 per contract)
2. **Never buy NO above 96¢** (guaranteed loss or break-even)
3. **Straddles need total cost ≤ 96¢** to be profitable
4. **Always calculate NET profit**, not gross
5. **Track fees separately** to understand true performance

Understanding fees is the difference between a profitable trading system and one that slowly bleeds money. Our system lost -$25.08 on 744 trades before we discovered the fee issue. With corrected thresholds, those same trades would have been profitable.

---

*Last Updated: February 2026*
