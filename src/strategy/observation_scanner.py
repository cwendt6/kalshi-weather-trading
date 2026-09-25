"""
Observation-Settled Fast Scanner

Speed-based strategy for guaranteed-outcome trades on weather bracket markets.

When a weather station's observed temperature surpasses a bracket's bounds,
that bracket is mathematically dead — the outcome is 100% certain. This scanner
detects those dead brackets FAST (every 60 seconds) and buys NO before the
market fully adjusts.

Key principles:
- SPEED: Runs on its own 60-second loop, separate from the 2-min main scan
- ACCURACY: Uses NWS METAR data from actual settlement stations (KNYC, KMIA, etc.)
- FEE-AWARE: Only enters if guaranteed profit after Kalshi's 2% winner fee
- PRIORITY: Obs-settled opportunities skip the normal queue

How it works:
1. Poll latest NWS observations for each enabled city
2. Track running daily high/low from actual station readings
3. Compare against ALL same-day bracket markets for that city
4. If observed temp has surpassed a bracket → bracket is DEAD
5. Check if NO price allows profit after fees
6. Generate high-priority BUY_NO opportunity

Fee math (Kalshi charges 2% on WINNINGS, not payout):
  cost = NO price (e.g., 95¢)
  payout = 100¢ (guaranteed since bracket is dead)
  winnings = 100 - cost = 5¢
  fee = 2% × winnings = 0.1¢
  net_profit = winnings - fee = 4.9¢

  At 97¢: profit = 0.98 × 3 = 2.94¢ per contract
  At 98¢: profit = 0.98 × 2 = 1.96¢ per contract
  At 99¢: profit = 0.98 × 1 = 0.98¢ per contract ← still profitable!

  MAX_ENTRY = 97¢ (guarantees ≥2.94¢ profit per contract)
"""

import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Dict, List, Optional, Tuple

from src.data_sources.nws_weather import NWSClient, KALSHI_STATIONS, StationObservation, get_active_cities
from src.data.database import get_db_session
from src.data.models import MarketDB, PriceDB

from src.utils.logging import logger

# Kalshi fee: 2% of winnings (payout - cost)
KALSHI_FEE_RATE = 0.02

# Maximum NO price we'll pay (in cents). At 97¢:
# profit = 0.98 × (100 - 97) = 2.94¢ per contract
MAX_OBS_SETTLED_ENTRY_CENTS = int(os.environ.get("OBS_SCANNER_MAX_ENTRY", "97"))

# Minimum net profit per contract (in cents) to bother executing
MIN_PROFIT_PER_CONTRACT_CENTS = float(os.environ.get("OBS_SCANNER_MIN_PROFIT", "1.5"))

# Minimum contracts to buy per dead bracket
MIN_CONTRACTS = int(os.environ.get("OBS_SCANNER_MIN_CONTRACTS", "3"))

# Maximum contracts to buy per dead bracket
MAX_CONTRACTS = int(os.environ.get("OBS_SCANNER_MAX_CONTRACTS", "20"))

# Maximum total capital per obs-settled trade (dollars)
MAX_CAPITAL_PER_TRADE = float(os.environ.get("OBS_SCANNER_MAX_CAPITAL", "10.0"))


@dataclass
class ObsSettledOpportunity:
    """A guaranteed-outcome trading opportunity from live observation."""
    ticker: str
    city: str
    side: str                   # Always "no" for dead brackets
    bracket_lower: float        # Bracket lower bound (°F)
    bracket_upper: float        # Bracket upper bound (°F)
    observed_extreme: float     # Observed temp that killed the bracket
    no_price_cents: int         # Current NO ask price
    guaranteed_profit_cents: float  # Net profit per contract after fees
    max_contracts: int          # How many to buy (capital-limited)
    total_profit_cents: float   # Total expected profit
    reason: str                 # Human-readable explanation
    market_type: str = "temperature"  # "high" or "low"
    observation_settled: bool = True
    station_id: str = ""
    observation_time: Optional[datetime] = None


class ObservationSettledScanner:
    """
    Fast scanner for observation-settled bracket trades.

    Polls NWS station observations and identifies dead brackets
    where buying NO is a guaranteed profit.
    """

    # City code mapping (ticker city codes → KALSHI_STATIONS keys)
    CITY_CODES = {
        "NY": "NYC",
        "NYC": "NYC",
        "CHI": "CHICAGO",
        "MIA": "MIAMI",
        "AUS": "AUSTIN",
        "DEN": "DENVER",
        "ATL": "ATLANTA",
        "PHIL": "PHILADELPHIA",
        "SEA": "SEATTLE",
        "LA": "LOS_ANGELES",
        "LAX": "LOS_ANGELES",
        "BOS": "BOSTON",
        "DAL": "DALLAS",
        "HOU": "HOUSTON",
        "DC": "WASHINGTON_DC",
        "DET": "DETROIT",
        "SLC": "SALT_LAKE_CITY",
    }

    def __init__(self, enabled_cities: Optional[List[str]] = None, risk_manager=None):
        """
        Initialize the observation scanner.

        Args:
            enabled_cities: List of city names to scan. If None, uses env var
                           WEATHER_ENABLED_CITIES (default: NYC,MIAMI,AUSTIN,LOS_ANGELES,CHICAGO)
            risk_manager: Optional RiskManager for obs-settled pool sizing.
                         If None, falls back to MAX_CAPITAL_PER_TRADE.
        """
        if enabled_cities:
            self.enabled_cities = [c.upper() for c in enabled_cities]
        else:
            self.enabled_cities = get_active_cities()

        self.risk_manager = risk_manager
        self.nws_client = NWSClient(cache_ttl_minutes=15)  # Forecasts at 15 min
        self._last_scan_time: Optional[datetime] = None
        self._scan_count = 0

        # Track which tickers we've already traded to avoid duplicates
        self._traded_tickers: set = set()
        # Reset traded tickers daily
        self._traded_date: Optional[date] = None

        logger.info(
            f"🔭 ObservationSettledScanner initialized | "
            f"cities={self.enabled_cities} | "
            f"max_entry={MAX_OBS_SETTLED_ENTRY_CENTS}¢ | "
            f"min_profit={MIN_PROFIT_PER_CONTRACT_CENTS}¢/contract"
        )

    def _reset_daily_state(self) -> None:
        """Reset daily tracking state at midnight."""
        today = date.today()
        if self._traded_date != today:
            self._traded_tickers.clear()
            self._traded_date = today
            logger.info("🔭 Obs scanner: daily state reset")

    def _get_same_day_bracket_markets(self) -> Dict[str, List[dict]]:
        """
        Get all same-day bracket markets grouped by city.

        Queries MarketDB + latest PriceDB for today's temperature bracket markets.

        Returns:
            Dict[city_name, List[{ticker, threshold, bracket_lower, bracket_upper,
                                   market_type, yes_ask, no_ask, ...}]]
        """
        today = date.today()
        today_str = today.strftime("%y%b%d").upper()  # e.g., "26FEB13"

        markets_by_city: Dict[str, List[dict]] = {}

        try:
            with next(get_db_session()) as session:
                # Get all temperature bracket markets for today
                # Bracket tickers contain "-B" (e.g., KXHIGHMIA-26FEB13-B79.5)
                all_markets = session.query(MarketDB).filter(
                    MarketDB.ticker.like(f"KX%{today_str}%B%"),
                    MarketDB.status == "open",
                ).all()

                if not all_markets:
                    logger.debug("No same-day bracket markets found in DB")
                    return markets_by_city

                # Get latest prices for these markets
                tickers = [m.ticker for m in all_markets]
                latest_prices = {}
                for ticker in tickers:
                    price = session.query(PriceDB).filter(
                        PriceDB.ticker == ticker
                    ).order_by(PriceDB.timestamp.desc()).first()
                    if price:
                        latest_prices[ticker] = {
                            "yes_ask": price.yes_ask,
                            "yes_bid": price.yes_bid,
                            "no_ask": price.no_ask,
                            "no_bid": price.no_bid,
                        }

                # Parse and group by city
                for market in all_markets:
                    parsed = self._parse_bracket_ticker(market.ticker)
                    if not parsed:
                        continue

                    city = parsed["city"]
                    if city not in self.enabled_cities:
                        continue

                    price_data = latest_prices.get(market.ticker, {})

                    # Calculate NO ask price
                    # If no_ask is available, use it directly
                    # Otherwise derive from yes_bid: no_ask ≈ 100 - yes_bid
                    no_ask = price_data.get("no_ask")
                    yes_bid = price_data.get("yes_bid")
                    if no_ask and no_ask > 0:
                        no_price_cents = no_ask
                    elif yes_bid and yes_bid > 0:
                        no_price_cents = 100 - yes_bid
                    else:
                        continue  # Can't price the NO side

                    if city not in markets_by_city:
                        markets_by_city[city] = []

                    markets_by_city[city].append({
                        "ticker": market.ticker,
                        "threshold": parsed["threshold"],
                        "bracket_lower": parsed["threshold"] - 1.0,
                        "bracket_upper": parsed["threshold"] + 1.0,
                        "market_type": parsed["type"],  # "above" (high) or "below" (low)
                        "no_price_cents": no_price_cents,
                        "yes_ask": price_data.get("yes_ask", 0),
                        "yes_bid": price_data.get("yes_bid", 0),
                    })

                logger.debug(
                    f"Found {sum(len(v) for v in markets_by_city.values())} "
                    f"same-day brackets across {len(markets_by_city)} cities"
                )

        except Exception as e:
            logger.error(f"Error fetching bracket markets: {e}")

        return markets_by_city

    def _parse_bracket_ticker(self, ticker: str) -> Optional[dict]:
        """
        Parse a bracket ticker into city, date, threshold, type.

        Examples:
            KXHIGHMIA-26FEB13-B79.5 → {city: MIAMI, threshold: 79.5, type: above}
            KXLOWTCHI-26FEB13-B24.5 → {city: CHICAGO, threshold: 24.5, type: below}
        """
        ticker_upper = ticker.upper()

        # HIGH bracket
        match = re.match(
            r'KXHIGH(\w{2,5})-(\d{2}[A-Z]{3}\d{2})-B([\d.]+)$',
            ticker_upper,
        )
        if match:
            city_code = match.group(1)
            threshold = float(match.group(3))
            city = self.CITY_CODES.get(city_code)
            return {"city": city, "threshold": threshold, "type": "above"} if city else None

        # LOW bracket
        match = re.match(
            r'KXLOWT?(\w{2,5})-(\d{2}[A-Z]{3}\d{2})-B([\d.]+)$',
            ticker_upper,
        )
        if match:
            city_code = match.group(1)
            threshold = float(match.group(3))
            city = self.CITY_CODES.get(city_code)
            return {"city": city, "threshold": threshold, "type": "below"} if city else None

        return None

    @staticmethod
    def calculate_net_profit_cents(no_price_cents: int) -> float:
        """
        Calculate guaranteed net profit per contract for a dead bracket NO trade.

        Kalshi fee = 2% of WINNINGS (not payout).

        Args:
            no_price_cents: Cost to buy one NO contract (in cents)

        Returns:
            Net profit in cents after fees
        """
        winnings_cents = 100 - no_price_cents
        fee_cents = KALSHI_FEE_RATE * winnings_cents
        return winnings_cents - fee_cents

    @staticmethod
    def max_contracts_for_capital(no_price_cents: int, max_capital_dollars: float) -> int:
        """
        Calculate max contracts we can buy within capital limit.

        Args:
            no_price_cents: Cost per contract in cents
            max_capital_dollars: Maximum capital to deploy

        Returns:
            Number of contracts (at least MIN_CONTRACTS, at most MAX_CONTRACTS)
        """
        max_capital_cents = max_capital_dollars * 100
        contracts = int(max_capital_cents / no_price_cents)
        return max(MIN_CONTRACTS, min(contracts, MAX_CONTRACTS))

    def scan(self, bankroll: Optional[float] = None) -> List[ObsSettledOpportunity]:
        """
        Run one observation-settled scan cycle.

        1. Fetch latest NWS observations for all enabled cities
        2. Get all same-day bracket markets with prices
        3. Identify dead brackets (observed temp surpassed bracket bounds)
        4. Calculate fee-aware profitability
        5. Return list of guaranteed-profit opportunities

        Args:
            bankroll: Current bankroll for obs pool sizing. If None or no
                     risk_manager, falls back to MAX_CAPITAL_PER_TRADE.

        Returns:
            List of ObsSettledOpportunity (empty if no opportunities)
        """
        self._reset_daily_state()
        self._scan_count += 1
        scan_start = datetime.now(timezone.utc)

        opportunities: List[ObsSettledOpportunity] = []

        # Determine capital budget from obs pool or fallback
        if self.risk_manager and bankroll:
            deploy_amount = self.risk_manager.get_obs_settled_deploy_amount(bankroll)
            if deploy_amount <= 0:
                logger.debug("Obs-settled pool depleted, skipping scan")
                return opportunities
            capital_per_trade = deploy_amount
        else:
            capital_per_trade = MAX_CAPITAL_PER_TRADE

        # 1. Fetch observations from actual NWS settlement stations
        observations = self.nws_client.get_all_observations(self.enabled_cities)
        if not observations:
            logger.debug("Obs scanner: no observations available")
            return opportunities

        # 2. Get all same-day bracket markets with prices
        markets_by_city = self._get_same_day_bracket_markets()
        if not markets_by_city:
            logger.debug("Obs scanner: no same-day bracket markets")
            return opportunities

        # 3. For each city, check each bracket against the observation
        dead_brackets_found = 0
        profitable_found = 0

        for city, obs in observations.items():
            city_markets = markets_by_city.get(city, [])
            if not city_markets:
                continue

            for market in city_markets:
                ticker = market["ticker"]
                bracket_lower = market["bracket_lower"]
                bracket_upper = market["bracket_upper"]
                market_type = market["market_type"]
                no_price = market["no_price_cents"]

                # Skip if we've already traded this ticker today
                if ticker in self._traded_tickers:
                    continue

                # Determine if bracket is DEAD based on observation
                is_dead = False
                reason = ""

                if market_type == "above":
                    # HIGH bracket: dead if observed high ≥ bracket_upper
                    if obs.daily_high_f >= bracket_upper:
                        is_dead = True
                        reason = (
                            f"💀 DEAD BRACKET: {obs.station_id} observed high "
                            f"{obs.daily_high_f:.0f}°F ≥ bracket upper {bracket_upper:.0f}°F "
                            f"[{bracket_lower:.0f},{bracket_upper:.0f})°F"
                        )
                    # Also dead if past peak and high never reached bracket_lower
                    elif obs.is_past_peak_high and obs.daily_high_f < bracket_lower:
                        is_dead = True
                        reason = (
                            f"💀 DEAD BRACKET: Past peak, {obs.station_id} high "
                            f"{obs.daily_high_f:.0f}°F never reached bracket "
                            f"[{bracket_lower:.0f},{bracket_upper:.0f})°F"
                        )
                else:
                    # LOW bracket: dead if observed low < bracket_lower
                    if obs.daily_low_f < bracket_lower:
                        is_dead = True
                        reason = (
                            f"💀 DEAD BRACKET: {obs.station_id} observed low "
                            f"{obs.daily_low_f:.0f}°F < bracket lower {bracket_lower:.0f}°F "
                            f"[{bracket_lower:.0f},{bracket_upper:.0f})°F"
                        )
                    # Also dead if past sunrise and low never dropped below bracket_upper
                    elif obs.is_past_sunrise and obs.daily_low_f >= bracket_upper:
                        is_dead = True
                        reason = (
                            f"💀 DEAD BRACKET: Past sunrise, {obs.station_id} low "
                            f"{obs.daily_low_f:.0f}°F never reached bracket "
                            f"[{bracket_lower:.0f},{bracket_upper:.0f})°F"
                        )

                if not is_dead:
                    continue

                dead_brackets_found += 1

                # 4. Fee-aware profitability check
                if no_price > MAX_OBS_SETTLED_ENTRY_CENTS:
                    logger.debug(
                        f"  Obs scanner: {ticker} NO at {no_price}¢ > max {MAX_OBS_SETTLED_ENTRY_CENTS}¢"
                    )
                    continue

                net_profit = self.calculate_net_profit_cents(no_price)
                if net_profit < MIN_PROFIT_PER_CONTRACT_CENTS:
                    logger.debug(
                        f"  Obs scanner: {ticker} profit {net_profit:.1f}¢ < min {MIN_PROFIT_PER_CONTRACT_CENTS}¢"
                    )
                    continue

                profitable_found += 1

                # 5. Calculate position size (uses obs pool budget if available)
                max_qty = self.max_contracts_for_capital(no_price, capital_per_trade)
                total_profit = net_profit * max_qty

                opp = ObsSettledOpportunity(
                    ticker=ticker,
                    city=city,
                    side="no",
                    bracket_lower=bracket_lower,
                    bracket_upper=bracket_upper,
                    observed_extreme=obs.daily_high_f if market_type == "above" else obs.daily_low_f,
                    no_price_cents=no_price,
                    guaranteed_profit_cents=net_profit,
                    max_contracts=max_qty,
                    total_profit_cents=total_profit,
                    reason=reason,
                    market_type=market_type,
                    station_id=obs.station_id,
                    observation_time=obs.observation_time,
                )

                opportunities.append(opp)

                logger.info(
                    f"  🎯 OBS-SETTLED: {ticker} | NO at {no_price}¢ | "
                    f"profit={net_profit:.1f}¢/contract × {max_qty} = "
                    f"{total_profit:.0f}¢ total | {reason}"
                )

        scan_duration = (datetime.now(timezone.utc) - scan_start).total_seconds()

        # Sort by profit per contract (best opportunities first)
        opportunities.sort(key=lambda o: o.guaranteed_profit_cents, reverse=True)

        if opportunities:
            logger.info(
                f"🔭 OBS SCAN #{self._scan_count} COMPLETE: "
                f"{dead_brackets_found} dead brackets, "
                f"{profitable_found} profitable, "
                f"{len(opportunities)} opportunities | "
                f"scan took {scan_duration:.1f}s"
            )
        else:
            logger.debug(
                f"Obs scan #{self._scan_count}: "
                f"{dead_brackets_found} dead (0 profitable) | {scan_duration:.1f}s"
            )

        self._last_scan_time = scan_start
        return opportunities

    def mark_traded(self, ticker: str) -> None:
        """Mark a ticker as traded so we don't re-enter."""
        self._traded_tickers.add(ticker)
        logger.debug(f"Obs scanner: marked {ticker} as traded")
