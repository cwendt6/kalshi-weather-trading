# Dynamic Weather Market Discovery

## Overview

The dynamic weather market discovery system replaces hardcoded weather series lists with API-driven discovery. This prevents phantom markets and ensures the system automatically adapts when Kalshi adds new weather markets.

### Problem Solved

The previous implementation had several limitations:

1. **Phantom Markets**: Fetching markets from series that no longer exist wastes API calls
2. **Missing New Markets**: When Kalshi adds new weather series, they must be manually added to the code
3. **Stale Data**: Markets that have expired are still queried until manually removed
4. **Manual Maintenance**: Updates required code changes and redeployment

### Solution

The new `WeatherMarketDiscovery` class dynamically queries the Kalshi API to find available weather series, then fetches markets from those series only. It uses intelligent caching to minimize API overhead while staying current.

## Architecture

### Components

```
WeatherMarketDiscovery
├── discover_weather_series()     # Query API for available series
├── get_active_weather_markets()  # Fetch markets from discovered series
├── classify_market()             # Identify market type (daily high, monthly rain, etc.)
└── Cache management              # 15-minute TTL with fallback to known list
```

### Weather Series Patterns

The system recognizes these market patterns:

| Market Type | Series Pattern | Example Market | Regex Pattern |
|-------------|---------------|-----------------|---------------|
| Daily High Temp | `KXHIGH{CITY}` | `KXHIGHNY-26FEB10-B36.5` | `^KXHIGH[A-Z]{2,10}$` |
| Daily Low Temp | `KXLOW{CITY}` or `KXLOWT{CITY}` | `KXLOWTNYC-26FEB10-T14` | `^KXLOWT?[A-Z]{2,10}$` |
| Daily Rain | `KXRAIN{CITY}` | `KXRAINNYC-26FEB10-T0` | `^KXRAIN[A-Z]{2,10}$` (no M) |
| Daily Snow | `KXSNOW{CITY}` | `KXSNOWNY-26FEB10-T0` | `^KXSNOW[A-Z]{2,10}$` (no M) |
| Monthly Rain | `KXRAIN{CITY}M` | `KXRAINNYCM-26FEB-3` | `^KXRAIN[A-Z]{2,10}M$` |
| Monthly Snow | `KX{CITY}SNOWM` | `KXBOSSNOWM-26FEB-12.0` | `^KX[A-Z]{2,10}SNOWM$` |

## Usage

### Basic Discovery

```python
from src.data.weather_market_discovery import get_weather_market_discovery
from src.api.kalshi_client import KalshiClient

discovery = get_weather_market_discovery()

async with KalshiClient() as client:
    # Discover available weather series
    series = await discovery.discover_weather_series(client)
    print(f"Found {len(series)} weather series")

    # Get all active weather markets
    markets = await discovery.get_active_weather_markets(client)
    print(f"Found {len(markets)} active weather markets")
```

### Market Classification

```python
# Check if a ticker is a weather market
is_weather = discovery.is_weather_market("KXHIGHNY-26FEB10-B36.5")
# Returns: True

# Get the market type
market_type = discovery.classify_market("KXRAINNYCM-26FEB-3")
# Returns: WeatherMarketType.MONTHLY_RAIN
```

### Caching

The system caches discovered series for 15 minutes by default:

```python
# Use cached results (up to 15 min old)
series = await discovery.discover_weather_series(client, use_cache=True)

# Force fresh API query
series = await discovery.discover_weather_series(client, use_cache=False)

# Clear cache manually
discovery.clear_cache()
```

## Integration with Collector

The `MarketDataCollector` now uses dynamic discovery in two places:

### 1. Weather Markets Fetch

```python
async def _fetch_weather_markets(self, client: KalshiClient) -> List[Market]:
    """Fetch weather markets using dynamic discovery instead of hardcoded list."""
    discovery = get_weather_market_discovery()
    weather_series = await discovery.discover_weather_series(client, use_cache=True)
    # ... fetch markets from discovered series
```

### 2. Discovery Loop

The collection loop now runs discovery more frequently:

- **Weather-only mode**: Every 15 minutes (3 cycles at 5-min interval)
- **Normal mode**: Every 1 hour (12 cycles at 5-min interval)

With API caching, these calls have minimal overhead.

## Fallback Mechanism

If API discovery fails, the system falls back to a known list of 50 common weather series:

```python
KNOWN_WEATHER_SERIES = [
    "KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA",  # ... daily high temps
    "KXLOWNY", "KXLOWTNYC",                 # ... daily low temps
    "KXBOSSNOWM", "KXNYCSNOWM",             # ... monthly snow
    "KXRAINNYCM", "KXRAINLAM",              # ... monthly rain
]
```

This ensures the system continues functioning even if the API is temporarily unavailable.

## Performance

### API Usage

- **Series Discovery**: 1 API call every 15 minutes (cached)
- **Market Fetching**: Paginated by series (200 markets per page)
- **Rate Limiting**: Respects Kalshi's 20 reads/sec limit

### Optimization Strategies

1. **Caching**: 15-minute TTL on discovered series reduces API calls by ~96%
2. **Pagination**: Fetches up to 1000 markets per series efficiently
3. **Parallel Processing**: Uses async/await for concurrent market fetching
4. **Selective Queries**: Only fetches weather markets, not entire universe

## Testing

Run the test suite:

```bash
# Unit tests
pytest tests/unit/test_weather_market_discovery.py -v

# Integration test script
python scripts/test_weather_discovery.py
```

### Test Coverage

- Series pattern matching (all types)
- Market classification
- API discovery with pagination
- Cache behavior
- Fallback to known series
- Singleton pattern

## Comparison: Before vs After

### Before (Hardcoded Lists)

```python
WEATHER_SERIES_TICKERS = [
    "KXHIGHNY", "KXHIGHCHI", ...  # 50 hardcoded entries
]

async def _fetch_weather_markets(self, client):
    for series in self.WEATHER_SERIES_TICKERS:
        markets, _ = await client.get_markets(series_ticker=series)
```

**Issues**:
- Phantom markets from deleted series
- Manual updates required for new series
- Fixed list, no adaptation

### After (Dynamic Discovery)

```python
async def _fetch_weather_markets(self, client):
    discovery = get_weather_market_discovery()
    series = await discovery.discover_weather_series(client, use_cache=True)
    for series in series:
        markets, _ = await client.get_markets(series_ticker=series)
```

**Benefits**:
- Automatic discovery of available series
- No phantom markets
- Adapts to new markets automatically
- Cached for efficiency

## Example Output

```
Weather Series Discovery Test
======================================================================

1. Testing weather series pattern matching:
✓ KXHIGHNY        ->     1 (Daily high temp)
✓ KXLOWTNYC       ->     1 (Daily low temp)
✓ KXRAINNYCM      ->     1 (Monthly rain)
✓ KXNYCSNOWM      ->     1 (Monthly snow)
✓ KXMODEL         ->     0 (Non-weather)

2. Fallback known weather series:
Total known series: 50
  Daily High Temps: 15 series
  Daily Low Temps: 10 series
  Monthly Rain: 10 series
  Monthly Snow: 15 series

3. Cache behavior:
Cache TTL: 900 seconds
Successfully discovered 50 weather series from API
```

## Implementation Details

### Files Modified

1. **Created**: `/src/data/weather_market_discovery.py`
   - `WeatherMarketDiscovery` class
   - Pattern matching and classification logic
   - API discovery and caching

2. **Updated**: `/src/data/collector.py`
   - Import `get_weather_market_discovery()`
   - Modified `_fetch_weather_markets()` to use discovery
   - Modified `_discover_weather_markets()` to use discovery
   - Updated `_markets_collection_loop()` discovery interval
   - Removed hardcoded `WEATHER_SERIES_TICKERS` list

3. **Created**: `/tests/unit/test_weather_market_discovery.py`
   - 20 unit tests covering all functionality
   - Mocked API calls for isolated testing

## Future Enhancements

1. **ML-Based Classification**: Use market titles to identify new weather series
2. **Confidence Scoring**: Rate how likely a series is to be weather-related
3. **Historical Tracking**: Log which series were discovered when (for trend analysis)
4. **Alert System**: Notify when new weather series appear
5. **Regional Expansion**: Support international weather markets as Kalshi expands

## References

- **Kalshi API**: Markets endpoint supports `series_ticker` filtering
- **Market Patterns**: Based on actual Kalshi data as of Feb 2026
- **Weather Standards**: NWS settlement data for temperature/precipitation
