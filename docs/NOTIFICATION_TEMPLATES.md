# Notification Templates

## Daily Summary Email

**Subject:** 📊 Kalshi Trading Daily Report - {date} | P&L: {pnl_formatted}

```html
<!DOCTYPE html>
<html>
<head>
    <style>
        body { font-family: 'JetBrains Mono', monospace; background: #0a0a0f; color: #e0e0e0; }
        .header { background: linear-gradient(135deg, #1a1a2e, #0a0a0f); padding: 20px; border-bottom: 2px solid #00ff88; }
        .stat-positive { color: #00ff88; }
        .stat-negative { color: #ff4455; }
        .stat-neutral { color: #00d4ff; }
        .stat-warning { color: #ffaa00; }
        .section { padding: 15px; border-bottom: 1px solid #333; }
        .metric-box { display: inline-block; padding: 10px 20px; margin: 5px; background: #1a1a2e; border-radius: 8px; }
        table { width: 100%; border-collapse: collapse; }
        th, td { padding: 8px 12px; text-align: left; border-bottom: 1px solid #333; }
        th { color: #00d4ff; }
    </style>
</head>
<body>
    <div class="header">
        <h1>📊 Daily Trading Report</h1>
        <p>{date} | Mode: {trading_mode}</p>
    </div>

    <!-- KEY METRICS -->
    <div class="section">
        <h2>💰 Today's Performance</h2>
        <div class="metric-box">
            <div style="font-size: 12px; color: #888;">Daily P&L</div>
            <div class="{pnl_class}" style="font-size: 24px; font-weight: bold;">{daily_pnl}</div>
        </div>
        <div class="metric-box">
            <div style="font-size: 12px; color: #888;">Win Rate</div>
            <div class="{winrate_class}" style="font-size: 24px; font-weight: bold;">{daily_winrate}%</div>
        </div>
        <div class="metric-box">
            <div style="font-size: 12px; color: #888;">Trades</div>
            <div class="stat-neutral" style="font-size: 24px; font-weight: bold;">{trades_today}</div>
        </div>
        <div class="metric-box">
            <div style="font-size: 12px; color: #888;">Outstanding</div>
            <div class="stat-neutral" style="font-size: 24px; font-weight: bold;">{outstanding_positions}</div>
        </div>
    </div>

    <!-- TRADE BREAKDOWN -->
    <div class="section">
        <h2>📈 Trade Breakdown</h2>
        <table>
            <tr>
                <th>Metric</th>
                <th>Today</th>
                <th>7-Day</th>
                <th>30-Day</th>
            </tr>
            <tr>
                <td>Markets Placed</td>
                <td>{trades_today}</td>
                <td>{trades_7d}</td>
                <td>{trades_30d}</td>
            </tr>
            <tr>
                <td>Bets Won</td>
                <td class="stat-positive">{wins_today}</td>
                <td class="stat-positive">{wins_7d}</td>
                <td class="stat-positive">{wins_30d}</td>
            </tr>
            <tr>
                <td>Bets Lost</td>
                <td class="stat-negative">{losses_today}</td>
                <td class="stat-negative">{losses_7d}</td>
                <td class="stat-negative">{losses_30d}</td>
            </tr>
            <tr>
                <td>Total P&L</td>
                <td class="{pnl_today_class}">{pnl_today}</td>
                <td class="{pnl_7d_class}">{pnl_7d}</td>
                <td class="{pnl_30d_class}">{pnl_30d}</td>
            </tr>
        </table>
    </div>

    <!-- MODEL HEALTH -->
    <div class="section">
        <h2>🏥 Model Health</h2>
        <table>
            <tr>
                <td>Brier Score (30d)</td>
                <td class="{brier_class}">{brier_score}</td>
                <td>{brier_status}</td>
            </tr>
            <tr>
                <td>Win Rate (50 trades)</td>
                <td class="{winrate_50_class}">{winrate_50}%</td>
                <td>{winrate_status}</td>
            </tr>
            <tr>
                <td>Current Streak</td>
                <td class="{streak_class}">{streak_type} {streak_count}</td>
                <td>{streak_status}</td>
            </tr>
            <tr>
                <td>Model Status</td>
                <td colspan="2" class="{model_status_class}">{model_status}</td>
            </tr>
        </table>
    </div>

    <!-- TOP PERFORMING CATEGORIES -->
    <div class="section">
        <h2>🎯 Category Performance</h2>
        <table>
            <tr>
                <th>Category</th>
                <th>Trades</th>
                <th>Win Rate</th>
                <th>P&L</th>
                <th>Trend</th>
            </tr>
            {category_rows}
        </table>
        <p style="font-size: 12px; color: #888;">💡 Consider increasing exposure to outperforming categories</p>
    </div>

    <!-- OUTSTANDING POSITIONS -->
    <div class="section">
        <h2>📋 Open Positions ({outstanding_count})</h2>
        <table>
            <tr>
                <th>Market</th>
                <th>Side</th>
                <th>Entry</th>
                <th>Current</th>
                <th>Unrealized</th>
            </tr>
            {positions_rows}
        </table>
        <p><strong>Total Outstanding:</strong> {total_outstanding}</p>
    </div>

    <!-- ERRORS & WARNINGS -->
    {errors_section}

    <!-- LONGSHOT PORTFOLIO -->
    <div class="section">
        <h2>🎰 Longshot Portfolio</h2>
        <table>
            <tr>
                <td>Active Longshots</td>
                <td>{active_longshots}</td>
            </tr>
            <tr>
                <td>Longshot Exposure</td>
                <td>{longshot_exposure}</td>
            </tr>
            <tr>
                <td>Longshot P&L (All Time)</td>
                <td class="{longshot_pnl_class}">{longshot_pnl}</td>
            </tr>
            <tr>
                <td>Best Longshot Win</td>
                <td class="stat-positive">{best_longshot}</td>
            </tr>
        </table>
    </div>

    <!-- FOOTER -->
    <div style="padding: 20px; text-align: center; color: #666; font-size: 12px;">
        <p>Generated by Kalshi Trading System v1.1</p>
        <p>Mode: {trading_mode} | Last Updated: {timestamp}</p>
    </div>
</body>
</html>
```

---

## Weekly Performance Report

**Subject:** 📈 Weekly Performance Report - Week of {week_start} | ROI: {weekly_roi}%

Additional sections beyond daily:
- Week-over-week comparison
- Best/worst trades of the week
- ML model feature importance changes
- Arbitrage opportunities detected vs executed
- News sentiment correlation with outcomes
- Recommendations for next week

---

## Critical Alert Templates

### 🚨 Model Drift Detected

**Subject:** 🚨 CRITICAL: Model Drift Detected - Trading Paused

```
⚠️ CRITICAL ALERT - MODEL DRIFT DETECTED

The trading model has been automatically PAUSED due to performance degradation.

TRIGGER: {trigger_reason}
- Brier Score: {brier_score} (threshold: 0.30)
- Win Rate: {win_rate}% over {n_trades} trades (threshold: 45%)
- Consecutive Losses: {loss_streak}

RECOMMENDED ACTIONS:
1. Review recent market conditions
2. Check for data quality issues
3. Consider model retraining
4. Manual approval required to resume

Dashboard: {dashboard_url}
```

### ⚠️ Warning - Performance Degradation

**Subject:** ⚠️ Warning: Performance Below Target - Position Sizes Reduced

```
⚠️ WARNING - PERFORMANCE DEGRADATION

Trading continues with REDUCED position sizes (50%).

METRICS:
- Brier Score: {brier_score} (warning at 0.25)
- Win Rate: {win_rate}%
- Streak: {streak_type} {streak_count}

This warning auto-clears when:
- Brier score < 0.25 over 20 trades
- Win rate > 52% over 30 trades

No action required unless condition persists >48 hours.
```

### 💥 Large Loss Alert

**Subject:** 💥 Alert: Large Loss - ${loss_amount} on {market_ticker}

```
💥 LARGE LOSS ALERT

A significant loss was recorded:

Market: {market_title}
Ticker: {market_ticker}
Side: {side}
Entry: ${entry_price}
Exit: ${exit_price}
Loss: ${loss_amount} ({loss_percent}%)

Portfolio Impact: {portfolio_impact}%

Review recommended but no automatic action taken.
```

### ✅ Loss Streak Recovery

**Subject:** ✅ Recovery: Loss Streak Ended - Back to Normal Operations

```
✅ RECOVERY CONFIRMED

The previous loss streak has ended.

Previous Streak: {streak_length} consecutive losses
Recovery Trade: {recovery_market}
Current Status: NORMAL OPERATIONS

Model health metrics have returned to acceptable levels.
```

### 🎯 Longshot Win Alert

**Subject:** 🎯 WINNER: Longshot Paid Off! +${profit} ({roi}% ROI)

```
🎯 LONGSHOT WIN!

One of your longshot bets has paid off:

Market: {market_title}
Entry: ${entry_price}
Resolution: YES @ $1.00
Profit: +${profit} ({roi}% ROI!)

Longshot Portfolio Update:
- Total Longshot P&L: ${total_longshot_pnl}
- Longshots Active: {active_count}
- Hit Rate: {hit_rate}%

This win covers approximately {losses_covered} losing longshots.
```

---

## Alert Triggers Summary

| Alert Type | Trigger | Severity | Auto-Action |
|------------|---------|----------|-------------|
| Model Drift | Brier > 0.35 OR Win Rate < 40% | 🚨 Critical | Pause trading |
| Performance Warning | Brier > 0.28 OR Win Rate < 48% | ⚠️ Warning | Reduce positions 50% |
| Loss Streak | 6+ consecutive losses | ⚠️ Warning | None (notify only) |
| Large Loss | Single loss > 10% portfolio | ⚠️ Warning | None (notify only) |
| System Error | API failure, scheduler crash | 🚨 Critical | Pause trading |
| Recovery | Metrics return to normal | ✅ Info | Resume normal |
| Longshot Win | Any longshot resolves YES | 🎯 Info | None (celebrate!) |

---

## Email Configuration

```python
EMAIL_CONFIG = {
    "recipient": "you@example.com",
    "sender": "you@example.com",  # Via Gmail API
    "daily_summary_time": "19:00",     # 7pm Eastern
    "timezone": "America/New_York",
    "weekly_report_day": "sunday",
    "longshot_wins": "batch_daily",     # Include in daily summary
    "critical_alerts": "immediate",     # Send right away (model drift, errors)
}
```

## Notification Channels

| Channel | Use Case |
|---------|----------|
| **Email** | Daily/Weekly summaries, all alerts |
| **SMS (future)** | Critical alerts only |
| **Dashboard Banner** | Real-time status |
| **Push (future)** | Mobile app alerts |

---

*Last Updated: January 2025*
*Author: Claude (PM) + User Requirements*
