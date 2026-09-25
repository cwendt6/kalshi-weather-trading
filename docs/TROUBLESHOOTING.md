# Troubleshooting Guide

## Common Issues and Solutions

This guide covers frequently encountered problems and their solutions.

---

## Quick Diagnostic Checklist

Before diving into specific issues:

- [ ] Is the dashboard loading? (`http://localhost:8501`)
- [ ] Is the API connected? (Green indicator)
- [ ] Are there recent error logs? (`tail -f logs/error.log`)
- [ ] Is the database accessible? (`sqlite3 kalshi_trading.db ".tables"`)
- [ ] Is the trading engine running? (`pgrep -f "python main.py"`)

---

## Issue Categories

1. [Dashboard Issues](#dashboard-issues)
2. [API Connection Issues](#api-connection-issues)
3. [Database Issues](#database-issues)
4. [Trading Issues](#trading-issues)
5. [Strategy Issues](#strategy-issues)
6. [Performance Issues](#performance-issues)

---

## Dashboard Issues

### Dashboard Won't Load

**Symptoms:**
- Browser shows "This site can't be reached"
- Streamlit error on startup

**Solutions:**

1. **Check if Streamlit is running:**
   ```bash
   pgrep -f streamlit
   ```

2. **Restart the dashboard:**
   ```bash
   pkill -f streamlit
   streamlit run app.py --server.port 8501
   ```

3. **Check for port conflicts:**
   ```bash
   lsof -i :8501
   # If something is using the port, kill it or use a different port
   streamlit run app.py --server.port 8502
   ```

4. **Check Python environment:**
   ```bash
   which python
   python --version
   pip list | grep streamlit
   ```

---

### Dashboard Shows Stale Data

**Symptoms:**
- Data doesn't update
- Last scan time is old
- Charts don't refresh

**Solutions:**

1. **Hard refresh the browser:**
   - Chrome: `Ctrl+Shift+R` (Windows) or `Cmd+Shift+R` (Mac)

2. **Check if trading engine is running:**
   ```bash
   pgrep -f "python main.py"
   ```

3. **Check database connection:**
   ```bash
   sqlite3 kalshi_trading.db "SELECT MAX(timestamp) FROM prices;"
   ```

4. **Restart the dashboard:**
   ```bash
   pkill -f streamlit
   streamlit run app.py
   ```

---

### Live Signals Not Updating

**Symptoms:**
- Live Signals page shows 0 signals
- Signal count doesn't change

**Solutions:**

1. **Check signal generator parameters:**
   - Default edge threshold might be too high
   - Lower to 2-3% in dashboard settings

2. **Verify markets are being scanned:**
   ```bash
   grep "scan" logs/kalshi.log | tail -20
   ```

3. **Check if forecasts exist:**
   ```sql
   SELECT COUNT(*) FROM forecasts WHERE timestamp > datetime('now', '-1 hour');
   ```

4. **Verify singleton is updating (known fix):**
   - The signal generator now creates new instances when parameters change
   - This was fixed - if still broken, check `src/strategy/signal_generator.py`

---

## API Connection Issues

### API Shows Disconnected

**Symptoms:**
- Red "Disconnected" indicator
- "Authentication failed" errors
- HTTP 401/403 errors

**Solutions:**

1. **Check credentials in `.env`:**
   ```bash
   cat .env | grep KALSHI
   ```

2. **Verify private key exists:**
   ```bash
   ls -la keys/
   cat keys/kalshi_private_key.pem | head -5
   ```

3. **Test API manually:**
   ```python
   from src.api.kalshi_client import get_kalshi_client
   client = get_kalshi_client()
   print(client.get_balance())
   ```

4. **Check Kalshi status:**
   - Visit https://status.kalshi.com/
   - Check for API outages

5. **Regenerate API keys:**
   - Log into Kalshi dashboard
   - Go to API settings
   - Generate new keys
   - Update `.env` and key file

---

### Rate Limiting (429 Errors)

**Symptoms:**
- "429 Too Many Requests" errors
- API calls failing intermittently

**Solutions:**

1. **Add delays between requests:**
   ```python
   # In src/api/kalshi_client.py
   import time
   time.sleep(0.1)  # 100ms between requests
   ```

2. **Check for runaway loops:**
   ```bash
   grep "request" logs/kalshi.log | wc -l
   ```

3. **Reduce scan frequency:**
   ```python
   # In settings
   SCAN_INTERVAL_SECONDS = 120  # 2 minutes instead of 30 seconds
   ```

---

### API Timeout Errors

**Symptoms:**
- "Connection timeout" errors
- Requests hanging

**Solutions:**

1. **Check internet connection:**
   ```bash
   ping api.kalshi.com
   ```

2. **Increase timeout settings:**
   ```python
   # In kalshi_client.py
   timeout = httpx.Timeout(30.0)  # 30 second timeout
   ```

3. **Check for network issues:**
   ```bash
   traceroute api.kalshi.com
   ```

---

## Database Issues

### "Database is Locked" Error

**Symptoms:**
- SQLite locked errors
- Operations timing out
- Multiple processes fighting for access

**Solutions:**

1. **Kill competing processes:**
   ```bash
   pkill -f "python main.py"
   pkill -f "streamlit"
   ```

2. **Enable WAL mode:**
   ```bash
   sqlite3 kalshi_trading.db "PRAGMA journal_mode=WAL;"
   ```

3. **Set busy timeout:**
   ```python
   # In database.py
   engine = create_engine(
       "sqlite:///kalshi_trading.db",
       connect_args={"timeout": 30}
   )
   ```

4. **Check for stuck transactions:**
   ```bash
   sqlite3 kalshi_trading.db ".timeout 5000"
   ```

---

### Database Corruption

**Symptoms:**
- "database disk image is malformed"
- Queries returning unexpected results
- Missing data

**Solutions:**

1. **Check integrity:**
   ```bash
   sqlite3 kalshi_trading.db "PRAGMA integrity_check;"
   ```

2. **Attempt repair:**
   ```bash
   sqlite3 kalshi_trading.db ".dump" > backup.sql
   mv kalshi_trading.db kalshi_trading_corrupted.db
   sqlite3 kalshi_trading.db < backup.sql
   ```

3. **Restore from backup:**
   ```bash
   cp backups/kalshi_trading_LATEST.db kalshi_trading.db
   ```

---

### Volume Data is NULL

**Symptoms:**
- Volume column shows NULL/0 for all markets
- Volume filters not working

**Solutions:**

This was a known issue - volume field wasn't mapping correctly from API.

**Fix Applied:**
```python
# In src/api/models.py - Add AliasChoices for volume
volume: Optional[int] = Field(
    default=0,
    validation_alias=AliasChoices("volume", "volume_24h", "total_volume")
)
```

**Verify fix:**
```sql
SELECT ticker, volume FROM markets WHERE volume > 0 LIMIT 10;
```

---

## Trading Issues

### No Trades Being Placed

**Symptoms:**
- Paper trades not executing
- Trade history empty
- Signals showing but no execution

**Solutions:**

1. **Check if auto-execute is enabled:**
   ```python
   # In settings
   AUTO_EXECUTE = True  # Must be True for auto trading
   ```

2. **Check risk manager:**
   - Daily loss limit might be hit
   - Position limits might be reached
   ```sql
   SELECT SUM(pnl) FROM trades WHERE DATE(timestamp) = DATE('now');
   ```

3. **Check signal approval:**
   - Signals must be `risk_approved = True`
   ```python
   signals = generator.get_executable_signals()
   for s in signals:
       print(f"{s.ticker}: approved={s.risk_approved}")
   ```

4. **Verify paper trading mode:**
   ```python
   # In .env
   PAPER_TRADING=true
   ```

---

### P&L Showing Negative Despite Wins

**Symptoms:**
- High win rate but negative P&L
- More winning trades than losing
- Confusion about profitability

**Cause:** Kalshi 2% winner fee not properly accounted for.

**Solution:**
This was the **critical bug** we fixed. Trades at 98-100¢ NO price lose money to fees.

**Verify fix is in place:**
```python
# In impossible_scanner.py
MAX_NO_PRICE = 0.96  # Must be 0.96, not 0.99 or 1.00
```

**Recalculate P&L with fees:**
```sql
SELECT
    SUM(CASE WHEN pnl > 0 THEN pnl - 0.02 ELSE pnl END) as net_pnl
FROM trades
WHERE status = 'resolved';
```

---

### Trades Not Resolving

**Symptoms:**
- Trades stuck in "pending" status
- Resolution tracker not updating
- Old trades never completed

**Solutions:**

1. **Run manual resolution check:**
   ```python
   from src.trading.resolution_tracker import check_resolutions
   check_resolutions()
   ```

2. **Check if market has settled:**
   ```sql
   SELECT ticker, status, result FROM markets WHERE ticker = 'KXNFL-...';
   ```

3. **Verify resolution tracker is running:**
   ```bash
   grep "resolution" logs/kalshi.log | tail -20
   ```

4. **Manually resolve stuck trades:**
   ```sql
   UPDATE trades
   SET status = 'resolved', pnl = 0.02
   WHERE ticker = 'XXX' AND status = 'pending';
   ```

---

## Strategy Issues

### Impossible Scanner Finding 0 Opportunities

**Symptoms:**
- Scanner runs but finds nothing
- Expected markets not appearing

**Solutions:**

1. **Lower thresholds temporarily:**
   ```python
   MIN_NO_PRICE = 0.90  # Temporarily lower to see if markets exist
   ```

2. **Check market categories:**
   ```sql
   SELECT DISTINCT category FROM markets WHERE status = 'active';
   ```

3. **Verify sports markets are included:**
   ```python
   SPORTS_PATTERNS = ["KXNFL", "KXNBA", "KXMLB", "KXNHL"]
   ```

4. **Check for stale market data:**
   ```sql
   SELECT MAX(timestamp) FROM prices;
   ```

---

### Arbitrage Scanner Showing 0 Results

**Symptoms:**
- Arbitrage page empty
- No straddle opportunities

**Causes & Solutions:**

1. **Volume filter too high:**
   ```python
   # Changed from 1000 to 0
   min_volume = 0
   ```

2. **NULL volume handling:**
   ```python
   # Add NULL coalesce
   func.coalesce(MarketDB.volume, 0)
   ```

3. **True arbitrage is rare:**
   - Markets are efficient
   - Opportunities exist briefly
   - May need to check more frequently

---

### Weather Strategy Not Generating Signals

**Symptoms:**
- Weather page empty
- No forecasts being created

**Solutions:**

1. **Verify NWS API is working:**
   ```python
   from src.data_sources.nws_weather import NWSClient
   client = NWSClient()
   forecast = client.get_forecast("NYC", date.today())
   print(forecast)
   ```

2. **Check for temperature markets:**
   ```sql
   SELECT * FROM markets WHERE ticker LIKE '%TEMP%' AND status = 'active';
   ```

3. **Verify station mapping:**
   - NYC should use KJFK, not city center
   - Check `KALSHI_STATIONS` in nws_weather.py

---

## Performance Issues

### System Running Slowly

**Symptoms:**
- Dashboard takes long to load
- Scans taking too long
- High CPU/memory usage

**Solutions:**

1. **Check database size:**
   ```bash
   ls -lh kalshi_trading.db
   ```

2. **Clean old data:**
   ```sql
   DELETE FROM prices WHERE timestamp < datetime('now', '-30 days');
   VACUUM;
   ```

3. **Add database indexes:**
   ```sql
   CREATE INDEX IF NOT EXISTS idx_prices_ticker ON prices(ticker);
   CREATE INDEX IF NOT EXISTS idx_prices_timestamp ON prices(timestamp);
   ```

4. **Check memory usage:**
   ```bash
   ps aux | grep python | awk '{print $4, $11}'
   ```

---

### High Memory Usage

**Symptoms:**
- Python using >1GB RAM
- System swapping
- Crashes with memory errors

**Solutions:**

1. **Reduce data retention:**
   ```python
   # Keep fewer price points in memory
   MAX_PRICE_HISTORY = 1000  # Not unlimited
   ```

2. **Clear caches:**
   ```python
   # Reset singleton instances
   _signal_generator = None
   _scanner = None
   ```

3. **Restart periodically:**
   ```bash
   # Add to crontab
   0 4 * * * pkill -f "python main.py" && python main.py
   ```

---

## Error Messages Reference

### Common Error Messages

| Error | Meaning | Solution |
|-------|---------|----------|
| `401 Unauthorized` | API auth failed | Check credentials |
| `429 Too Many Requests` | Rate limited | Slow down requests |
| `database is locked` | DB contention | Enable WAL mode |
| `Connection refused` | Service not running | Start the service |
| `No module named X` | Missing dependency | `pip install X` |
| `KeyError: 'volume'` | API response changed | Update field mapping |
| `division by zero` | Missing data | Add null checks |

---

## Getting Help

### Collecting Debug Information

Before asking for help, gather:

1. **Error logs:**
   ```bash
   tail -100 logs/error.log > debug_error.log
   ```

2. **Recent activity:**
   ```bash
   tail -500 logs/kalshi.log > debug_activity.log
   ```

3. **System state:**
   ```bash
   python --version
   pip freeze > debug_requirements.txt
   ```

4. **Database state:**
   ```sql
   SELECT 'trades', COUNT(*) FROM trades
   UNION SELECT 'markets', COUNT(*) FROM markets
   UNION SELECT 'prices', COUNT(*) FROM prices;
   ```

### Debug Mode

Enable verbose logging:

```python
# In config/settings.py
LOG_LEVEL = "DEBUG"
```

Or via environment:
```bash
LOG_LEVEL=DEBUG python main.py
```

---

## Prevention Checklist

### Before Going Live

- [ ] All strategies tested in paper mode
- [ ] Fee calculations verified
- [ ] Database backed up
- [ ] Monitoring alerts configured
- [ ] Emergency stop procedure tested
- [ ] API credentials secured
- [ ] Loss limits set appropriately

### Regular Maintenance

- [ ] Weekly: Review logs for errors
- [ ] Weekly: Backup database
- [ ] Monthly: Clean old data
- [ ] Monthly: Review strategy performance
- [ ] Quarterly: Update dependencies

---

*Last Updated: February 2026*
