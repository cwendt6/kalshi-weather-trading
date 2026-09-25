# Kalshi Trading Bot — Technical Roadmap
## Updated: February 2026

---

## Architecture Philosophy
**Inputs (data) → deterministic logic → orders via API.**
ML is a probability refiner, not a decision maker.
Edge = (Better probability × discipline × execution speed)

---

## PHASE 0: Foundation (DONE)
Core trading loop, data collection, paper trading infrastructure.

- [x] Kalshi API integration (auth, orders, market data)
- [x] SQLite database (markets, prices, trades, positions, forecasts)
- [x] Paper trading executor with $10K simulated bankroll
- [x] Price collection with tiered filtering (20K → ~200 markets)
- [x] 10 data API connectors (CoinGecko, FRED, BLS, OddsAPI, NWS, Polymarket, Metaculus, Dome, API Ninjas, Census)
- [x] Risk manager (Kelly sizing, daily loss limits, exposure caps)
- [x] Signal generator with edge-based trading
- [x] Straddle arbitrage scanner (YES + NO > $1)
- [x] Impossible event scanner
- [x] Velocity scanner (fast-moving markets)
- [x] Weather strategy, sports odds strategy, midrange strategy
- [x] Position manager with exit ladder
- [x] LLM forecaster (built, now disabled — too slow/expensive)

---

## PHASE 1: Math-Based Probability Engine (CURRENT)
Kill LLM dependency. Replace with deterministic, instant probability calculations.

- [x] Disable LLM forecaster (was $1/cycle, 13 min per cycle)
- [x] ProbabilityEngine with market classification and routing
- [x] WeatherCalculator — NWS forecast + normal distribution CDF
- [x] CryptoCalculator — log-normal model with CoinGecko volatility
- [x] SportsCalculator — sportsbook consensus (vig-removed implied odds)
- [x] EconomicsCalculator — FRED/BLS data + historical surprise distribution
- [x] CrossMarketCalculator — Polymarket/Metaculus/Dome implied probabilities
- [x] Confidence-weighted ensemble (domain calcs 2x weight, price anchor 0.3x)
- [x] Integration into main.py trading loop (5-min cycle, zero API cost)
- [x] Market coverage expansion (3 tiers, volume ≥ 0, 14-day horizon)

---

## PHASE 2: ML Training Data Pipeline (CURRENT)
Every forecast is a future training sample. Collect clean data now.

- [x] ForecastDB expanded: features_json, domain, resolution, brier_score
- [x] Meta-features stored: spread, volume, OI, hours_to_expiry, market_age
- [x] Calibration bucket assignment (nearest 0.05 for tracking)
- [x] Resolution backfill system (matches resolved markets → forecast outcomes)
- [x] Calibration tracker (Brier score, log loss, per-bucket accuracy)
- [x] "Don't trade" filter (spread, liquidity, time-to-expiry, extreme prices)
- [x] Migration script for existing database (scripts/migrate_ml_columns.py)
- [ ] Run migration on production database
- [ ] Verify bot runs clean fast cycles with full probability engine
- [ ] Accumulate 200-300 resolved forecasts with features

---

## PHASE 3: Structured ML — Logistic Regression (NEXT — needs 200-300 samples)
First ML model: simple, interpretable, hard to overfit.

- [ ] Feature extraction pipeline: ForecastDB → training DataFrame
- [ ] Target: y ∈ {0, 1} (event outcome), not market price
- [ ] Walk-forward time-split validation (never random split)
- [ ] Logistic regression with L2 regularization
- [ ] Per-domain models (weather, crypto, sports, economics)
- [ ] Optimize for log loss / Brier score (not accuracy)
- [ ] Platt scaling or isotonic regression for calibration
- [ ] Feature importance analysis (which features drive edge?)
- [ ] A/B: compare ML-adjusted prob vs math-only prob
- [ ] Only deploy if calibration improves over math-only baseline

---

## PHASE 4: Gradient Boosting (needs 500+ samples per domain)
Detect nonlinear interactions that logistic regression misses.

- [ ] XGBoost or LightGBM per domain
- [ ] Additional features: price velocity, order book imbalance, time-of-day
- [ ] Ensemble: blend logistic regression + gradient boosting
- [ ] SHAP values for trade explainability (Point 12: explain every trade)
- [ ] Bootstrap resampling for uncertainty estimation
- [ ] "Is this market worth trading?" classifier (Point 11)
- [ ] Walk-forward backtest with realistic slippage and partial fills
- [ ] Performance decay detection (auto-alert if Brier score degrades)

---

## PHASE 5: Advanced Models (needs 1000+ samples per domain)
Only if Phase 3-4 prove profitable and data volume justifies complexity.

- [ ] Random Forest for sanity-checking XGBoost predictions
- [ ] Stacking ensemble (multiple base models → meta-learner)
- [ ] Online learning (incremental updates as new data arrives)
- [ ] Kalman filter for time-series markets (crypto price levels)
- [ ] Bayesian neural networks for uncertainty quantification
- [ ] Sub-models per question format (thresholds vs ranges vs brackets)

---

## ONGOING: Infrastructure & Monitoring

### Data Quality
- [ ] Stale data detection (alert if any API hasn't updated in >2x expected interval)
- [ ] Feature distribution monitoring (detect regime changes)
- [ ] Automated data validation on every collection cycle

### Risk & Execution
- [ ] True edge calculation everywhere (spread + fee adjusted, not gross)
- [ ] Graceful shutdown (current issue: LLM didn't stop on Ctrl+C)
- [ ] Order fill tracking and slippage measurement
- [ ] Live trading mode (switch from paper when profitable)

### Backtesting
- [ ] Walk-forward backtest harness using historical PriceDB data
- [ ] Realistic slippage model (based on spread width)
- [ ] Partial fill simulation for thin markets
- [ ] Position size caps matching live constraints

### Calibration & Evaluation
- [ ] Daily calibration curve report (auto-generated)
- [ ] Per-domain Brier score tracking over time
- [ ] Calibration regression alerts (model getting worse?)
- [ ] Comparison dashboard: math-only vs ML-adjusted performance

### Market Coverage
- [ ] Political/election market calculator
- [ ] Climate/environment market calculator
- [ ] Entertainment/awards market calculator
- [ ] Financial derivatives (S&P 500 level) via options pricing models

---

## ML Rules of Engagement (from advisor review)

1. ML outputs ONE number: P(event). Everything else stays deterministic.
2. Target is event outcome y ∈ {0,1}, never market price.
3. Features > model choice. Invest in features first.
4. Boring models first. Don't move on until logistic regression is beaten.
5. Time splits only. Never random train/test split.
6. Log loss and Brier score are primary metrics. Not accuracy.
7. Force calibration after every training run.
8. Separate model confidence from trade confidence.
9. One model per market domain. Never mix weather + CPI + sports.
10. Backtest like a pessimist. Include slippage, partial fills, delays.
11. ML should also say "don't trade." Avoiding bad markets = alpha.
12. If you can't explain why the model likes a trade, don't take it.
13. Watch for: leakage, overfitting rare events, regime changes, thin markets.
14. ML is a probability refiner, not a decision maker.

---

## Data Sample Targets

| Domain | Current Samples | Logistic Regression | XGBoost | Advanced |
|--------|----------------|--------------------| --------|----------|
| Weather | ~0 (just started) | 200-300 | 500+ | 1000+ |
| Crypto | ~0 | 200-300 | 500+ | 1000+ |
| Sports | ~0 | 200-300 | 500+ | 1000+ |
| Economics | ~0 | 200-300 | 500+ | 1000+ |

At ~50-100 forecasts/day across all domains, Phase 3 readiness: ~1-2 weeks.
Phase 4 readiness: ~1-2 months.
