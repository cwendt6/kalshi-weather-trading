# Kalshi Weather Trading System

An autonomous trading system for [Kalshi](https://kalshi.com) weather prediction markets. Uses NWS forecast data, probability modeling, and disciplined risk management to identify and execute positive expected value trades on temperature, rain, and snow markets.

**Status:** not actively traded. Paper mode is the default; live mode needs an explicit flag and confirmation.

## How It Works

The system runs a continuous loop that scans weather markets every 10 minutes, compares NWS forecasts against market prices, and executes trades where the model finds positive directional edge. Exits are managed by a single deterministic engine (PositionReEvaluator) that monitors edge erosion, forecast shifts, and opportunity cost.

**Core strategy**: Buy NO on temperature brackets far from the NWS forecast (high win rate, reliable income) and selectively buy YES on brackets near the forecast when edge is strong (convergence plays).

## The model

1. **Forecast to probability.** Each temperature market is a bracket or threshold, such as "NYC high above 40°F". The NWS point forecast for the settlement station is the mean of a normal distribution. Its standard deviation depends on forecast lead time and time of day: it narrows as the observation window closes, with a 0.5°F floor. The chance of the bracket settling YES is the area under that curve.
2. **Edge after fees.** Edge = model probability minus market price, on the side being bought. Kalshi charges 2% of winnings, not of the payout, so fee drag is largest on cheap contracts. Trades must clear an edge floor, and profitability checks include the fee (see `src/utils/fees.py` and `docs/FEE_GUIDE.md`).
3. **Sizing.** Half-Kelly on the fee-adjusted edge, capped by per-position, per-city and per-event limits. The caps scale with bankroll phase (table below).
4. **Exits.** A single engine, `PositionReEvaluator`, re-scores every open position each cycle. It exits when the edge erodes, the forecast shifts, or a better use of the capital shows up. Limit sells rest at model EV, with an escalation path for illiquid markets.

## Prerequisites

- Python 3.10+
- [Kalshi account](https://kalshi.com) with API access
- Kalshi API key and private key file

## Quick Start

```bash
# Install (add ,dashboard for the Streamlit dashboard, ,reports for the Excel report)
pip install -e ".[dev]"

# Copy and configure environment
cp .env.example .env
# Edit .env with your KALSHI_API_KEY and KALSHI_PRIVATE_KEY_PATH

# Run in paper trading mode (default)
python -m src.main

# Run in live trading mode (requires confirmation)
python -m src.main --live
```

## Project Structure

```
src/
├── main.py                    # Main trading loop (weather-only)
├── strategy/
│   └── weather_strategy.py    # Edge detection & opportunity generation
├── execution/
│   ├── risk_manager.py        # Bankroll phases, daily loss limits, exposure caps
│   ├── position_sizer.py      # Half-Kelly position sizing
│   ├── position_reevaluator.py# Single exit engine (edge, opportunity cost, time decay)
│   ├── city_budget_allocator.py# Edge-weighted city budget allocation
│   ├── anti_loop_detector.py  # Prevents fee-burning re-entry cycles
│   ├── resting_order_manager.py# Limit sells at model EV, illiquid exit escalation
│   ├── executor.py            # Order execution (paper & live)
│   └── portfolio.py           # Position tracking
├── probability/
│   └── weather.py             # Normal distribution temperature probability
├── data_sources/
│   └── nws_weather.py         # NWS API with settlement station mapping
├── data/
│   ├── models.py              # SQLAlchemy models (trades, positions, markets)
│   ├── database.py            # DB connection management
│   └── collector.py           # Market data collection
├── analysis/
│   ├── edge.py                # Edge calculation
│   ├── calibration.py         # Forecast accuracy tracking
│   └── forecaster.py          # LLM probability estimation
├── analytics/
│   └── model_health.py        # Model health monitoring
├── monitoring/
│   └── system_monitor.py      # System health & alerting
├── api/
│   └── kalshi_client.py       # Kalshi API client
└── utils/
    ├── logging.py             # Loguru setup
    ├── alerts.py              # Discord/Slack alerts
    └── fees.py                # Kalshi fee calculations

scripts/
├── generate_performance_report.py  # Automated Excel report from logs + DB
config/
├── settings.py                # Pydantic settings from .env
data/
├── kalshi_trading.db          # SQLite database (trades, positions, markets)
```

## Risk Management

The system uses a bankroll phase system that dynamically adjusts risk limits:

| Phase | Bankroll | Max Position | Max Exposure | Daily Loss Limit | Max Positions |
|-------|----------|-------------|-------------|-----------------|---------------|
| Survival | < $300 | 10% | 60% | 8% | 5 |
| Acceleration | $300–$1000 | 15% | 75% | 10% | 8 |
| Scaling | $1000+ | 20% | 80% | 12% | 12 |

### Safety Gates (enforced before every trade)

1. **Edge Floor**: Minimum 5% positive directional edge
2. **Time Gate**: No entries < 2h to close; need > 10% edge within 4h
3. **Price Floor**: Minimum $0.03 contract price
4. **Rain/Snow Rules**: 12% minimum edge, monthly markets disabled
5. **YES Cap**: Maximum 2 simultaneous YES positions
6. **Anti-Loop**: Cooldown period + 5% edge increase required for re-entry

## Performance Tracking

Generate an automated Excel report from trading logs and the SQLite database:

```bash
python scripts/generate_performance_report.py
python scripts/generate_performance_report.py --days 7
python scripts/generate_performance_report.py --output my_report.xlsx
```

The report includes 6 sheets: Key Metrics, Daily Summary, By City, By Trade Type, Edge Analysis, and Trade Log.

## Configuration

All settings loaded from `.env` via pydantic-settings. Key variables:

- `KALSHI_API_KEY` / `KALSHI_PRIVATE_KEY_PATH`: API authentication
- `WEATHER_MAX_PER_EVENT`: Max dollars per weather event (default: $20)
- `WEATHER_MAX_YES_POSITIONS`: Max simultaneous YES positions (default: 2)
- `WEATHER_PORTFOLIO_PCT_PER_CITY`: Portfolio % allocated per city (default: 10%)
- `WEATHER_NO_MAX_CONTRACTS` / `WEATHER_YES_MAX_CONTRACTS`: Contract limits

## Testing

```bash
pip install -e ".[dev,dashboard]"
pytest tests/
```

Tests need no credentials or network access. `tests/conftest.py` sets throwaway keys and a temp SQLite database. CI runs the suite and a gitleaks secrets scan on every PR.

`tests/known_failures.txt` lists tests that lag behind recent strategy changes. They run as expected failures, so CI still flags any regression in the rest of the suite.

## Disclaimer

This software is for educational purposes only. Trading prediction markets involves significant financial risk. Past performance does not guarantee future results. Only trade with money you can afford to lose.
