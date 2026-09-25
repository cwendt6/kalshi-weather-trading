# Operations Runbook

## Daily Operations Guide for Kalshi Trading System

This runbook covers day-to-day operations, monitoring procedures, and standard operating procedures for the trading system.

---

## Quick Start

### Starting the System

```bash
# Navigate to project directory
cd /path/to/kalshi-trading-system

# Activate virtual environment
source venv/bin/activate

# Start the dashboard
streamlit run app.py --server.port 8501

# In a separate terminal, start the trading engine
python main.py
```

### Stopping the System

```bash
# Stop trading (graceful)
# Use the STOP button in dashboard, or:
kill -SIGTERM $(pgrep -f "python main.py")

# Stop dashboard
Ctrl+C in the terminal running streamlit
```

---

## Daily Checklist

### Morning (Before Market Open)

- [ ] Check system health at `http://localhost:8501`
- [ ] Verify API connection (green indicator)
- [ ] Review overnight P&L
- [ ] Check for any error alerts
- [ ] Verify all strategies are enabled/disabled as intended
- [ ] Review daily loss limit (reset at midnight)
- [ ] Check Kalshi account balance

### Throughout the Day

- [ ] Monitor P&L every 2-4 hours
- [ ] Watch for unusual win/loss streaks
- [ ] Check for API errors in logs
- [ ] Verify trades are executing as expected

### Evening (After Market Close)

- [ ] Review daily performance
- [ ] Check resolved trades and P&L
- [ ] Note any issues for investigation
- [ ] Backup database if significant changes

---

## Dashboard Overview

### Accessing the Dashboard

**URL:** `http://localhost:8501`

### Key Sections

| Section | Purpose |
|---------|---------|
| **Summary** | Total P&L, win rate, active positions |
| **Live Signals** | Current trading opportunities |
| **Trade History** | Recent executed trades |
| **Markets** | Browsable market data |
| **Arbitrage** | Straddle opportunities |
| **Settings** | Strategy configuration |

### Important Metrics to Monitor

1. **Net P&L** - Your actual profit after fees
2. **Win Rate** - Should be >52% for most strategies
3. **Active Positions** - Current open trades
4. **API Status** - Must be green/connected
5. **Daily Loss** - Watch for approaching limits

---

## Strategy Operations

### Enabling/Disabling Strategies

**Via Dashboard:**
1. Go to Settings page
2. Toggle strategy switches
3. Click "Save Settings"

**Via Code:**
```python
# In config/settings.py
STRATEGIES_ENABLED = {
    "impossible_scanner": True,
    "weather_strategy": True,
    "straddle_arbitrage": True,
    "ml_signals": False,  # Disabled
}
```

### Adjusting Parameters

**Impossible Scanner:**
```python
# Lower volume for more trades
MIN_PROFIT_PCT = 0.04  # 4% gross minimum

# Position sizing
POSITION_SIZES = {
    "HIGH": 50.0,    # $50 for high confidence
    "MEDIUM": 25.0,  # $25 for medium
    "LOW": 10.0,     # $10 for low
}
```

**Weather Strategy:**
```python
MIN_EDGE_PCT = 0.10  # 10% edge required
```

**Signal Generator:**
```python
MIN_EDGE = 0.05       # 5% minimum edge
MIN_CONFIDENCE = 0.55  # 55% confidence
```

---

## Monitoring Procedures

### Health Checks

**Every Hour:**
1. Dashboard loads without errors
2. API status is connected
3. Last scan time is recent (<5 minutes)
4. No error banners displayed

**Every 4 Hours:**
1. P&L is reasonable (no sudden large losses)
2. Trade count is appropriate for strategy settings
3. Win rate is within expected range
4. Database is responding

**Daily:**
1. Full P&L review
2. Check logs for errors
3. Verify all strategies ran
4. Review any stopped/paused strategies

### Log Locations

```bash
# Application logs
tail -f logs/kalshi.log

# Error logs
tail -f logs/error.log

# Trade execution logs
tail -f logs/trades.log
```

### Log Severity Levels

| Level | Meaning | Action Required |
|-------|---------|-----------------|
| DEBUG | Verbose details | None |
| INFO | Normal operations | None |
| WARNING | Potential issue | Monitor |
| ERROR | Something failed | Investigate |
| CRITICAL | System failure | Immediate action |

---

## Common Operations

### Checking Current Positions

**Dashboard:** Navigate to "Trade History" → filter by "Open"

**CLI:**
```bash
python -c "from src.cli.commands import show_positions; show_positions()"
```

**Database:**
```sql
SELECT * FROM positions WHERE status = 'open';
```

### Viewing P&L

**Dashboard:** Top of Summary page shows Total P&L

**Breakdown:**
- Gross P&L = Sum of all trade profits (before fees)
- Fees = Sum of fees on winning trades
- Net P&L = Gross P&L - Fees

### Manual Trade Execution (Emergency)

If auto-trading is disabled but you see a good opportunity:

```python
from src.execution.executor import execute_trade

# Place a trade manually
result = execute_trade(
    ticker="KXNFL-SUPERBOWL-CHIEFS",
    side="no",
    quantity=10,
    price=96,  # cents
    paper_mode=True,  # Set False for live
)
print(result)
```

### Pausing All Trading

**Dashboard:** Click the red "STOP ALL TRADING" button

**CLI:**
```bash
# Create pause file
touch .trading_paused

# To resume, remove the file
rm .trading_paused
```

---

## Database Operations

### Location
```
./kalshi_trading.db
```

### Backup

```bash
# Manual backup
cp kalshi_trading.db backups/kalshi_trading_$(date +%Y%m%d_%H%M%S).db

# Verify backup
sqlite3 backups/kalshi_trading_*.db "SELECT COUNT(*) FROM trades;"
```

### Common Queries

```sql
-- Total P&L
SELECT SUM(pnl) as total_pnl FROM trades WHERE status = 'resolved';

-- Win rate
SELECT
    COUNT(CASE WHEN pnl > 0 THEN 1 END) * 100.0 / COUNT(*) as win_rate
FROM trades
WHERE status = 'resolved';

-- Recent trades
SELECT * FROM trades ORDER BY timestamp DESC LIMIT 20;

-- Trades by strategy
SELECT
    strategy,
    COUNT(*) as trades,
    SUM(pnl) as total_pnl,
    AVG(pnl) as avg_pnl
FROM trades
GROUP BY strategy;
```

### Database Maintenance

```bash
# Vacuum to reclaim space
sqlite3 kalshi_trading.db "VACUUM;"

# Check integrity
sqlite3 kalshi_trading.db "PRAGMA integrity_check;"

# Enable WAL mode (better concurrency)
sqlite3 kalshi_trading.db "PRAGMA journal_mode=WAL;"
```

---

## API Management

### Checking API Status

**Dashboard:** Look for green "Connected" indicator

**CLI:**
```python
from src.api.kalshi_client import get_kalshi_client
client = get_kalshi_client()
print(f"Connected: {client.is_connected()}")
print(f"Balance: ${client.get_balance()}")
```

### API Rate Limits

Kalshi has rate limits. Current settings:
- Max 10 requests/second
- 1000 requests/hour

If you see `429 Too Many Requests`:
1. Increase delay between requests
2. Check for runaway loops
3. Wait 1 minute and retry

### Refreshing API Credentials

If API authentication fails:

1. Check credentials in `.env` file
2. Verify API key hasn't expired
3. Re-generate keys in Kalshi dashboard if needed

```bash
# .env file
KALSHI_API_KEY=your_api_key_here
KALSHI_PRIVATE_KEY_PATH=./keys/kalshi_private_key.pem
```

---

## Incident Response

### Trading Losses Exceed Limit

**Symptom:** System auto-paused, alert displayed

**Response:**
1. Check "Why did we lose?" - review recent trades
2. Check for API errors or data issues
3. Check if market conditions changed
4. Decide: Resume trading or investigate further
5. If resuming: Click "Resume Trading" in dashboard

### API Connection Lost

**Symptom:** Red "Disconnected" indicator

**Response:**
1. Check internet connection
2. Check Kalshi status page
3. Check API credentials
4. Restart the trading engine
5. If persists: Contact Kalshi support

### Database Locked

**Symptom:** "database is locked" errors

**Response:**
1. Check for multiple processes accessing DB
2. Kill any stuck processes: `pkill -f "python main.py"`
3. Enable WAL mode: `sqlite3 kalshi_trading.db "PRAGMA journal_mode=WAL;"`
4. Restart system

### Unexpected Behavior

**Symptom:** Trades not matching expectations

**Response:**
1. Check logs for errors
2. Verify strategy parameters
3. Check if market data is stale
4. Review recent code changes
5. Run in paper mode to test

---

## Performance Monitoring

### Key Performance Indicators (KPIs)

| KPI | Target | Alert Threshold |
|-----|--------|-----------------|
| Win Rate | >55% | <50% |
| Daily P&L | >0 | <-5% of bankroll |
| Trade Count | 10-50/day | <5 or >100 |
| API Errors | 0/hour | >5/hour |
| Signal Quality | >0.6 | <0.4 |

### Tracking Performance

**Daily Review:**
```sql
SELECT
    DATE(timestamp) as date,
    COUNT(*) as trades,
    SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
    SUM(pnl) as daily_pnl
FROM trades
WHERE timestamp > datetime('now', '-7 days')
GROUP BY DATE(timestamp)
ORDER BY date DESC;
```

### Performance Degradation

If performance drops:
1. Check if market conditions changed
2. Review strategy parameters
3. Check for data quality issues
4. Consider retraining ML models
5. Review fee impact on recent trades

---

## Scheduled Tasks

### Automatic (Built-in)

| Task | Frequency | Description |
|------|-----------|-------------|
| Market Scan | 2 min | Scan for opportunities |
| Price Update | 1 min | Refresh market prices |
| Resolution Check | 5 min | Check for resolved markets |
| Health Check | 10 min | System health verification |
| Stats Update | 1 hour | Update dashboard stats |

### Manual (Recommended)

| Task | Frequency | Description |
|------|-----------|-------------|
| Log Review | Daily | Check for errors |
| DB Backup | Weekly | Backup database |
| Performance Review | Weekly | Analyze P&L trends |
| Strategy Review | Monthly | Adjust parameters |
| Full System Check | Monthly | Verify all components |

---

## Emergency Procedures

### Complete System Shutdown

```bash
# 1. Stop trading first
touch .trading_paused

# 2. Wait for open orders to complete (or cancel them)

# 3. Stop all processes
pkill -f "streamlit"
pkill -f "python main.py"

# 4. Backup database
cp kalshi_trading.db kalshi_trading_emergency_$(date +%Y%m%d).db
```

### Rollback to Previous State

```bash
# 1. Stop system
pkill -f "python main.py"

# 2. Restore database backup
cp backups/kalshi_trading_YYYYMMDD.db kalshi_trading.db

# 3. Restart
python main.py
```

### Contact Information

- **Kalshi Support:** support@kalshi.com
- **System Issues:** Check logs first, then escalate
- **API Issues:** Check Kalshi status page

---

## Appendix

### File Locations

```
kalshi-trading-system/
├── app.py                 # Dashboard entry point
├── main.py                # Trading engine entry point
├── kalshi_trading.db      # SQLite database
├── .env                   # Environment variables (secrets)
├── config/
│   └── settings.py        # Configuration
├── logs/
│   ├── kalshi.log         # Main log
│   ├── error.log          # Error log
│   └── trades.log         # Trade log
├── src/
│   ├── api/               # Kalshi API client
│   ├── strategy/          # Trading strategies
│   ├── execution/         # Order execution
│   └── data/              # Data management
└── docs/                  # Documentation
```

### Environment Variables

```bash
# Required
KALSHI_API_KEY=xxx
KALSHI_PRIVATE_KEY_PATH=./keys/private.pem

# Optional
PAPER_TRADING=true
LOG_LEVEL=INFO
DB_PATH=./kalshi_trading.db
```

### Useful Commands

```bash
# Check if system is running
pgrep -f "python main.py"

# View live logs
tail -f logs/kalshi.log

# Quick database query
sqlite3 kalshi_trading.db "SELECT COUNT(*) FROM trades;"

# Check disk space
df -h .

# Memory usage
ps aux | grep python
```

---

*Last Updated: February 2026*
