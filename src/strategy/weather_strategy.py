"""
Weather Strategy - NWS-based temperature, snow, and rain market trading.

Uses National Weather Service forecasts as the "ground truth" since
Kalshi temperature markets settle based on NWS data.

Strategy:
1. Fetch NWS forecasts for supported cities
2. Match forecasts to active Kalshi weather markets
3. Calculate edge (NWS probability vs market price)
4. Generate signals when edge > threshold

Supported Market Types:
- Temperature: KXHIGHNY-26FEB10-B36.5, KXLOWTNYC-26FEB10-T21
- Snow monthly: KXBOSSNOWM-26FEB-15.0
- Snow daily: SNOWNY-22FEB19-T0
- Rain monthly: KXRAINNYCM-26FEB-3
- Rain daily: RAINNY-21SEP02-T0
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import List, Dict, Optional, Tuple
import re
import json

from sqlalchemy import or_, func
import os

from src.data.database import get_db_session
from src.data.models import MarketDB, ForecastDB, PriceDB
from src.data_sources.nws_weather import TemperatureForecast, KALSHI_STATIONS
from src.strategy.city_bracket_portfolio import (
    CityBracketPortfolio,
    CityPortfolioManager,
    get_city_portfolio_manager,
)
from src.utils.logging import logger
from src.utils.probability_utils import norm_cdf as _norm_cdf

# NWS is the ONLY forecast source — Kalshi settles on NWS data.
# Using any other source adds noise. Mock is allowed for testing only.
if os.getenv("USE_MOCK_WEATHER", "false").lower() == "true":
    from src.data_sources.mock_weather import MockNWSClient as NWSClient
    logger.warning("Using MOCK weather client - DO NOT use for real money trading!")
else:
    from src.data_sources.nws_weather import NWSClient
    logger.info("Using NWS API for weather forecasts")



@dataclass
class WeatherOpportunity:
    """A potential weather trading opportunity."""
    ticker: str
    city: str
    market_date: date
    threshold_temp: int
    threshold_type: str  # "above" or "below"
    nws_forecast_temp: int
    nws_confidence: str
    market_price: float  # Current YES price
    our_probability: float  # Our calculated probability
    edge: float  # our_probability - market_price (positive = buy YES)
    recommendation: str  # "BUY_YES", "BUY_NO", "NO_TRADE"
    reasoning: str
    market_type: str = "temperature"  # "temperature", "snow", "rain"
    trade_type: str = "unknown"  # "no_exclusion", "yes_convergence", or "unknown"
    observation_settled: bool = False  # True if live observation has confirmed outcome


@dataclass
class WeatherStrategyResult:
    """Results from a weather strategy scan."""
    markets_scanned: int = 0
    markets_parsed: int = 0
    markets_with_price: int = 0
    forecasts_fetched: int = 0
    opportunities_found: int = 0
    signals_generated: int = 0
    opportunities: List[WeatherOpportunity] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    parse_failures: List[str] = field(default_factory=list)


class WeatherStrategy:
    """
    NWS-based weather market trading strategy.

    Uses NWS forecasts as ground truth since Kalshi settles on NWS data.
    Generates trading signals when our calculated probability diverges
    significantly from market prices.
    """

    # Edge thresholds — lowered for paper trading volume
    MIN_EDGE_PCT = 0.05  # 5% minimum edge (paper mode)
    HIGH_CONFIDENCE_EDGE = 0.10  # 10% edge for high confidence

    # Confidence adjustments
    CONFIDENCE_MULTIPLIERS = {
        "high": 1.0,    # Full confidence for 1-day forecasts
        "medium": 0.85,  # Reduce confidence for 2-3 day forecasts
        "low": 0.70,    # Further reduce for 4+ day forecasts
    }

    # City code mapping: Kalshi ticker suffix -> NWS city name
    CITY_CODES = {
        "NY": "NYC",
        "NYC": "NYC",
        "NEWYORK": "NYC",
        "CHI": "CHICAGO",
        "CHICAGO": "CHICAGO",
        "MIA": "MIAMI",
        "MIAMI": "MIAMI",
        "AUS": "AUSTIN",
        "AUSTIN": "AUSTIN",
        "LA": "LOS_ANGELES",
        "DEN": "DENVER",
        "ATL": "ATLANTA",
        "PHIL": "PHILADELPHIA",
        "SEA": "SEATTLE",
        "BOS": "BOSTON",
        "BOSTON": "BOSTON",
        "DAL": "DALLAS",
        "DALLAS": "DALLAS",
        "HOU": "HOUSTON",
        "HOUS": "HOUSTON",
        "HOUSTON": "HOUSTON",
        "DC": "WASHINGTON_DC",
        "DET": "DETROIT",
        "DETROIT": "DETROIT",
        "SLC": "SALT_LAKE_CITY",
    }

    # Legacy alias map (kept for backwards compatibility)
    CITY_ALIASES = {
        "NYC": ["NYC", "NY", "NEWYORK", "NEW-YORK"],
        "CHICAGO": ["CHICAGO", "CHI"],
        "MIAMI": ["MIAMI", "MIA"],
        "AUSTIN": ["AUSTIN", "AUS"],
    }

    # Cities where snow is physically implausible (no winter storm warning check yet)
    SNOW_IMPLAUSIBLE_CITIES = {"MIAMI", "AUSTIN", "LOS_ANGELES", "HOUSTON"}

    # Monthly climatological normals: city -> month -> {high_mean, high_std, low_mean, low_std}
    # Source: NOAA 1991-2020 Climate Normals (rounded to 1 decimal)
    CLIMATE_NORMALS = {
        "NYC": {
            1: {"high_mean": 39.5, "high_std": 7.2, "low_mean": 27.3, "low_std": 7.0},
            2: {"high_mean": 42.2, "high_std": 7.5, "low_mean": 29.3, "low_std": 7.1},
            3: {"high_mean": 50.3, "high_std": 8.0, "low_mean": 35.7, "low_std": 6.5},
            4: {"high_mean": 61.8, "high_std": 7.5, "low_mean": 45.2, "low_std": 5.8},
            5: {"high_mean": 72.0, "high_std": 7.0, "low_mean": 55.0, "low_std": 5.5},
            6: {"high_mean": 80.5, "high_std": 6.5, "low_mean": 64.5, "low_std": 5.0},
            7: {"high_mean": 85.2, "high_std": 5.8, "low_mean": 69.8, "low_std": 4.5},
            8: {"high_mean": 83.6, "high_std": 5.5, "low_mean": 68.5, "low_std": 4.5},
            9: {"high_mean": 76.5, "high_std": 6.0, "low_mean": 61.2, "low_std": 5.0},
            10: {"high_mean": 65.0, "high_std": 7.0, "low_mean": 50.5, "low_std": 5.5},
            11: {"high_mean": 54.0, "high_std": 7.5, "low_mean": 41.5, "low_std": 6.0},
            12: {"high_mean": 43.5, "high_std": 7.0, "low_mean": 31.5, "low_std": 6.5},
        },
        "CHICAGO": {
            1: {"high_mean": 32.0, "high_std": 9.5, "low_mean": 18.5, "low_std": 9.0},
            2: {"high_mean": 36.0, "high_std": 9.5, "low_mean": 22.0, "low_std": 9.0},
            3: {"high_mean": 47.5, "high_std": 9.0, "low_mean": 31.5, "low_std": 7.5},
            4: {"high_mean": 59.5, "high_std": 9.0, "low_mean": 41.0, "low_std": 7.0},
            5: {"high_mean": 70.5, "high_std": 8.0, "low_mean": 51.5, "low_std": 6.5},
            6: {"high_mean": 80.5, "high_std": 6.5, "low_mean": 62.0, "low_std": 5.5},
            7: {"high_mean": 84.5, "high_std": 5.5, "low_mean": 67.0, "low_std": 4.5},
            8: {"high_mean": 82.5, "high_std": 5.5, "low_mean": 65.5, "low_std": 4.5},
            9: {"high_mean": 75.5, "high_std": 7.0, "low_mean": 57.0, "low_std": 6.0},
            10: {"high_mean": 62.5, "high_std": 8.0, "low_mean": 45.0, "low_std": 6.5},
            11: {"high_mean": 48.0, "high_std": 9.0, "low_mean": 33.5, "low_std": 7.5},
            12: {"high_mean": 35.5, "high_std": 9.0, "low_mean": 22.0, "low_std": 8.5},
        },
        "MIAMI": {
            1: {"high_mean": 76.5, "high_std": 5.0, "low_mean": 61.0, "low_std": 5.5},
            2: {"high_mean": 78.0, "high_std": 5.0, "low_mean": 62.5, "low_std": 5.5},
            3: {"high_mean": 80.5, "high_std": 4.5, "low_mean": 65.5, "low_std": 5.0},
            4: {"high_mean": 83.5, "high_std": 3.5, "low_mean": 69.5, "low_std": 4.0},
            5: {"high_mean": 87.0, "high_std": 3.0, "low_mean": 74.0, "low_std": 3.0},
            6: {"high_mean": 90.0, "high_std": 2.5, "low_mean": 76.5, "low_std": 2.5},
            7: {"high_mean": 91.5, "high_std": 2.0, "low_mean": 77.5, "low_std": 2.0},
            8: {"high_mean": 91.0, "high_std": 2.0, "low_mean": 77.5, "low_std": 2.0},
            9: {"high_mean": 89.5, "high_std": 2.5, "low_mean": 76.5, "low_std": 2.5},
            10: {"high_mean": 86.0, "high_std": 3.0, "low_mean": 73.0, "low_std": 3.5},
            11: {"high_mean": 81.5, "high_std": 4.0, "low_mean": 67.5, "low_std": 4.5},
            12: {"high_mean": 77.5, "high_std": 4.5, "low_mean": 63.0, "low_std": 5.0},
        },
        "AUSTIN": {
            1: {"high_mean": 62.0, "high_std": 9.0, "low_mean": 40.0, "low_std": 8.5},
            2: {"high_mean": 65.5, "high_std": 10.0, "low_mean": 43.5, "low_std": 9.0},
            3: {"high_mean": 73.0, "high_std": 8.5, "low_mean": 51.0, "low_std": 7.5},
            4: {"high_mean": 80.0, "high_std": 7.0, "low_mean": 58.5, "low_std": 6.0},
            5: {"high_mean": 86.5, "high_std": 6.0, "low_mean": 66.0, "low_std": 5.0},
            6: {"high_mean": 93.0, "high_std": 5.0, "low_mean": 72.5, "low_std": 3.5},
            7: {"high_mean": 97.0, "high_std": 4.5, "low_mean": 75.0, "low_std": 3.0},
            8: {"high_mean": 98.0, "high_std": 4.5, "low_mean": 75.0, "low_std": 3.0},
            9: {"high_mean": 92.0, "high_std": 5.5, "low_mean": 70.0, "low_std": 4.5},
            10: {"high_mean": 82.0, "high_std": 7.5, "low_mean": 58.5, "low_std": 6.5},
            11: {"high_mean": 71.0, "high_std": 8.5, "low_mean": 48.5, "low_std": 8.0},
            12: {"high_mean": 63.0, "high_std": 9.0, "low_mean": 41.0, "low_std": 8.5},
        },
        "DENVER": {
            1: {"high_mean": 45.5, "high_std": 10.0, "low_mean": 17.5, "low_std": 9.0},
            2: {"high_mean": 46.5, "high_std": 10.5, "low_mean": 19.5, "low_std": 9.0},
            3: {"high_mean": 54.5, "high_std": 10.0, "low_mean": 26.5, "low_std": 8.0},
            4: {"high_mean": 61.0, "high_std": 10.0, "low_mean": 33.5, "low_std": 7.5},
            5: {"high_mean": 70.5, "high_std": 8.5, "low_mean": 43.5, "low_std": 6.5},
            6: {"high_mean": 82.0, "high_std": 7.0, "low_mean": 53.0, "low_std": 5.5},
            7: {"high_mean": 88.5, "high_std": 5.5, "low_mean": 59.5, "low_std": 4.5},
            8: {"high_mean": 86.5, "high_std": 5.5, "low_mean": 57.5, "low_std": 4.5},
            9: {"high_mean": 79.0, "high_std": 7.0, "low_mean": 49.0, "low_std": 6.0},
            10: {"high_mean": 65.0, "high_std": 9.5, "low_mean": 36.5, "low_std": 7.5},
            11: {"high_mean": 52.5, "high_std": 10.0, "low_mean": 25.0, "low_std": 8.5},
            12: {"high_mean": 44.5, "high_std": 9.5, "low_mean": 17.5, "low_std": 8.5},
        },
        "ATLANTA": {
            1: {"high_mean": 52.5, "high_std": 8.0, "low_mean": 34.0, "low_std": 7.5},
            2: {"high_mean": 56.5, "high_std": 8.5, "low_mean": 37.5, "low_std": 7.5},
            3: {"high_mean": 64.0, "high_std": 8.0, "low_mean": 44.0, "low_std": 6.5},
            4: {"high_mean": 72.5, "high_std": 7.0, "low_mean": 52.0, "low_std": 5.5},
            5: {"high_mean": 80.0, "high_std": 6.0, "low_mean": 61.0, "low_std": 4.5},
            6: {"high_mean": 87.0, "high_std": 5.0, "low_mean": 68.5, "low_std": 3.5},
            7: {"high_mean": 89.5, "high_std": 4.0, "low_mean": 72.0, "low_std": 3.0},
            8: {"high_mean": 89.0, "high_std": 4.0, "low_mean": 71.5, "low_std": 3.0},
            9: {"high_mean": 83.5, "high_std": 5.5, "low_mean": 65.0, "low_std": 4.5},
            10: {"high_mean": 73.0, "high_std": 7.0, "low_mean": 53.0, "low_std": 6.0},
            11: {"high_mean": 62.5, "high_std": 7.5, "low_mean": 43.0, "low_std": 6.5},
            12: {"high_mean": 54.0, "high_std": 7.5, "low_mean": 36.0, "low_std": 7.0},
        },
        "PHILADELPHIA": {
            1: {"high_mean": 40.5, "high_std": 7.5, "low_mean": 26.0, "low_std": 7.0},
            2: {"high_mean": 44.0, "high_std": 7.5, "low_mean": 28.5, "low_std": 7.0},
            3: {"high_mean": 53.0, "high_std": 8.0, "low_mean": 35.5, "low_std": 6.5},
            4: {"high_mean": 64.5, "high_std": 7.5, "low_mean": 45.5, "low_std": 5.5},
            5: {"high_mean": 74.5, "high_std": 7.0, "low_mean": 55.5, "low_std": 5.0},
            6: {"high_mean": 83.5, "high_std": 6.0, "low_mean": 65.0, "low_std": 4.5},
            7: {"high_mean": 87.5, "high_std": 5.5, "low_mean": 70.0, "low_std": 4.0},
            8: {"high_mean": 85.5, "high_std": 5.5, "low_mean": 68.5, "low_std": 4.0},
            9: {"high_mean": 78.5, "high_std": 6.0, "low_mean": 61.0, "low_std": 5.0},
            10: {"high_mean": 67.0, "high_std": 7.0, "low_mean": 49.5, "low_std": 5.5},
            11: {"high_mean": 55.5, "high_std": 7.5, "low_mean": 40.0, "low_std": 6.0},
            12: {"high_mean": 44.5, "high_std": 7.0, "low_mean": 31.0, "low_std": 6.5},
        },
        "SEATTLE": {
            1: {"high_mean": 47.0, "high_std": 5.5, "low_mean": 37.0, "low_std": 5.0},
            2: {"high_mean": 49.5, "high_std": 5.5, "low_mean": 37.5, "low_std": 4.5},
            3: {"high_mean": 53.5, "high_std": 5.0, "low_mean": 39.5, "low_std": 4.0},
            4: {"high_mean": 58.5, "high_std": 5.0, "low_mean": 43.0, "low_std": 3.5},
            5: {"high_mean": 65.0, "high_std": 5.5, "low_mean": 49.0, "low_std": 3.5},
            6: {"high_mean": 70.5, "high_std": 5.5, "low_mean": 54.0, "low_std": 3.5},
            7: {"high_mean": 76.5, "high_std": 5.5, "low_mean": 58.0, "low_std": 3.5},
            8: {"high_mean": 76.5, "high_std": 5.5, "low_mean": 58.0, "low_std": 3.5},
            9: {"high_mean": 71.5, "high_std": 6.0, "low_mean": 53.5, "low_std": 4.0},
            10: {"high_mean": 60.0, "high_std": 5.5, "low_mean": 46.0, "low_std": 4.0},
            11: {"high_mean": 51.0, "high_std": 5.0, "low_mean": 40.0, "low_std": 4.5},
            12: {"high_mean": 45.5, "high_std": 5.5, "low_mean": 35.5, "low_std": 5.0},
        },
        "LOS_ANGELES": {
            1: {"high_mean": 68.0, "high_std": 5.5, "low_mean": 49.0, "low_std": 4.5},
            2: {"high_mean": 68.0, "high_std": 5.0, "low_mean": 50.0, "low_std": 4.0},
            3: {"high_mean": 69.5, "high_std": 4.5, "low_mean": 52.0, "low_std": 3.5},
            4: {"high_mean": 72.0, "high_std": 4.5, "low_mean": 54.5, "low_std": 3.5},
            5: {"high_mean": 74.0, "high_std": 4.0, "low_mean": 58.5, "low_std": 3.0},
            6: {"high_mean": 78.5, "high_std": 4.0, "low_mean": 62.5, "low_std": 3.0},
            7: {"high_mean": 84.0, "high_std": 4.0, "low_mean": 66.0, "low_std": 2.5},
            8: {"high_mean": 85.0, "high_std": 4.0, "low_mean": 66.5, "low_std": 2.5},
            9: {"high_mean": 83.5, "high_std": 5.0, "low_mean": 65.0, "low_std": 3.0},
            10: {"high_mean": 78.0, "high_std": 5.5, "low_mean": 59.5, "low_std": 3.5},
            11: {"high_mean": 73.0, "high_std": 5.5, "low_mean": 53.5, "low_std": 4.0},
            12: {"high_mean": 67.5, "high_std": 5.5, "low_mean": 48.5, "low_std": 4.5},
        },
        "BOSTON": {
            1: {"high_mean": 36.5, "high_std": 7.5, "low_mean": 22.5, "low_std": 7.5},
            2: {"high_mean": 39.0, "high_std": 7.5, "low_mean": 24.5, "low_std": 7.5},
            3: {"high_mean": 46.0, "high_std": 8.0, "low_mean": 31.5, "low_std": 7.0},
            4: {"high_mean": 56.5, "high_std": 7.5, "low_mean": 41.0, "low_std": 6.0},
            5: {"high_mean": 67.0, "high_std": 7.0, "low_mean": 50.5, "low_std": 5.5},
            6: {"high_mean": 76.5, "high_std": 6.5, "low_mean": 60.5, "low_std": 5.0},
            7: {"high_mean": 82.0, "high_std": 5.5, "low_mean": 66.0, "low_std": 4.5},
            8: {"high_mean": 80.0, "high_std": 5.5, "low_mean": 65.0, "low_std": 4.5},
            9: {"high_mean": 73.0, "high_std": 6.0, "low_mean": 57.0, "low_std": 5.0},
            10: {"high_mean": 62.0, "high_std": 7.0, "low_mean": 47.0, "low_std": 5.5},
            11: {"high_mean": 51.0, "high_std": 7.5, "low_mean": 37.0, "low_std": 6.5},
            12: {"high_mean": 40.0, "high_std": 7.0, "low_mean": 26.5, "low_std": 7.0},
        },
        "DALLAS": {
            1: {"high_mean": 57.0, "high_std": 10.0, "low_mean": 37.0, "low_std": 9.0},
            2: {"high_mean": 60.5, "high_std": 10.5, "low_mean": 40.5, "low_std": 9.5},
            3: {"high_mean": 68.5, "high_std": 9.0, "low_mean": 48.5, "low_std": 7.5},
            4: {"high_mean": 76.5, "high_std": 7.5, "low_mean": 57.0, "low_std": 6.0},
            5: {"high_mean": 84.0, "high_std": 6.5, "low_mean": 65.5, "low_std": 5.0},
            6: {"high_mean": 92.5, "high_std": 5.0, "low_mean": 73.0, "low_std": 3.5},
            7: {"high_mean": 97.0, "high_std": 4.5, "low_mean": 77.0, "low_std": 3.0},
            8: {"high_mean": 97.5, "high_std": 4.5, "low_mean": 76.5, "low_std": 3.0},
            9: {"high_mean": 90.0, "high_std": 6.0, "low_mean": 68.5, "low_std": 5.0},
            10: {"high_mean": 78.5, "high_std": 8.0, "low_mean": 57.0, "low_std": 6.5},
            11: {"high_mean": 66.5, "high_std": 9.0, "low_mean": 46.0, "low_std": 8.0},
            12: {"high_mean": 58.0, "high_std": 9.5, "low_mean": 38.0, "low_std": 8.5},
        },
        "HOUSTON": {
            1: {"high_mean": 63.0, "high_std": 8.5, "low_mean": 44.0, "low_std": 8.0},
            2: {"high_mean": 66.5, "high_std": 9.0, "low_mean": 47.0, "low_std": 8.5},
            3: {"high_mean": 73.0, "high_std": 7.5, "low_mean": 53.5, "low_std": 7.0},
            4: {"high_mean": 79.5, "high_std": 6.5, "low_mean": 60.5, "low_std": 5.5},
            5: {"high_mean": 86.0, "high_std": 5.5, "low_mean": 68.5, "low_std": 4.0},
            6: {"high_mean": 91.5, "high_std": 4.0, "low_mean": 74.0, "low_std": 3.0},
            7: {"high_mean": 94.0, "high_std": 3.5, "low_mean": 76.0, "low_std": 2.5},
            8: {"high_mean": 95.0, "high_std": 3.5, "low_mean": 76.0, "low_std": 2.5},
            9: {"high_mean": 90.5, "high_std": 4.5, "low_mean": 72.0, "low_std": 4.0},
            10: {"high_mean": 82.0, "high_std": 6.5, "low_mean": 62.0, "low_std": 6.0},
            11: {"high_mean": 72.0, "high_std": 8.0, "low_mean": 52.0, "low_std": 7.0},
            12: {"high_mean": 64.0, "high_std": 8.5, "low_mean": 45.0, "low_std": 8.0},
        },
        "WASHINGTON_DC": {
            1: {"high_mean": 44.0, "high_std": 7.5, "low_mean": 29.0, "low_std": 7.0},
            2: {"high_mean": 47.5, "high_std": 8.0, "low_mean": 31.5, "low_std": 7.0},
            3: {"high_mean": 56.0, "high_std": 8.0, "low_mean": 38.5, "low_std": 6.5},
            4: {"high_mean": 67.0, "high_std": 7.5, "low_mean": 48.0, "low_std": 5.5},
            5: {"high_mean": 76.0, "high_std": 7.0, "low_mean": 57.5, "low_std": 5.0},
            6: {"high_mean": 84.5, "high_std": 6.0, "low_mean": 67.0, "low_std": 4.5},
            7: {"high_mean": 88.5, "high_std": 5.0, "low_mean": 72.0, "low_std": 3.5},
            8: {"high_mean": 87.0, "high_std": 5.0, "low_mean": 70.5, "low_std": 4.0},
            9: {"high_mean": 80.5, "high_std": 6.0, "low_mean": 63.0, "low_std": 5.0},
            10: {"high_mean": 68.5, "high_std": 7.0, "low_mean": 51.5, "low_std": 5.5},
            11: {"high_mean": 57.0, "high_std": 7.5, "low_mean": 41.0, "low_std": 6.0},
            12: {"high_mean": 46.5, "high_std": 7.5, "low_mean": 32.5, "low_std": 7.0},
        },
        "DETROIT": {
            1: {"high_mean": 32.5, "high_std": 8.5, "low_mean": 19.5, "low_std": 8.5},
            2: {"high_mean": 35.5, "high_std": 8.5, "low_mean": 21.0, "low_std": 8.5},
            3: {"high_mean": 46.0, "high_std": 9.0, "low_mean": 29.5, "low_std": 7.5},
            4: {"high_mean": 58.5, "high_std": 8.5, "low_mean": 39.5, "low_std": 6.5},
            5: {"high_mean": 70.0, "high_std": 8.0, "low_mean": 49.5, "low_std": 6.0},
            6: {"high_mean": 79.5, "high_std": 6.5, "low_mean": 59.5, "low_std": 5.0},
            7: {"high_mean": 83.5, "high_std": 5.5, "low_mean": 64.0, "low_std": 4.5},
            8: {"high_mean": 81.5, "high_std": 5.5, "low_mean": 62.5, "low_std": 4.5},
            9: {"high_mean": 74.5, "high_std": 7.0, "low_mean": 54.5, "low_std": 5.5},
            10: {"high_mean": 61.5, "high_std": 8.0, "low_mean": 43.5, "low_std": 6.0},
            11: {"high_mean": 48.5, "high_std": 8.5, "low_mean": 34.0, "low_std": 7.0},
            12: {"high_mean": 36.0, "high_std": 8.0, "low_mean": 23.5, "low_std": 8.0},
        },
        "SALT_LAKE_CITY": {
            1: {"high_mean": 37.5, "high_std": 8.5, "low_mean": 22.0, "low_std": 7.5},
            2: {"high_mean": 43.0, "high_std": 8.5, "low_mean": 26.0, "low_std": 7.0},
            3: {"high_mean": 55.0, "high_std": 8.0, "low_mean": 34.0, "low_std": 6.0},
            4: {"high_mean": 62.5, "high_std": 8.5, "low_mean": 40.5, "low_std": 5.5},
            5: {"high_mean": 73.0, "high_std": 8.0, "low_mean": 49.5, "low_std": 5.5},
            6: {"high_mean": 85.0, "high_std": 7.0, "low_mean": 58.0, "low_std": 5.0},
            7: {"high_mean": 93.5, "high_std": 5.5, "low_mean": 66.5, "low_std": 4.0},
            8: {"high_mean": 91.5, "high_std": 5.5, "low_mean": 64.5, "low_std": 4.0},
            9: {"high_mean": 81.5, "high_std": 7.0, "low_mean": 54.0, "low_std": 5.5},
            10: {"high_mean": 66.0, "high_std": 8.5, "low_mean": 42.0, "low_std": 6.5},
            11: {"high_mean": 50.0, "high_std": 8.5, "low_mean": 31.0, "low_std": 7.0},
            12: {"high_mean": 38.5, "high_std": 8.0, "low_mean": 22.5, "low_std": 7.0},
        },
    }

    # Bracket squeeze configuration (env-configurable)
    BRACKET_CONVERGENCE_DISTANCE_F = float(os.environ.get("WEATHER_CONVERGENCE_DISTANCE", "2.0"))
    BRACKET_EXCLUSION_DISTANCE_F = float(os.environ.get("WEATHER_EXCLUSION_DISTANCE", "5.0"))
    BRACKET_EXCLUSION_MIN_PROB = float(os.environ.get("WEATHER_EXCLUSION_MIN_PROB", "0.80"))

    def __init__(self, min_edge: float = 0.10):
        """
        Initialize weather strategy.

        Args:
            min_edge: Minimum edge percentage to generate a signal (default 10%)
        """
        self.min_edge = min_edge
        self.nws_client = NWSClient()
        logger.info(f"WeatherStrategy initialized with min_edge={min_edge*100:.0f}%")

    # Months lookup for date parsing
    _MONTHS = {
        'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4,
        'MAY': 5, 'JUN': 6, 'JUL': 7, 'AUG': 8,
        'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12,
    }

    def _parse_date(self, date_str: str) -> Optional[date]:
        """
        Parse Kalshi date format.

        Handles both daily and monthly formats:
        - 26FEB10 -> 2026-02-10 (daily)
        - 26FEB   -> 2026-02-01 (monthly, returns 1st of month)
        """
        try:
            year = 2000 + int(date_str[:2])
            month_str = date_str[2:5]
            month = self._MONTHS.get(month_str, 0)
            if month == 0:
                return None
            if len(date_str) >= 7:
                day = int(date_str[5:7])
            else:
                # Monthly format — use 1st of month
                day = 1
            return date(year, month, day)
        except (ValueError, IndexError):
            return None

    def _parse_temperature_ticker(self, ticker: str) -> Optional[Dict]:
        """
        Parse a Kalshi temperature market ticker.

        Patterns:
        - KXHIGHNY-26FEB10-B36.5  (high temp bracket)
        - KXHIGHNY-26FEB10-T39    (high temp threshold: above 39F)
        - KXHIGHNY-26FEB10-T32    (may be "below" per title, but parser uses regex)
        - KXLOWTNYC-26FEB10-T21   (low temp threshold)
        - KXLOWTCHI-26FEB10-B34.5 (low temp bracket)
        - KXTEMP-NYC-26FEB01-40   (legacy format)
        """
        ticker_upper = ticker.upper()

        # Pattern 1: KXHIGH{CITY}-{DATE}(-B{THRESHOLD} or -T{THRESHOLD})?
        match = re.match(
            r'KXHIGH(\w{2,5})-(\d{2}[A-Z]{3}\d{2})(?:-(B|T)([\d.]+))?$',
            ticker_upper,
        )
        if match:
            city_code, date_str, bt_letter, threshold_str = match.groups()
            city = self.CITY_CODES.get(city_code)
            if not city:
                logger.debug(f"Unknown city code '{city_code}' in ticker: {ticker}")
                return None
            market_date = self._parse_date(date_str)
            if not market_date:
                logger.debug(f"Could not parse date '{date_str}' in ticker: {ticker}")
                return None
            threshold = float(threshold_str) if threshold_str else 0.0
            is_bracket = (bt_letter == "B") if bt_letter else False
            return {
                "city": city,
                "date": market_date,
                "threshold": threshold,
                "type": "above",
                "temp_series": "high",   # KXHIGH → use forecast.high_f
                "market_type": "temperature",
                "is_monthly": False,
                "is_bracket": is_bracket,
            }

        # Pattern 2: KXLOWT{CITY}-{DATE}(-B{THRESHOLD} or -T{THRESHOLD})?
        # Also handles KXLOW{CITY} (without T)
        match = re.match(
            r'KXLOWT?(\w{2,5})-(\d{2}[A-Z]{3}\d{2})(?:-(B|T)([\d.]+))?$',
            ticker_upper,
        )
        if match:
            city_code, date_str, bt_letter, threshold_str = match.groups()
            city = self.CITY_CODES.get(city_code)
            if not city:
                logger.debug(f"Unknown city code '{city_code}' in ticker: {ticker}")
                return None
            market_date = self._parse_date(date_str)
            if not market_date:
                logger.debug(f"Could not parse date '{date_str}' in ticker: {ticker}")
                return None
            threshold = float(threshold_str) if threshold_str else 0.0
            is_bracket = (bt_letter == "B") if bt_letter else False
            return {
                "city": city,
                "date": market_date,
                "threshold": threshold,
                "type": "below",
                "temp_series": "low",    # KXLOWT → use forecast.low_f
                "market_type": "temperature",
                "is_monthly": False,
                "is_bracket": is_bracket,
            }

        # Pattern 3: Legacy KXTEMP-CITY-DATE-THRESHOLD
        match = re.match(
            r'KXTEMP(?:HI|LO)?-([A-Z\-]+)-(\d{2}[A-Z]{3}\d{2})-(\d+)$',
            ticker_upper,
        )
        if match:
            city_raw, date_str, threshold_str = match.groups()
            city = self.CITY_CODES.get(city_raw.replace("-", ""))
            if not city:
                # Try alias lookup
                for std_city, aliases in self.CITY_ALIASES.items():
                    if city_raw.replace("-", "") in aliases:
                        city = std_city
                        break
            if not city:
                return None
            market_date = self._parse_date(date_str)
            if not market_date:
                return None
            threshold_type = "below" if "LO" in ticker_upper else "above"
            return {
                "city": city,
                "date": market_date,
                "threshold": int(threshold_str),
                "type": threshold_type,
                "market_type": "temperature",
                "is_monthly": False,
            }

        return None

    def _parse_snow_ticker(self, ticker: str) -> Optional[Dict]:
        """
        Parse a Kalshi snow market ticker.

        Patterns:
        - KXBOSSNOWM-26FEB-15.0    (monthly: will Boston get >15" snow in Feb?)
        - SNOWNY-22FEB19-T0         (daily: will it snow in NYC on Feb 19?)
        - SNOWNY-22FEB19-T2         (daily: will it snow >2" in NYC on Feb 19?)
        """
        ticker_upper = ticker.upper()

        # Pattern 1: KX{CITY}SNOWM-{YYMM}-{THRESHOLD} (monthly)
        match = re.match(
            r'KX(\w{2,5})SNOWM-(\d{2}[A-Z]{3})(?:-([\d.]+))?$',
            ticker_upper,
        )
        if match:
            city_code, date_str, threshold_str = match.groups()
            city = self.CITY_CODES.get(city_code)
            if not city:
                logger.debug(f"Unknown city code '{city_code}' in snow ticker: {ticker}")
                return None
            market_date = self._parse_date(date_str)
            if not market_date:
                return None
            threshold = float(threshold_str) if threshold_str else 0.0
            return {
                "city": city,
                "date": market_date,
                "threshold": threshold,
                "type": "above",
                "market_type": "snow",
                "is_monthly": True,
            }

        # Pattern 2: SNOW{CITY}-{DATE}-T{THRESHOLD} (daily)
        match = re.match(
            r'SNOW(\w{2,5})-(\d{2}[A-Z]{3}\d{2})-T([\d.]+)$',
            ticker_upper,
        )
        if match:
            city_code, date_str, threshold_str = match.groups()
            city = self.CITY_CODES.get(city_code)
            if not city:
                logger.debug(f"Unknown city code '{city_code}' in snow ticker: {ticker}")
                return None
            market_date = self._parse_date(date_str)
            if not market_date:
                return None
            threshold = float(threshold_str)
            return {
                "city": city,
                "date": market_date,
                "threshold": threshold,
                "type": "above",
                "market_type": "snow",
                "is_monthly": False,
            }

        return None

    def _parse_rain_ticker(self, ticker: str) -> Optional[Dict]:
        """
        Parse a Kalshi rain market ticker.

        Patterns:
        - KXRAINNYCM-26FEB-3      (monthly: will NYC get >3" rain in Feb?)
        - RAINNY-21SEP02-T0        (daily: will it rain in NYC on Sep 2?)
        - RAINNY-21AUG22-T2.6     (daily: will NYC get >2.6" rain on Aug 22?)
        """
        ticker_upper = ticker.upper()

        # Pattern 1: KXRAIN{CITY}M-{YYMM}-{THRESHOLD} (monthly)
        match = re.match(
            r'KXRAIN(\w{2,5})M-(\d{2}[A-Z]{3})(?:-([\d.]+))?$',
            ticker_upper,
        )
        if match:
            city_code, date_str, threshold_str = match.groups()
            city = self.CITY_CODES.get(city_code)
            if not city:
                logger.debug(f"Unknown city code '{city_code}' in rain ticker: {ticker}")
                return None
            market_date = self._parse_date(date_str)
            if not market_date:
                return None
            threshold = float(threshold_str) if threshold_str else 0.0
            return {
                "city": city,
                "date": market_date,
                "threshold": threshold,
                "type": "above",
                "market_type": "rain",
                "is_monthly": True,
            }

        # Pattern 2: RAIN{CITY}-{DATE}-T{THRESHOLD} (daily)
        match = re.match(
            r'RAIN(\w{2,5})-(\d{2}[A-Z]{3}\d{2})-T([\d.]+)$',
            ticker_upper,
        )
        if match:
            city_code, date_str, threshold_str = match.groups()
            city = self.CITY_CODES.get(city_code)
            if not city:
                logger.debug(f"Unknown city code '{city_code}' in rain ticker: {ticker}")
                return None
            market_date = self._parse_date(date_str)
            if not market_date:
                return None
            threshold = float(threshold_str)
            return {
                "city": city,
                "date": market_date,
                "threshold": threshold,
                "type": "above",
                "market_type": "rain",
                "is_monthly": False,
            }

        return None

    def _parse_weather_ticker(self, ticker: str) -> Optional[Dict]:
        """
        Unified parser: try temperature, then snow, then rain.

        Returns parsed dict or None if no parser matches.
        """
        result = self._parse_temperature_ticker(ticker)
        if result:
            return result

        result = self._parse_snow_ticker(ticker)
        if result:
            return result

        result = self._parse_rain_ticker(ticker)
        if result:
            return result

        return None

    def parse_ticker_with_direction_fix(self, ticker: str) -> Optional[Dict]:
        """
        Parse ticker and fix threshold direction using MarketDB title.

        For temperature T-tickers, the parser can't distinguish bottom-tail
        ("below") from top-tail ("above") using the ticker alone. The
        MarketDB title contains '<' or '>' which tells us the true direction.

        Without this fix, ALL KXLOWT T-tickers get type="below" and ALL
        KXHIGH T-tickers get type="above" — which is wrong for the opposite
        tail of each series. This causes inverted probability calculations.

        Use this method instead of _parse_weather_ticker when you need
        correct threshold direction (e.g., for probability calculations).
        """
        parsed = self._parse_weather_ticker(ticker)
        if not parsed:
            return None

        # Only fix T-tickers (non-brackets) for temperature markets
        if parsed.get("is_bracket", False) or parsed.get("market_type") != "temperature":
            return parsed

        # Look up MarketDB title to determine true threshold direction
        try:
            from src.data.database import get_db_session
            from src.data.models import MarketDB
            with next(get_db_session()) as session:
                market = session.query(MarketDB).filter(
                    MarketDB.ticker == ticker
                ).first()
                if market and market.title:
                    title = str(market.title)
                    if "<" in title:
                        parsed["type"] = "below"
                    elif ">" in title:
                        parsed["type"] = "above"
        except Exception:
            pass  # If DB lookup fails, keep parser default

        return parsed

    def _calculate_probability(
        self,
        forecast: TemperatureForecast,
        threshold: float,
        threshold_type: str,
        is_bracket: bool = False,
        temp_series: Optional[str] = None,
    ) -> Tuple[float, str]:
        """
        Calculate probability for a temperature market.

        For THRESHOLD markets (-T):
            P(temp > threshold) or P(temp < threshold)
        For BRACKET markets (-B):
            P(lower <= temp < upper) where the bracket is 2°F wide
            centered on the threshold value. E.g., B41.5 → bracket [41, 43).

        Uses a normal distribution centered on the NWS forecast with
        uncertainty based on forecast lead time and confidence level.

        For SAME-DAY markets, also checks if the outcome is already known
        (e.g., high temp already recorded past peak hours).

        Args:
            forecast: NWS temperature forecast
            threshold: Temperature threshold value
            threshold_type: "above" (P > threshold) or "below" (P < threshold)
                This is the PROBABILITY DIRECTION — determined from the market
                title (< or >) for T-tickers, NOT the series type.
            is_bracket: True for bracket markets (B-tickers)
            temp_series: "high" or "low" — which forecast temperature to use.
                "high" → forecast.high_f (for KXHIGH markets)
                "low" → forecast.low_f (for KXLOWT markets)
                If None, falls back to threshold_type for backward compatibility.

        NWS forecast accuracy studies show:
        - Day 1: RMSE ~3F for highs, ~4F for lows
        - Day 2: RMSE ~4F for highs, ~5F for lows
        - Day 3+: RMSE ~5-7F

        Returns:
            Tuple of (probability, reasoning)
        """
        # Check for same-day settlement using live observations
        days_out = (forecast.forecast_date - date.today()).days
        _obs_forecast_bias = None  # Will be set if live obs shifts our effective forecast

        if days_out == 0 and not is_bracket:
            # Same-day THRESHOLD market - check if outcome is already determined
            try:
                if hasattr(self.nws_client, 'check_same_day_settlement'):
                    settlement = self.nws_client.check_same_day_settlement(
                        city=forecast.city,
                        threshold=int(threshold),
                        threshold_type=threshold_type
                    )
                    if settlement:
                        prob, reason = settlement
                        logger.info(
                            f"\U0001f3af SAME-DAY SETTLED: {forecast.city} | "
                            f"Temp already {'exceeded' if threshold_type == 'above' else 'below'} "
                            f"{threshold}°F! Probability → {prob:.0%}"
                        )
                        return prob, reason
                # Threshold didn't settle — still check obs for forecast bias
                if hasattr(self.nws_client, 'get_current_observation'):
                    obs = self.nws_client.get_current_observation(forecast.city)
                    if obs:
                        if threshold_type == "above" and obs.observed_high_f > forecast.high_f:
                            _obs_forecast_bias = obs.observed_high_f * 0.6 + forecast.high_f * 0.4
                        elif threshold_type != "above" and obs.observed_low_f < forecast.low_f:
                            _obs_forecast_bias = obs.observed_low_f * 0.6 + forecast.low_f * 0.4
            except Exception as e:
                logger.debug(f"Same-day settlement check failed: {e}")
        if days_out == 0 and is_bracket:
            try:
                if hasattr(self.nws_client, 'get_current_observation'):
                    obs = self.nws_client.get_current_observation(forecast.city)
                    if obs:
                        bracket_lower = threshold - 1.0
                        bracket_upper = threshold + 1.0

                        if threshold_type == "above":
                            # HIGH bracket: observed_high is a running maximum
                            # Once exceeded, temp can only go higher — bracket is DEAD
                            observed_max = obs.observed_high_f

                            if observed_max >= bracket_upper:
                                # High already passed bracket upper → bracket DEAD
                                logger.info(
                                    f"\U0001f480 BRACKET DEAD: {forecast.city} | "
                                    f"Observed high {observed_max:.0f}°F ≥ bracket upper {bracket_upper:.0f}°F"
                                )
                                return 0.02, (
                                    f"BRACKET DEAD: observed high {observed_max:.0f}°F "
                                    f"already exceeded [{bracket_lower:.0f},{bracket_upper:.0f})°F"
                                )

                            if obs.is_past_peak_high:
                                if bracket_lower <= observed_max < bracket_upper:
                                    # Past peak, high fell IN bracket → WINNER
                                    logger.info(
                                        f"\U0001f3c6 BRACKET WINNER: {forecast.city} | "
                                        f"Past peak, observed high {observed_max:.0f}°F "
                                        f"in [{bracket_lower:.0f},{bracket_upper:.0f})°F"
                                    )
                                    return 0.95, (
                                        f"BRACKET WINNER: past peak hours, observed high "
                                        f"{observed_max:.0f}°F in [{bracket_lower:.0f},{bracket_upper:.0f})°F"
                                    )
                                elif observed_max < bracket_lower:
                                    # Past peak, high never reached bracket → DEAD
                                    logger.info(
                                        f"\U0001f480 BRACKET DEAD: {forecast.city} | "
                                        f"Past peak, high {observed_max:.0f}°F < bracket lower {bracket_lower:.0f}°F"
                                    )
                                    return 0.02, (
                                        f"BRACKET DEAD: past peak, observed high "
                                        f"{observed_max:.0f}°F never reached [{bracket_lower:.0f},{bracket_upper:.0f})°F"
                                    )

                            # Not past peak: bias forecast toward observation
                            # If observed temp exceeds our forecast, the NWS was low —
                            # blend observation into our effective forecast
                            if threshold_type == "above":
                                fcst_temp = forecast.high_f
                            else:
                                fcst_temp = forecast.low_f
                            if observed_max > fcst_temp:
                                # Weight: 60% observation, 40% forecast (obs is real data)
                                _obs_forecast_bias = observed_max * 0.6 + fcst_temp * 0.4
                                logger.info(
                                    f"\U0001f4c8 OBS BIAS: {forecast.city} | "
                                    f"Observed high {observed_max:.0f}°F > forecast {fcst_temp:.0f}°F "
                                    f"→ effective forecast {_obs_forecast_bias:.1f}°F"
                                )

                        else:
                            # LOW bracket: observed_low is a running minimum
                            observed_min = obs.observed_low_f

                            if observed_min < bracket_lower:
                                # Low already dropped below bracket → bracket DEAD
                                logger.info(
                                    f"\U0001f480 BRACKET DEAD: {forecast.city} | "
                                    f"Observed low {observed_min:.0f}°F < bracket lower {bracket_lower:.0f}°F"
                                )
                                return 0.02, (
                                    f"BRACKET DEAD: observed low {observed_min:.0f}°F "
                                    f"already below [{bracket_lower:.0f},{bracket_upper:.0f})°F"
                                )

                            if obs.is_past_sunrise:
                                if bracket_lower <= observed_min < bracket_upper:
                                    # Past sunrise, low fell IN bracket → WINNER
                                    return 0.95, (
                                        f"BRACKET WINNER: past sunrise, observed low "
                                        f"{observed_min:.0f}°F in [{bracket_lower:.0f},{bracket_upper:.0f})°F"
                                    )
                                elif observed_min >= bracket_upper:
                                    # Past sunrise, low never reached bracket → DEAD
                                    return 0.02, (
                                        f"BRACKET DEAD: past sunrise, observed low "
                                        f"{observed_min:.0f}°F never reached [{bracket_lower:.0f},{bracket_upper:.0f})°F"
                                    )

                            # Bias forecast toward observation
                            fcst_temp = forecast.low_f
                            if observed_min < fcst_temp:
                                _obs_forecast_bias = observed_min * 0.6 + fcst_temp * 0.4
                                logger.info(
                                    f"\U0001f4c9 OBS BIAS: {forecast.city} | "
                                    f"Observed low {observed_min:.0f}°F < forecast {fcst_temp:.0f}°F "
                                    f"→ effective forecast {_obs_forecast_bias:.1f}°F"
                                )

            except Exception as e:
                logger.debug(f"Bracket same-day observation check failed: {e}")

        # Select forecast temperature based on market series type, NOT threshold direction.
        # temp_series="high" → KXHIGH market → use high_f
        # temp_series="low"  → KXLOWT market → use low_f
        # This is critical: a KXLOWT market with an "above" threshold (e.g., T49 ">49°")
        # still needs the LOW temperature forecast, not the HIGH.
        _series = temp_series if temp_series else threshold_type  # backward compat
        if _series == "high" or (_series == "above" and temp_series is None):
            forecast_temp = forecast.high_f
        else:
            forecast_temp = forecast.low_f

        if forecast_temp == 0:
            return 0.5, "No forecast temperature available"

        # Apply observation bias: if live temp exceeds forecast, shift our model
        if _obs_forecast_bias is not None:
            logger.debug(
                f"Applying obs bias: forecast {forecast_temp:.0f}°F → {_obs_forecast_bias:.1f}°F"
            )
            forecast_temp = _obs_forecast_bias

        # Delegate σ selection + CDF math to WeatherCalculator (single source of truth).
        # Strategy layer handles observation logic above; core math lives in one place.
        _is_high = (_series in ("high", "above"))

        # Get local hour for same-day σ lookup
        local_hour = None
        if days_out == 0:
            try:
                from zoneinfo import ZoneInfo
                city_name = forecast.city if hasattr(forecast, 'city') else None
                tz_name = "America/New_York"
                if city_name:
                    tz_name = KALSHI_STATIONS.get(city_name, {}).get("timezone", tz_name)
                local_hour = datetime.now(ZoneInfo(tz_name)).hour
            except Exception:
                local_hour = 12

        from src.probability.weather import WeatherCalculator
        probability, reasoning, std_dev = WeatherCalculator().compute_probability(
            forecast_temp=forecast_temp,
            threshold=threshold,
            threshold_type=threshold_type,
            is_bracket=is_bracket,
            is_high=_is_high,
            days_out=days_out,
            confidence=forecast.high_confidence or "medium",
            local_hour=local_hour,
        )

        return probability, reasoning

    def _calculate_snow_probability(
        self,
        forecast: TemperatureForecast,
        threshold: float,
        is_monthly: bool,
    ) -> Tuple[float, str]:
        """
        Calculate probability for snow accumulation markets.

        For daily markets: based on temperature (snow needs temp < 35F)
        and general precipitation likelihood.
        For monthly markets: use historical snowfall averages as base rate,
        adjusted by current temperature forecasts.

        Returns:
            Tuple of (probability, reasoning)
        """
        high_temp = forecast.high_f
        low_temp = forecast.low_f

        if is_monthly:
            # Monthly snow: use temperature as a proxy for snow potential
            # Average monthly snowfall (inches) by city in winter months
            avg_monthly_snow = {
                "BOSTON": {"DEC": 12.9, "JAN": 14.8, "FEB": 12.0, "MAR": 8.0},
                "NYC": {"DEC": 4.8, "JAN": 7.0, "FEB": 8.5, "MAR": 4.2},
                "CHICAGO": {"DEC": 8.7, "JAN": 11.5, "FEB": 9.1, "MAR": 5.3},
                "PHILADELPHIA": {"DEC": 3.4, "JAN": 6.5, "FEB": 7.4, "MAR": 3.1},
                "DENVER": {"DEC": 7.7, "JAN": 6.8, "FEB": 6.6, "MAR": 11.3},
                "DALLAS": {"DEC": 0.5, "JAN": 0.8, "FEB": 0.7, "MAR": 0.1},
                "HOUSTON": {"DEC": 0.0, "JAN": 0.0, "FEB": 0.1, "MAR": 0.0},
                "WASHINGTON_DC": {"DEC": 2.5, "JAN": 5.7, "FEB": 5.1, "MAR": 2.0},
                "DETROIT": {"DEC": 10.2, "JAN": 13.5, "FEB": 10.1, "MAR": 6.1},
                "SALT_LAKE_CITY": {"DEC": 12.5, "JAN": 13.2, "FEB": 10.2, "MAR": 9.0},
            }
            city = forecast.city.upper()
            month_str = forecast.forecast_date.strftime("%b").upper()
            city_data = avg_monthly_snow.get(city, {})
            avg_snow = city_data.get(month_str, 6.0)  # Default 6" if unknown

            # Use normal distribution: P(snow > threshold)
            # std_dev is roughly 60% of mean for monthly snowfall
            std_dev = max(avg_snow * 0.6, 2.0)
            probability = 1.0 - _norm_cdf(threshold, loc=avg_snow, scale=std_dev)
            probability = max(0.02, min(0.98, probability))

            reasoning = (
                f"Monthly snow: avg={avg_snow:.1f}\" for {city} in {month_str}, "
                f"threshold={threshold}\", std_dev={std_dev:.1f}\". P={probability:.1%}"
            )
        else:
            # Daily snow: temperature must be below ~35F for snow
            # Use simple heuristic based on temperature
            avg_temp = (high_temp + low_temp) / 2 if low_temp else high_temp

            if avg_temp > 40:
                # Too warm for snow
                probability = 0.05
                reasoning = f"Too warm for snow (avg temp {avg_temp}F > 40F)"
            elif avg_temp > 35:
                # Marginal — could be rain or snow
                probability = 0.20
                reasoning = f"Marginal snow temp (avg {avg_temp}F, 35-40F range)"
            else:
                # Cold enough for snow; whether it actually snows depends on moisture
                # Higher threshold = lower probability of exceeding it
                if threshold <= 0.5:
                    # "Will it snow at all?" — if cold enough, ~40-60% base
                    probability = 0.50
                elif threshold <= 2:
                    probability = 0.35
                elif threshold <= 5:
                    probability = 0.20
                else:
                    probability = 0.10

                # Adjust for temperature (colder = more likely significant snow)
                if avg_temp < 25:
                    probability *= 1.2
                probability = max(0.02, min(0.98, probability))
                reasoning = (
                    f"Daily snow: avg_temp={avg_temp:.0f}F, threshold={threshold}\", "
                    f"P={probability:.1%}"
                )

        return probability, reasoning

    def _calculate_rain_probability(
        self,
        forecast: TemperatureForecast,
        threshold: float,
        is_monthly: bool,
    ) -> Tuple[float, str]:
        """
        Calculate probability for rain/precipitation markets.

        For daily: based on general precipitation likelihood.
        For monthly: use historical rainfall averages.

        Returns:
            Tuple of (probability, reasoning)
        """
        if is_monthly:
            # Average monthly rainfall (inches) by city
            avg_monthly_rain = {
                "NYC": {"JAN": 3.6, "FEB": 3.1, "MAR": 4.3, "APR": 4.5, "MAY": 4.2,
                         "JUN": 4.5, "JUL": 4.6, "AUG": 4.4, "SEP": 4.3, "OCT": 4.4,
                         "NOV": 3.6, "DEC": 3.9},
                "BOSTON": {"JAN": 3.4, "FEB": 3.3, "MAR": 4.3, "APR": 3.7, "MAY": 3.5,
                           "JUN": 3.7, "JUL": 3.4, "AUG": 3.4, "SEP": 3.5, "OCT": 3.8,
                           "NOV": 3.7, "DEC": 3.6},
                "CHICAGO": {"JAN": 2.1, "FEB": 1.8, "MAR": 2.7, "APR": 3.6, "MAY": 4.1,
                             "JUN": 4.1, "JUL": 3.5, "AUG": 4.6, "SEP": 3.3, "OCT": 3.4,
                             "NOV": 3.0, "DEC": 2.6},
                "MIAMI": {"JAN": 2.0, "FEB": 2.3, "MAR": 2.9, "APR": 3.3, "MAY": 5.3,
                           "JUN": 9.7, "JUL": 6.5, "AUG": 8.9, "SEP": 9.8, "OCT": 6.3,
                           "NOV": 3.3, "DEC": 2.2},
                "DALLAS": {"JAN": 2.3, "FEB": 2.8, "MAR": 3.5, "APR": 3.8, "MAY": 5.0,
                            "JUN": 3.9, "JUL": 2.0, "AUG": 2.2, "SEP": 3.1, "OCT": 4.5,
                            "NOV": 2.9, "DEC": 2.7},
                "HOUSTON": {"JAN": 3.7, "FEB": 3.0, "MAR": 3.4, "APR": 3.6, "MAY": 5.2,
                             "JUN": 5.9, "JUL": 3.3, "AUG": 4.8, "SEP": 5.4, "OCT": 4.8,
                             "NOV": 4.2, "DEC": 3.7},
                "DENVER": {"JAN": 0.5, "FEB": 0.5, "MAR": 1.3, "APR": 1.8, "MAY": 2.4,
                            "JUN": 1.6, "JUL": 2.2, "AUG": 1.8, "SEP": 1.3, "OCT": 1.0,
                            "NOV": 0.7, "DEC": 0.5},
                "AUSTIN": {"JAN": 2.3, "FEB": 2.0, "MAR": 2.8, "APR": 2.7, "MAY": 4.3,
                            "JUN": 3.9, "JUL": 1.8, "AUG": 2.2, "SEP": 3.3, "OCT": 3.9,
                            "NOV": 2.7, "DEC": 2.5},
                "PHILADELPHIA": {"JAN": 3.1, "FEB": 2.7, "MAR": 3.6, "APR": 3.5, "MAY": 3.5,
                                  "JUN": 3.7, "JUL": 4.4, "AUG": 3.8, "SEP": 4.1, "OCT": 3.1,
                                  "NOV": 3.1, "DEC": 3.4},
                "SEATTLE": {"JAN": 5.6, "FEB": 3.5, "MAR": 3.8, "APR": 2.7, "MAY": 2.0,
                             "JUN": 1.5, "JUL": 0.6, "AUG": 0.9, "SEP": 1.6, "OCT": 3.5,
                             "NOV": 6.2, "DEC": 5.6},
            }
            city = forecast.city.upper()
            month_str = forecast.forecast_date.strftime("%b").upper()
            city_data = avg_monthly_rain.get(city, {})
            avg_rain = city_data.get(month_str, 3.5)  # Default 3.5" if unknown

            std_dev = max(avg_rain * 0.5, 1.0)
            probability = 1.0 - _norm_cdf(threshold, loc=avg_rain, scale=std_dev)
            probability = max(0.02, min(0.98, probability))

            reasoning = (
                f"Monthly rain: avg={avg_rain:.1f}\" for {city} in {month_str}, "
                f"threshold={threshold}\", P={probability:.1%}"
            )
        else:
            # Daily rain: simple heuristic
            if threshold <= 0:
                # "Will it rain at all?" — base ~40% for most US cities
                probability = 0.40
            elif threshold <= 1:
                probability = 0.20
            elif threshold <= 2:
                probability = 0.10
            else:
                probability = 0.05

            probability = max(0.02, min(0.98, probability))
            reasoning = f"Daily rain: threshold={threshold}\", P={probability:.1%}"

        return probability, reasoning

    def _fetch_latest_prices(
        self, session, tickers: List[str]
    ) -> Dict[str, Dict[str, int]]:
        """
        Fetch latest PriceDB snapshot for a list of tickers.

        Returns dict mapping ticker -> {yes_ask, yes_bid, no_ask, no_bid}.
        """
        if not tickers:
            return {}

        # Subquery for max timestamp per ticker
        latest_ts = (
            session.query(
                PriceDB.ticker,
                func.max(PriceDB.timestamp).label("max_ts"),
            )
            .filter(PriceDB.ticker.in_(tickers))
            .group_by(PriceDB.ticker)
            .subquery()
        )

        price_rows = (
            session.query(PriceDB)
            .join(
                latest_ts,
                (PriceDB.ticker == latest_ts.c.ticker)
                & (PriceDB.timestamp == latest_ts.c.max_ts),
            )
            .all()
        )

        prices: Dict[str, Dict[str, int]] = {}
        for p in price_rows:
            prices[str(p.ticker)] = {
                "yes_ask": p.yes_ask,
                "yes_bid": p.yes_bid,
                "no_ask": p.no_ask,
                "no_bid": p.no_bid,
            }

        return prices

    def scan_markets(self) -> WeatherStrategyResult:
        """
        Scan all weather markets and generate trading opportunities.

        Returns:
            WeatherStrategyResult with opportunities and statistics
        """
        result = WeatherStrategyResult()

        try:
            with next(get_db_session()) as session:
                # Query using specific ticker prefixes to avoid false positives
                markets = session.query(MarketDB).filter(
                    MarketDB.status == "active",
                    or_(
                        # Temperature markets
                        MarketDB.ticker.like("KXHIGH%"),
                        MarketDB.ticker.like("KXLOWT%"),
                        # Snow markets (monthly + daily)
                        MarketDB.ticker.op("GLOB")("KX*SNOWM*"),
                        MarketDB.ticker.like("SNOW%"),
                        # Rain markets (monthly + daily)
                        MarketDB.ticker.like("KXRAIN%"),
                        MarketDB.ticker.like("RAIN%"),
                        # Category-based (catch-all)
                        MarketDB.category.ilike("%climate%"),
                        MarketDB.category.ilike("%weather%"),
                    ),
                ).all()

                # Filter out known false positives
                filtered_markets = []
                for m in markets:
                    t = str(m.ticker)
                    # Skip non-weather tickers that match broad patterns
                    if any(t.startswith(p) for p in (
                        "KXMODEL", "KXPGA", "KXTRUMP", "KXHMONTH",
                    )):
                        continue
                    filtered_markets.append(m)

                result.markets_scanned = len(filtered_markets)

                if not filtered_markets:
                    logger.info("No active weather markets found")
                    return result

                logger.info(f"Scanning {len(filtered_markets)} weather markets")

                # Fetch latest prices for all weather tickers
                all_tickers = [str(m.ticker) for m in filtered_markets]
                latest_prices = self._fetch_latest_prices(session, all_tickers)
                result.markets_with_price = len(latest_prices)

                logger.info(
                    f"Price data available for {len(latest_prices)}/{len(all_tickers)} "
                    f"weather markets"
                )

                # Group markets by city, date, and type for efficient NWS lookups
                market_groups: Dict[Tuple[str, date, str], List[Tuple[MarketDB, Dict]]] = {}

                for market in filtered_markets:
                    parsed = self._parse_weather_ticker(market.ticker)
                    if parsed:
                        # Fix threshold direction using title: "<X" → below, ">X" → above
                        # The parser can't distinguish low-end vs high-end T tickers
                        # from the ticker alone, but the title always says "<" or ">"
                        if not parsed.get("is_bracket", False) and market.title:
                            title = str(market.title)
                            if "<" in title:
                                parsed["type"] = "below"
                            elif ">" in title:
                                parsed["type"] = "above"

                        result.markets_parsed += 1
                        key = (parsed["city"], parsed["date"], parsed["market_type"])
                        if key not in market_groups:
                            market_groups[key] = []
                        market_groups[key].append((market, parsed))
                    else:
                        result.parse_failures.append(str(market.ticker))
                        logger.debug(
                            f"Could not parse weather ticker: {market.ticker} | "
                            f"{market.title}"
                        )

                if result.parse_failures:
                    logger.info(
                        f"Parse failures: {len(result.parse_failures)} tickers "
                        f"(first 5: {result.parse_failures[:5]})"
                    )

                # ── City filter: only trade markets we've validated ──
                _enabled_cities_str = os.environ.get(
                    "WEATHER_ENABLED_CITIES", "NYC,MIAMI,AUSTIN,LOS_ANGELES,CHICAGO"
                )
                _enabled_cities = {c.strip().upper() for c in _enabled_cities_str.split(",")}
                _skipped_city = 0

                # ── Daily-only filter: skip monthly and far-out markets ──
                only_daily = os.environ.get("WEATHER_ONLY_DAILY", "true").lower() == "true"
                max_days_out = int(os.environ.get("WEATHER_MAX_DAYS_OUT", "1"))
                skipped_monthly = 0
                skipped_far_out = 0
                skipped_past = 0

                # Fetch NWS forecasts and analyze each group
                for (city, market_date, market_type), market_list in market_groups.items():
                    # Skip cities we haven't validated
                    if city.upper() not in _enabled_cities:
                        _skipped_city += len(market_list)
                        continue

                    # Skip past dates (daily) or past months (monthly)
                    today = date.today()
                    is_monthly = market_list[0][1].get("is_monthly", False)
                    if is_monthly:
                        if only_daily:
                            # Skip ALL monthly markets when daily-only mode is on
                            skipped_monthly += len(market_list)
                            continue
                        # For monthly, skip if the month has already ended
                        import calendar
                        _, last_day = calendar.monthrange(market_date.year, market_date.month)
                        month_end = date(market_date.year, market_date.month, last_day)
                        if month_end < today:
                            skipped_past += len(market_list)
                            continue
                    else:
                        if market_date < today:
                            skipped_past += len(market_list)
                            continue
                        # Skip markets more than max_days_out away
                        days_out = (market_date - today).days
                        if days_out > max_days_out:
                            skipped_far_out += len(market_list)
                            continue

                    # Get NWS forecast (use today or tomorrow for monthly markets)
                    forecast_date = market_date if not is_monthly else today
                    forecast = self.nws_client.get_forecast(city, forecast_date)
                    if not forecast:
                        result.errors.append(
                            f"Could not get NWS forecast for {city} on {forecast_date}"
                        )
                        continue

                    result.forecasts_fetched += 1

                    # Analyze each market against the forecast
                    for market, parsed in market_list:
                        try:
                            # Get market price from PriceDB
                            price_data = latest_prices.get(str(market.ticker))
                            if price_data and price_data["yes_ask"] and price_data["yes_ask"] > 0:
                                market_price = price_data["yes_ask"] / 100.0
                            elif price_data and price_data["yes_bid"] and price_data["yes_bid"] > 0:
                                # Fallback to bid if no ask
                                market_price = price_data["yes_bid"] / 100.0
                            else:
                                # No price data — skip (can't trade without a price)
                                logger.debug(
                                    f"No price data for {market.ticker}, skipping"
                                )
                                continue

                            if market_price <= 0 or market_price >= 1:
                                market_price = 0.50  # Unreasonable price, use default

                            # Calculate probability based on market type
                            if market_type == "temperature":
                                our_prob, reasoning = self._calculate_probability(
                                    forecast,
                                    parsed["threshold"],
                                    parsed["type"],
                                    is_bracket=parsed.get("is_bracket", False),
                                    temp_series=parsed.get("temp_series"),
                                )
                                # Use temp_series for forecast temp (not threshold_type
                                # which may differ for upper-tail T-tickers)
                                _ts = parsed.get("temp_series", parsed["type"])
                                forecast_temp = (
                                    forecast.high_f
                                    if _ts in ("high", "above")
                                    else forecast.low_f
                                )
                            elif market_type == "snow":
                                our_prob, reasoning = self._calculate_snow_probability(
                                    forecast,
                                    parsed["threshold"],
                                    parsed.get("is_monthly", False),
                                )
                                forecast_temp = forecast.low_f  # Snow correlates with low temps
                            elif market_type == "rain":
                                our_prob, reasoning = self._calculate_rain_probability(
                                    forecast,
                                    parsed["threshold"],
                                    parsed.get("is_monthly", False),
                                )
                                forecast_temp = forecast.high_f
                            else:
                                continue

                            # Calculate edge from YES perspective
                            yes_edge = our_prob - market_price
                            # Calculate edge from NO perspective
                            no_edge = (1.0 - our_prob) - (1.0 - market_price)
                            # no_edge simplifies to market_price - our_prob = -yes_edge

                            # Determine recommendation based on DIRECTIONAL edge
                            if yes_edge >= self.min_edge:
                                recommendation = "BUY_YES"
                                edge = yes_edge  # Positive YES-side edge
                            elif no_edge >= self.min_edge:
                                recommendation = "BUY_NO"
                                edge = no_edge  # Positive NO-side edge
                            else:
                                recommendation = "NO_TRADE"
                                edge = yes_edge  # Store for logging

                            # Detect if live observation has confirmed the outcome
                            # (BRACKET DEAD, BRACKET WINNER, or SAME-DAY SETTLED)
                            _obs_settled = any(
                                kw in reasoning.upper()
                                for kw in ["BRACKET DEAD", "BRACKET WINNER", "SAME-DAY SETTLED"]
                            )

                            opportunity = WeatherOpportunity(
                                ticker=market.ticker,
                                city=city,
                                market_date=market_date,
                                threshold_temp=int(parsed["threshold"]),
                                threshold_type=parsed["type"],
                                nws_forecast_temp=forecast_temp,
                                nws_confidence=forecast.high_confidence,
                                market_price=market_price,
                                our_probability=our_prob,
                                edge=edge,
                                recommendation=recommendation,
                                reasoning=reasoning,
                                market_type=market_type,
                                observation_settled=_obs_settled,
                            )

                            if recommendation != "NO_TRADE" and edge >= self.MIN_EDGE_PCT:
                                result.opportunities.append(opportunity)
                                result.opportunities_found += 1

                        except Exception as e:
                            result.errors.append(f"Error analyzing {market.ticker}: {e}")

                # Sort by directional edge (highest positive edge first)
                result.opportunities.sort(key=lambda x: x.edge, reverse=True)

                logger.info(
                    f"Weather scan complete: {result.markets_scanned} scanned, "
                    f"{result.markets_parsed} parsed, "
                    f"{result.markets_with_price} with prices, "
                    f"{result.forecasts_fetched} forecasts, "
                    f"{result.opportunities_found} opportunities | "
                    f"Filtered out: {_skipped_city} disabled cities, "
                    f"{skipped_monthly} monthly, "
                    f"{skipped_far_out} too far out (>{max_days_out}d), "
                    f"{skipped_past} past | "
                    f"Enabled: {', '.join(sorted(_enabled_cities))}"
                )

        except Exception as e:
            logger.error(f"Weather strategy scan failed: {e}")
            result.errors.append(str(e))

        return result

    def generate_forecasts(self) -> int:
        """
        Generate forecast records in the database for signal generator.

        Returns:
            Number of forecasts created
        """
        result = self.scan_markets()
        forecasts_created = 0

        if not result.opportunities:
            logger.debug("No weather opportunities to save as forecasts")
            return 0

        try:
            with next(get_db_session()) as session:
                for opp in result.opportunities:
                    # Only create forecasts for tradeable opportunities
                    if opp.recommendation == "NO_TRADE":
                        continue

                    # Create forecast record
                    forecast = ForecastDB(
                        ticker=opp.ticker,
                        timestamp=datetime.now(timezone.utc),
                        probability=opp.our_probability,
                        confidence=self.CONFIDENCE_MULTIPLIERS.get(opp.nws_confidence, 0.75),
                        method="nws_weather",
                        edge=opp.edge,
                        market_probability=opp.market_price,
                        context_data=json.dumps({
                            "city": opp.city,
                            "market_date": opp.market_date.isoformat(),
                            "threshold_temp": opp.threshold_temp,
                            "threshold_type": opp.threshold_type,
                            "nws_forecast_temp": opp.nws_forecast_temp,
                            "nws_confidence": opp.nws_confidence,
                            "recommendation": opp.recommendation,
                            "reasoning": opp.reasoning,
                            "strategy": "weather",
                            "market_type": opp.market_type,
                        }),
                    )
                    session.add(forecast)
                    forecasts_created += 1

                    logger.info(
                        f"Weather forecast: {opp.ticker}",
                        city=opp.city,
                        threshold=f"{opp.threshold_temp}F",
                        edge=f"{opp.edge*100:.1f}%",
                        recommendation=opp.recommendation,
                    )

                session.commit()

        except Exception as e:
            logger.error(f"Failed to save weather forecasts: {e}")

        return forecasts_created

    def _check_plausibility(
        self,
        city: str,
        market_date: date,
        threshold: float,
        threshold_type: str,
        market_type: str,
        market_price: float,
        recommendation: str = "BUY_YES",
    ) -> Tuple[bool, str]:
        """
        Check if a weather market is physically plausible.

        Returns (is_plausible, rejection_reason).

        Checks:
        1. Snow blocklist (no snow in Miami/Austin/LA/Houston)
        2. Temperature extremes beyond 3 std devs from climatological mean
        3. YES-side only: Implied probability higher than climatological probability -> reject
           (Skipped for BUY_NO — an overpriced YES is exactly what creates the No-side edge)
        """
        # 1. Snow blocklist
        if market_type == "snow" and city in self.SNOW_IMPLAUSIBLE_CITIES:
            return False, f"Snow implausible in {city}"

        # 2. Temperature extremes beyond 3 standard deviations
        if market_type == "temperature":
            month = market_date.month
            normals = self.CLIMATE_NORMALS.get(city, {}).get(month)
            if normals:
                if threshold_type == "above":
                    mean = normals["high_mean"]
                    std = normals["high_std"]
                else:
                    mean = normals["low_mean"]
                    std = normals["low_std"]

                z_score = abs(threshold - mean) / std if std > 0 else 0
                if z_score > 3.0:
                    return False, (
                        f"Temperature {threshold}F is {z_score:.1f} std devs from "
                        f"{city} {threshold_type} normal ({mean:.0f}F ± {std:.0f}F)"
                    )

                # 3. Market price > climatological probability + 10%
                # Only applies to YES-side buys.  For NO-side (BUY_NO), a high
                # YES price is the *source* of edge — the market overvalues YES
                # so the NO side is cheap.  Don't reject these.
                if recommendation != "BUY_NO":
                    try:
                        if threshold_type == "above":
                            clim_prob = 1.0 - _norm_cdf(threshold, loc=mean, scale=std)
                        else:
                            clim_prob = _norm_cdf(threshold, loc=mean, scale=std)
                        clim_prob = max(0.01, min(0.99, clim_prob))

                        if market_price > clim_prob + 0.10:
                            return False, (
                                f"Market price {market_price:.0%} exceeds climatological "
                                f"probability {clim_prob:.0%} + 10% for {city} "
                                f"{threshold_type} {threshold}F"
                            )
                    except ImportError:
                        pass  # scipy not available, skip this check

        return True, ""

    def _get_local_hour(self, city: str) -> int:
        """Get current local hour (0-23) for a city."""
        try:
            from zoneinfo import ZoneInfo
            from src.data_sources.nws_weather import KALSHI_STATIONS
            tz_name = KALSHI_STATIONS.get(city, {}).get("timezone", "America/New_York")
            local_now = datetime.now(ZoneInfo(tz_name))
            return local_now.hour
        except Exception:
            return 12  # Default to midday if timezone lookup fails

    def scan_bracket_squeeze(self) -> WeatherStrategyResult:
        """
        Dual-side bracket squeeze: generate BOTH no_exclusion and yes_convergence
        opportunities from a single forecast per (city, date, type) group.

        For each group of brackets (e.g., NYC high temp 2/12):
        1. Fetch NWS forecast (e.g., 36F)
        2. Classify each bracket by distance from forecast:
           - Within CONVERGENCE_DISTANCE (2F): yes_convergence (buy YES, take profit intraday)
           - Beyond EXCLUSION_DISTANCE (5F) with prob > 80%: no_exclusion (buy NO, hold to resolution)
           - Middle zone: normal evaluation (trade_type stays "unknown")
        3. Apply plausibility filter to reject impossible events
        4. Apply time-of-day filter for same-day markets

        This wraps scan_markets() and enriches results with trade_type classification.
        """
        result = self.scan_markets()

        # Enrich each opportunity with trade_type based on bracket squeeze logic
        enriched_opps = []
        plausibility_rejects = 0

        for opp in result.opportunities:
            # Pre-compute effective recommendation for plausibility filter.
            # scan_markets() sets recommendation based on YES-side edge, but
            # for temperature brackets far from the forecast the bracket squeeze
            # will later reclassify them as BUY_NO (no_exclusion).  We need to
            # tell the plausibility filter NOW so it doesn't reject them for
            # having an "overpriced YES" — that's exactly what makes the NO cheap.
            effective_rec = opp.recommendation
            if opp.market_type == "temperature" and opp.nws_forecast_temp is not None:
                distance = abs(opp.threshold_temp - opp.nws_forecast_temp)
                if distance >= self.BRACKET_EXCLUSION_DISTANCE_F:
                    no_prob = 1.0 - opp.our_probability
                    if no_prob >= self.BRACKET_EXCLUSION_MIN_PROB:
                        effective_rec = "BUY_NO"

            # Plausibility filter (applies to ALL market types)
            # Pass effective recommendation so the filter knows whether we're
            # buying YES or NO — overpriced YES markets are rejected for YES
            # buys but are the *source of edge* for NO buys (bracket squeeze).
            is_plausible, reject_reason = self._check_plausibility(
                city=opp.city,
                market_date=opp.market_date,
                threshold=opp.threshold_temp,
                threshold_type=opp.threshold_type,
                market_type=opp.market_type,
                market_price=opp.market_price,
                recommendation=effective_rec,
            )
            if not is_plausible:
                result.parse_failures.append(f"PLAUSIBILITY: {opp.ticker} — {reject_reason}")
                plausibility_rejects += 1
                logger.info(f"⛔ PLAUSIBILITY REJECT: {opp.ticker} | {reject_reason}")
                continue

            # Non-temperature markets pass through without bracket classification
            if opp.market_type != "temperature":
                enriched_opps.append(opp)
                continue

            # Time-of-day awareness for same-day markets
            # Previously: hard cutoff at 2 PM blocked all afternoon trades.
            # Now: allow trading all day — by afternoon we have BETTER info
            # (actual temps already recorded, weather data more certain).
            # Only block if market is essentially settled (e.g., 23:00+).
            days_out = (opp.market_date - date.today()).days
            entry_cutoff = int(os.environ.get("WEATHER_ENTRY_CUTOFF_HOUR", "23"))
            if days_out == 0:
                local_hour = self._get_local_hour(opp.city)
                if local_hour >= entry_cutoff:
                    result.parse_failures.append(
                        f"TOO_LATE: {opp.ticker} — {opp.city} at {local_hour}:00 local"
                    )
                    logger.debug(
                        f"Skip same-day entry {opp.ticker}: {local_hour}:00 local >= {entry_cutoff}:00 cutoff"
                    )
                    continue

            # Classify by distance from forecast
            distance = abs(opp.threshold_temp - opp.nws_forecast_temp)

            # ── THRESHOLD markets (outer limits: "X or below" / "X or above") ──
            # These are NOT brackets — they're cumulative tail bets.
            # Near forecast: treat like convergence (YES on underpriced tail)
            # Far from forecast: keep the initial recommendation from scan_markets
            parsed_ticker = self._parse_weather_ticker(opp.ticker)
            is_bracket = parsed_ticker.get("is_bracket", False) if parsed_ticker else True

            if not is_bracket:
                if distance <= self.BRACKET_CONVERGENCE_DISTANCE_F + 1:
                    # Threshold near forecast: keep YES if underpriced, skip NO
                    # e.g., Miami "81° or above" with forecast 80°F — YES is reasonable
                    if opp.recommendation == "BUY_YES" and opp.edge >= self.min_edge:
                        opp.trade_type = "yes_convergence"
                    elif opp.recommendation == "BUY_NO" and opp.edge >= self.min_edge:
                        # Don't bet NO on a threshold near the forecast
                        opp.trade_type = "unknown"
                        opp.recommendation = "NO_TRADE"
                        opp.edge = 0.0
                    else:
                        opp.trade_type = "threshold"
                else:
                    # Far threshold: keep whatever edge calculation says
                    opp.trade_type = "threshold"

                if opp.edge >= self.min_edge and opp.recommendation != "NO_TRADE":
                    enriched_opps.append(opp)
                continue

            # ── BRACKET markets (2°F ranges like "37-38°") ──
            if distance <= self.BRACKET_CONVERGENCE_DISTANCE_F:
                # Near forecast: primarily YES convergence zone.
                # Buy YES on underpriced brackets near the forecast.
                # BUT if YES edge is negative and NO side has strong edge,
                # allow NO (e.g., adjacent brackets the market overprices).
                yes_edge = opp.our_probability - opp.market_price

                if yes_edge >= self.min_edge:
                    # Positive YES-side edge near forecast — classic convergence trade
                    opp.trade_type = "yes_convergence"
                    opp.recommendation = "BUY_YES"
                    opp.edge = yes_edge
                else:
                    # YES edge insufficient — check NO side before killing
                    no_prob = 1.0 - opp.our_probability
                    no_price = 1.0 - opp.market_price
                    no_edge = no_prob - no_price
                    if no_edge >= self.min_edge and no_prob >= 0.70:
                        # Strong NO edge + high NO probability — market
                        # overprices YES on this adjacent bracket
                        opp.trade_type = "no_middle"
                        opp.recommendation = "BUY_NO"
                        opp.edge = no_edge
                    else:
                        # Neither side has edge — skip
                        opp.trade_type = "unknown"
                        opp.recommendation = "NO_TRADE"
                        opp.edge = 0.0

            elif distance >= self.BRACKET_EXCLUSION_DISTANCE_F:
                # Far from forecast: NO exclusion candidate
                # Calculate NO-side probability (probability bracket does NOT hit)
                no_prob = 1.0 - opp.our_probability
                if no_prob >= self.BRACKET_EXCLUSION_MIN_PROB:
                    opp.trade_type = "no_exclusion"
                    opp.recommendation = "BUY_NO"
                    # Recalculate edge from NO perspective
                    no_price = 1.0 - opp.market_price
                    opp.edge = no_prob - no_price  # Edge on NO side
                    logger.info(
                        f"🎯 NO_EXCLUSION: {opp.ticker} | "
                        f"dist={distance:.0f}F | NO_prob={no_prob:.0%} | "
                        f"NO_price={no_price:.0%} | edge={opp.edge:.0%}"
                    )
                else:
                    opp.trade_type = "unknown"
                    opp.recommendation = "NO_TRADE"
                    opp.edge = 0.0
            else:
                # Middle zone (between convergence and exclusion):
                # Allow YES or NO based on which side has edge.
                # YES: bracket is underpriced → buy cheap, hope forecast is a degree off
                # NO: bracket is overpriced and far-ish from forecast → likely won't hit
                yes_edge = opp.our_probability - opp.market_price
                no_prob = 1.0 - opp.our_probability
                no_price = 1.0 - opp.market_price
                no_edge = no_prob - no_price

                if yes_edge >= self.min_edge:
                    opp.trade_type = "yes_middle"
                    opp.recommendation = "BUY_YES"
                    opp.edge = yes_edge
                elif no_edge >= self.min_edge and no_prob >= 0.70:
                    # Require 70% NO probability in middle zone (stricter than exclusion)
                    opp.trade_type = "no_middle"
                    opp.recommendation = "BUY_NO"
                    opp.edge = no_edge
                else:
                    opp.trade_type = "unknown"
                    opp.recommendation = "NO_TRADE"
                    opp.edge = 0.0

            # Only include if there's enough POSITIVE directional edge
            # Edge should always be positive at this point (recalculated per side)
            # but guard against any path that didn't recalculate
            if opp.edge >= self.min_edge and opp.recommendation != "NO_TRADE":
                enriched_opps.append(opp)

        if plausibility_rejects > 0:
            logger.info(f"Plausibility filter rejected {plausibility_rejects} markets")

        result.opportunities = enriched_opps
        result.opportunities_found = len(enriched_opps)

        # Sort: yes_convergence first (hold-to-settlement winners), then no_exclusion, then unknown
        # Sort by directional edge (highest positive edge first within each type)
        type_priority = {"yes_convergence": 0, "no_exclusion": 1, "unknown": 2}
        result.opportunities.sort(
            key=lambda x: (type_priority.get(x.trade_type, 2), -x.edge)
        )

        return result

    def build_city_portfolios(self) -> List[CityBracketPortfolio]:
        """
        Build bracket portfolios for all active cities.

        Instead of returning individual WeatherOpportunity objects, this groups
        all temperature brackets for each (city, date) and builds a portfolio
        view using the CityPortfolioManager.

        Coexists with scan_markets() / scan_bracket_squeeze() — the main loop
        can call either approach.

        Returns:
            List of CityBracketPortfolio objects with entry orders set.
        """
        portfolio_mgr = get_city_portfolio_manager()
        portfolios: List[CityBracketPortfolio] = []

        try:
            with next(get_db_session()) as session:
                # Query temperature bracket markets only (not threshold, snow, or rain)
                markets = session.query(MarketDB).filter(
                    MarketDB.status == "active",
                    or_(
                        MarketDB.ticker.like("KXHIGH%"),
                        MarketDB.ticker.like("KXLOWT%"),
                    ),
                ).all()

                if not markets:
                    logger.info("No active temperature markets found for portfolio build")
                    return portfolios

                # Parse and group by (city, date, market_type)
                # Only include bracket markets (-B tickers) for the 5 active cities
                active_cities = set(portfolio_mgr.ACTIVE_CITIES)
                today = date.today()
                max_days_out = int(os.environ.get("WEATHER_MAX_DAYS_OUT", "1"))

                # Group: (city, date, threshold_type) -> list of parsed brackets
                bracket_groups: Dict[Tuple[str, date, str], List[Tuple[str, Dict]]] = {}

                for market in markets:
                    parsed = self._parse_temperature_ticker(str(market.ticker))
                    if not parsed:
                        continue
                    if not parsed.get("is_bracket", False):
                        continue  # Only bracket markets for portfolio
                    if parsed["city"] not in active_cities:
                        continue
                    if parsed["date"] < today:
                        continue
                    if (parsed["date"] - today).days > max_days_out:
                        continue

                    # Fix threshold direction from title
                    if market.title:
                        title = str(market.title)
                        if "<" in title:
                            parsed["type"] = "below"
                        elif ">" in title:
                            parsed["type"] = "above"

                    key = (parsed["city"], parsed["date"], parsed["type"])
                    if key not in bracket_groups:
                        bracket_groups[key] = []
                    bracket_groups[key].append((str(market.ticker), parsed))

                if not bracket_groups:
                    logger.info("No bracket markets found for active cities")
                    return portfolios

                # Fetch prices for all relevant tickers
                all_tickers = [
                    ticker
                    for group in bracket_groups.values()
                    for ticker, _ in group
                ]
                latest_prices = self._fetch_latest_prices(session, all_tickers)

                # Build a portfolio for each (city, date, type) group
                for (city, market_date, threshold_type), group in bracket_groups.items():
                    # Fetch NWS forecast
                    forecast = self.nws_client.get_forecast(city, market_date)
                    if not forecast:
                        logger.warning(
                            f"No NWS forecast for {city} on {market_date}, "
                            f"skipping portfolio"
                        )
                        continue

                    forecast_temp = (
                        forecast.high_f
                        if threshold_type == "above"
                        else forecast.low_f
                    )
                    if forecast_temp == 0:
                        continue

                    # Build bracket list for CityPortfolioManager
                    brackets = []
                    for ticker, parsed in group:
                        brackets.append({
                            "ticker": ticker,
                            "threshold": parsed["threshold"],
                            "type": parsed["type"],
                            "is_bracket": True,
                        })

                    # Calculate lead_days from market_date
                    lead_days = max(0, (market_date - today).days)

                    # Extract NWS confidence if available
                    nws_confidence = getattr(forecast, "high_confidence", "medium") or "medium"

                    # Calculate hours_to_close from market close_time
                    # This enables time-of-day uncertainty reduction for same-day markets
                    hours_to_close = None
                    try:
                        # Get close_time from any market in this group
                        sample_ticker = group[0][0]
                        sample_market = session.query(MarketDB).filter(
                            MarketDB.ticker == sample_ticker
                        ).first()
                        if sample_market and sample_market.close_time:
                            close_str = str(sample_market.close_time)
                            from datetime import datetime as dt
                            if "+" in close_str or close_str.endswith("Z"):
                                close_dt = dt.fromisoformat(close_str.replace("Z", "+00:00"))
                            else:
                                close_dt = dt.fromisoformat(close_str).replace(
                                    tzinfo=timezone.utc
                                )
                            now = dt.now(timezone.utc)
                            hours_to_close = max(0.0, (close_dt - now).total_seconds() / 3600.0)
                    except Exception as e:
                        logger.debug(f"Could not calculate hours_to_close for {city}/{market_date}: {e}")

                    # Get current observation for uncertainty reduction
                    current_obs = None
                    try:
                        obs = self.nws_client.get_station_observation(city)
                        if obs and obs.current_temp_f is not None:
                            current_obs = float(obs.current_temp_f)
                    except Exception:
                        pass

                    # Build portfolio with dynamic uncertainty
                    portfolio = portfolio_mgr.build_portfolio(
                        city=city,
                        market_date=market_date,
                        forecast_temp=forecast_temp,
                        brackets=brackets,
                        current_prices=latest_prices,
                        lead_days=lead_days,
                        confidence=nws_confidence,
                        hours_to_close=hours_to_close,
                        current_observation=current_obs,
                        is_high=(threshold_type == "above"),
                    )

                    if portfolio.brackets:
                        portfolios.append(portfolio)
                        entry_order = portfolio_mgr.get_entry_order(portfolio)
                        logger.info(
                            f"📊 PORTFOLIO: {city} {market_date} "
                            f"{'HIGH' if threshold_type == 'above' else 'LOW'} "
                            f"| forecast={forecast_temp:.0f}F "
                            f"| {len(portfolio.brackets)} brackets "
                            f"| total_edge={portfolio.total_edge:.1%} "
                            f"| entry_order: "
                            + ", ".join(
                                f"{bv.ticker}({bv.role.value})"
                                for bv in entry_order[:5]
                            )
                        )

        except Exception as e:
            logger.error(f"Failed to build city portfolios: {e}")

        return portfolios

    # ── Temperature Laddering (neobrother-style) ─────────────────────
    # Instead of betting on a single bracket, spread small bets across
    # 3-5 adjacent brackets near the NWS forecast. This guarantees at
    # least one winner per temperature event even when σ calibration is
    # imperfect.

    LADDER_MAX_DISTANCE_F: float = 6.0   # ±6°F from forecast
    LADDER_MIN_EDGE: float = 0.03        # 3% (lower than normal 5% — ladder diversifies)
    LADDER_MAX_YES_PRICE: float = 0.20   # Only buy YES at ≤ 20¢
    LADDER_MAX_BRACKETS: int = 5         # Max brackets per ladder
    LADDER_MAX_PER_BRACKET_DOLLARS: float = 2.00  # $2 cap per bracket

    def generate_ladder_opportunities(self) -> List[WeatherOpportunity]:
        """
        Generate temperature ladder opportunities: small YES bets across
        3-5 adjacent brackets near the NWS forecast.

        Each ladder is treated as ONE YES position for the YES cap.
        Ladder opportunities have trade_type="yes_ladder".

        Returns:
            List of WeatherOpportunity objects with trade_type="yes_ladder".
        """
        ladders: List[WeatherOpportunity] = []

        try:
            from src.probability.weather import WeatherCalculator
            calc = WeatherCalculator()

            with next(get_db_session()) as session:
                # Find all active bracket markets (B-type) for temperature
                bracket_markets = session.query(MarketDB).filter(
                    MarketDB.status == "active",
                    or_(
                        MarketDB.ticker.like("KXHIGH%"),
                        MarketDB.ticker.like("KXLOWT%"),
                    ),
                ).all()

                if not bracket_markets:
                    return ladders

                # Parse all bracket tickers and group by (city, date, series)
                groups: Dict[tuple, List[tuple]] = {}  # (city, date, series) -> [(market, parsed)]
                for mkt in bracket_markets:
                    parsed = self._parse_weather_ticker(mkt.ticker)
                    if not parsed or not parsed.get("is_bracket"):
                        continue
                    key = (parsed["city"], parsed["date"], parsed.get("temp_series", "high"))
                    if key not in groups:
                        groups[key] = []
                    groups[key].append((mkt, parsed))

                # City and date filters (same as scan_markets)
                _enabled_str = os.environ.get(
                    "WEATHER_ENABLED_CITIES", "NYC,MIAMI,AUSTIN,LOS_ANGELES,CHICAGO"
                )
                _enabled = {c.strip().upper() for c in _enabled_str.split(",")}
                today = date.today()
                max_days_out = int(os.environ.get("WEATHER_MAX_DAYS_OUT", "1"))

                for (city, market_date, temp_series), market_list in groups.items():
                    if city.upper() not in _enabled:
                        continue
                    if market_date < today:
                        continue
                    days_out = (market_date - today).days
                    if days_out > max_days_out:
                        continue

                    # Get NWS forecast
                    forecast = self.nws_client.get_forecast(city, market_date)
                    if not forecast:
                        continue

                    is_high = (temp_series == "high")
                    forecast_temp = forecast.high_f if is_high else forecast.low_f
                    if not forecast_temp or forecast_temp == 0:
                        continue

                    # Get local hour for same-day σ
                    local_hour = self._get_local_hour(city) if days_out == 0 else None

                    # Sort brackets by threshold (ascending)
                    market_list.sort(key=lambda x: x[1]["threshold"])

                    # Fetch prices for these tickers
                    tickers = [str(m.ticker) for m, _ in market_list]
                    prices = self._fetch_latest_prices(session, tickers)

                    # Select brackets within ±LADDER_MAX_DISTANCE_F of forecast
                    candidates = []
                    for mkt, parsed in market_list:
                        threshold = parsed["threshold"]
                        distance = abs(threshold - forecast_temp)
                        if distance > self.LADDER_MAX_DISTANCE_F:
                            continue

                        price_data = prices.get(str(mkt.ticker))
                        if not price_data:
                            continue

                        # YES price: use ask if available, otherwise bid
                        yes_ask = price_data.get("yes_ask") or 0
                        yes_bid = price_data.get("yes_bid") or 0
                        yes_price_cents = yes_ask if yes_ask > 0 else yes_bid
                        if yes_price_cents <= 0:
                            continue

                        yes_price = yes_price_cents / 100.0

                        # Guard: only buy YES at ≤ 20¢
                        if yes_price > self.LADDER_MAX_YES_PRICE:
                            continue

                        # Calculate probability via unified path
                        probability, reasoning, std_dev = calc.compute_probability(
                            forecast_temp=forecast_temp,
                            threshold=threshold,
                            threshold_type=parsed["type"],
                            is_bracket=True,
                            is_high=is_high,
                            days_out=days_out,
                            confidence=forecast.high_confidence or "medium",
                            local_hour=local_hour,
                        )

                        # Edge: our probability minus market price
                        edge = probability - yes_price
                        if edge < self.LADDER_MIN_EDGE:
                            continue

                        candidates.append((mkt, parsed, yes_price, probability, edge, reasoning))

                    if not candidates:
                        continue

                    # Sort by proximity to forecast (closest first), then by edge
                    candidates.sort(key=lambda c: (abs(c[1]["threshold"] - forecast_temp), -c[4]))

                    # Take up to LADDER_MAX_BRACKETS
                    selected = candidates[:self.LADDER_MAX_BRACKETS]

                    if len(selected) < 2:
                        # A single bracket isn't a ladder — let normal logic handle it
                        continue

                    ladder_id = f"{city}_{market_date.isoformat()}_{temp_series}"
                    logger.info(
                        f"🪜 LADDER: {ladder_id} | forecast={forecast_temp:.0f}°F | "
                        f"{len(selected)} brackets | "
                        f"edges={', '.join(f'{c[4]:.0%}' for c in selected)}"
                    )

                    for mkt, parsed, yes_price, probability, edge, reasoning in selected:
                        opp = WeatherOpportunity(
                            ticker=str(mkt.ticker),
                            city=city,
                            market_date=market_date,
                            threshold_temp=int(parsed["threshold"]),
                            threshold_type=parsed["type"],
                            nws_forecast_temp=forecast_temp,
                            nws_confidence=forecast.high_confidence or "medium",
                            market_price=yes_price,
                            our_probability=probability,
                            edge=edge,
                            recommendation="BUY_YES",
                            reasoning=f"LADDER[{ladder_id}]: {reasoning}",
                            market_type="temperature",
                            trade_type="yes_ladder",
                            observation_settled=False,
                        )
                        ladders.append(opp)

        except Exception as e:
            logger.error(f"Ladder opportunity generation failed: {e}")

        if ladders:
            logger.info(f"🪜 Generated {len(ladders)} ladder opportunities")

        return ladders

    def get_tradeable_opportunities(self) -> List[WeatherOpportunity]:
        """
        Get all opportunities with edge >= threshold, using bracket squeeze logic.
        Also includes temperature ladder opportunities (yes_ladder).

        Returns:
            List of tradeable WeatherOpportunity objects with trade_type classification.
        """
        result = self.scan_bracket_squeeze()
        opportunities = [opp for opp in result.opportunities if opp.recommendation != "NO_TRADE"]

        # Add ladder opportunities, skipping tickers already covered by squeeze
        existing_tickers = {opp.ticker for opp in opportunities}
        ladder_opps = self.generate_ladder_opportunities()
        for opp in ladder_opps:
            if opp.ticker not in existing_tickers:
                opportunities.append(opp)
                existing_tickers.add(opp.ticker)

        return opportunities


# Singleton instance
_weather_strategy: Optional[WeatherStrategy] = None


def get_weather_strategy(min_edge: float = 0.05) -> WeatherStrategy:
    """Get or create weather strategy instance."""
    global _weather_strategy
    if _weather_strategy is None:
        _weather_strategy = WeatherStrategy(min_edge=min_edge)
    return _weather_strategy


# Test function
if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)

    print("=" * 60)
    print("Weather Strategy Test")
    print("=" * 60)

    strategy = WeatherStrategy()
    result = strategy.scan_markets()

    print(f"\nMarkets scanned: {result.markets_scanned}")
    print(f"Markets parsed: {result.markets_parsed}")
    print(f"Markets with prices: {result.markets_with_price}")
    print(f"Forecasts fetched: {result.forecasts_fetched}")
    print(f"Opportunities found: {result.opportunities_found}")

    if result.parse_failures:
        print(f"\nParse failures ({len(result.parse_failures)}):")
        for ticker in result.parse_failures[:10]:
            print(f"  {ticker}")

    if result.opportunities:
        print("\nTop Opportunities:")
        for opp in result.opportunities[:5]:
            print(f"  {opp.ticker}: {opp.recommendation} [{opp.market_type}]")
            print(f"    Edge: {opp.edge*100:.1f}%")
            print(f"    NWS: {opp.nws_forecast_temp}F vs threshold {opp.threshold_temp}F")
            print(f"    Market price: {opp.market_price:.2f}")
            print(f"    {opp.reasoning}")

    if result.errors:
        print(f"\nErrors: {result.errors}")
