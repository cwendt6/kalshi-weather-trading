# Model Health & Drift Detection Guide

## Overview

This document defines how we distinguish between a **broken model** (systematic failure) and **bad luck** (variance within expected parameters). The goal is to auto-pause trading when the model is statistically broken while tolerating normal variance.

---

## Key Metrics

### 1. Brier Score (Primary Calibration Metric)

The [Brier score](https://en.wikipedia.org/wiki/Brier_score) measures how well our probability predictions match actual outcomes.

| Score | Interpretation |
|-------|----------------|
| 0.00 | Perfect predictions |
| 0.25 | Random guessing (50/50) |
| 0.33 | Worse than random |
| 1.00 | Always wrong |

**Our Targets:**
| Threshold | Action |
|-----------|--------|
| **< 0.20** | Excellent - increase position sizes |
| **0.20 - 0.25** | Good - normal operation |
| **0.25 - 0.30** | Warning - reduce position sizes by 50% |
| **> 0.30** | Critical - auto-pause trading |

**Rolling Window:** Calculate over last 50 resolved trades (minimum for statistical significance).

### 2. Win Rate with Statistical Significance

Based on [research on trading strategy significance](https://medium.com/@trading.dude/how-many-trades-are-enough-a-guide-to-statistical-significance-in-backtesting-093c2eac6f05):

| Sample Size | Required for 95% Confidence |
|-------------|----------------------------|
| 30 trades | Minimum to begin testing |
| 100 trades | Basic significance possible |
| 385 trades | 5% margin of error |
| 500+ trades | Strong statistical power |

**Statistical Test:** Use t-test against null hypothesis that returns = 0

### 3. Expected vs Actual Accuracy

For well-calibrated predictions:
- When we predict 70% → outcomes should be YES ~70% of the time
- When we predict 30% → outcomes should be YES ~30% of the time

**Calibration Buckets:**
| Predicted Probability | Expected Accuracy | Alert if Actual is |
|----------------------|-------------------|-------------------|
| 60-70% | ~65% YES | < 50% or > 80% |
| 70-80% | ~75% YES | < 60% or > 90% |
| 80-90% | ~85% YES | < 70% or > 95% |
| 90%+ | ~92% YES | < 80% |

---

## Broken Model vs Bad Luck

### Bad Luck (Variance - Don't Panic)

**Characteristics:**
- Losing streak of 3-5 trades in a row
- Win rate temporarily drops but rebounds
- Brier score stays under 0.30
- Losses are on **high-uncertainty** trades (predicted 55-65%)
- Longshot losses (expected - these will lose often)

**Statistical Reality:** Even with a 60% win rate:
- 5 losses in a row: 1.0% chance (will happen ~1x per 100 trades)
- 4 losses in a row: 2.6% chance (will happen ~3x per 100 trades)
- 3 losses in a row: 6.4% chance (will happen ~6x per 100 trades)

**Action:** Continue trading, review positions but don't change strategy.

### Broken Model (Systematic Failure - Take Action)

**Characteristics:**
- Win rate below 45% over 50+ resolved trades
- Brier score > 0.30 for 2+ consecutive weeks
- Losing on **high-confidence** trades (predicted 75%+)
- Pattern: Consistently wrong on specific market categories
- Smart money consensus diverging from model

**Statistical Test:**
If observed win rate is W over N trades, calculate:
```
z = (W - 0.50) / sqrt(0.50 * 0.50 / N)
```
If z < -1.96, model is statistically worse than random (p < 0.05).

**Example:**
- 100 trades, 40 wins (40% win rate)
- z = (0.40 - 0.50) / sqrt(0.25 / 100) = -0.10 / 0.05 = -2.0
- Result: Statistically significant underperformance → PAUSE

**Action:** Auto-pause trading, alert user, investigate root cause.

---

## Auto-Pause Triggers

### Immediate Pause (Any Single Trigger)
1. Brier score > 0.35 over 30+ trades
2. Win rate < 40% over 100+ trades
3. 8+ consecutive losses (0.4% chance if model is working)
4. Single loss > 20% of bankroll

### Warning State (Reduce Position Sizes 50%)
1. Brier score 0.28-0.35 over 30+ trades
2. Win rate 40-45% over 50+ trades
3. 5-7 consecutive losses
4. Smart money disagreement rate > 40%

### Normal Operation Resume
1. Brier score returns to < 0.25 over 20+ new trades
2. Win rate recovers to > 52% over 30+ new trades
3. Manual review and approval

---

## Longshot-Specific Rules

Longshots have different statistics:

| Metric | Regular Trades | Longshots |
|--------|----------------|-----------|
| **Expected Win Rate** | 55-65% | 10-25% |
| **Acceptable Loss Streak** | 5 trades | 15 trades |
| **Evaluation Window** | 50 trades | 20 trades |
| **Success Criteria** | Positive P&L | 1 in 10 covers losses |

**Longshot Portfolio Health:**
- Track separately from main portfolio
- Success = Total longshot P&L positive over 20+ resolved bets
- One 10x winner erases nine 1x losses

---

## Dashboard KPIs

### Model Health Panel

| Metric | Display | Alert Threshold |
|--------|---------|-----------------|
| **Brier Score (30d)** | 0.XX | Yellow > 0.25, Red > 0.30 |
| **Win Rate (50 trades)** | XX% | Yellow < 50%, Red < 45% |
| **Current Streak** | W/L + count | Red if 6+ losses |
| **Calibration Error** | ±X% | Red if > 15% |
| **Model Confidence** | High/Med/Low | Based on above metrics |
| **Days Since Retrain** | X days | Yellow > 30, Red > 60 |

### Trend Charts
1. **Rolling Brier Score** - 7-day, 30-day lines
2. **Cumulative P&L** - With confidence bands
3. **Calibration Plot** - Predicted vs Actual by bucket
4. **Win Rate Moving Average** - 20-trade window

### Alerts Log
- Timestamp + Event + Severity
- "2025-01-28 14:30 - 5 consecutive losses - WARNING"
- "2025-01-28 15:00 - Brier score 0.28 - WARNING"

---

## Statistical Formulas for Implementation

```python
import numpy as np
from scipy import stats

def calculate_brier_score(predictions: list[float], outcomes: list[int]) -> float:
    """
    Brier score: mean squared error of probability predictions.
    predictions: list of predicted probabilities (0-1)
    outcomes: list of actual outcomes (0 or 1)
    """
    return np.mean((np.array(predictions) - np.array(outcomes)) ** 2)


def is_model_broken(win_rate: float, n_trades: int, alpha: float = 0.05) -> bool:
    """
    Test if model is statistically worse than random (50%).
    Returns True if model should be paused.
    """
    if n_trades < 30:
        return False  # Not enough data

    # Z-test against 50% null hypothesis
    z = (win_rate - 0.50) / np.sqrt(0.25 / n_trades)
    p_value = stats.norm.cdf(z)  # One-tailed test (underperformance)

    return p_value < alpha


def consecutive_loss_probability(win_rate: float, streak: int) -> float:
    """
    Probability of seeing N consecutive losses given win rate.
    """
    loss_rate = 1 - win_rate
    return loss_rate ** streak


def required_sample_size(margin_of_error: float = 0.05, confidence: float = 0.95) -> int:
    """
    Calculate required sample size for given margin of error.
    """
    z = stats.norm.ppf(1 - (1 - confidence) / 2)
    n = (z ** 2 * 0.25) / (margin_of_error ** 2)
    return int(np.ceil(n))


# Example thresholds
class ModelHealthThresholds:
    BRIER_EXCELLENT = 0.20
    BRIER_GOOD = 0.25
    BRIER_WARNING = 0.30
    BRIER_CRITICAL = 0.35

    WIN_RATE_GOOD = 0.52
    WIN_RATE_WARNING = 0.45
    WIN_RATE_CRITICAL = 0.40

    MIN_TRADES_BASIC = 30
    MIN_TRADES_SIGNIFICANT = 100
    MIN_TRADES_STRONG = 385

    MAX_CONSECUTIVE_LOSSES = 8
    WARNING_CONSECUTIVE_LOSSES = 5
```

---

## Recovery Protocol

**Resume Mode:** AUTO-RESUME (confirmed by user)

When model is paused:

1. **Immediate:** Stop all new trades, switch to paper-only mode
2. **Within 1 hour:** Generate diagnostic report
   - Which categories are underperforming?
   - Which confidence levels are miscalibrated?
   - Any data quality issues?
3. **Auto-Recovery Phase:**
   - Continue paper trading while paused
   - Monitor metrics on paper trades
4. **Auto-Resume Criteria (ALL must be met):**
   - Brier score < 0.25 over 20+ paper trades
   - Win rate > 52% over 30+ paper trades
   - No critical errors in last 24 hours
   - System automatically resumes live trading
5. **Notification:**
   - Email sent when paused
   - Email sent when auto-resumed with recovery stats

---

## Sources

- [Brier Score - Wikipedia](https://en.wikipedia.org/wiki/Brier_score)
- [How Many Trades Are Enough? - Medium](https://medium.com/@trading.dude/how-many-trades-are-enough-a-guide-to-statistical-significance-in-backtesting-093c2eac6f05)
- [Evaluating Prediction Markets - Simon de la Rouviere](https://sceneswithsimon.com/p/evaluating-prediction-markets)
- [Neptune.ai - Brier Score and Model Calibration](https://neptune.ai/blog/brier-score-and-model-calibration)
- [QuantInsti - Hypothesis Testing in Trading](https://blog.quantinsti.com/hypothesis-testing-trading-guide/)

---

*Last Updated: January 2025*
*Author: Claude (PM) based on user requirements and statistical research*
