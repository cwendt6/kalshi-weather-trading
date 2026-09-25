"""
Probability Engine: Routes weather markets to the weather calculator
and produces probability estimates.

Architecture:
    Market → classify domain → route to calculator → ensemble → Forecast
"""
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from src.utils.logging import logger


@dataclass
class ProbabilityEstimate:
    """Output from a single probability calculator."""
    probability: float       # 0.0 to 1.0
    confidence: float        # 0.0 to 1.0 (how much to trust this estimate)
    method: str              # e.g. "nws_normal"
    reasoning: str           # human-readable explanation
    data_quality: str = "good"  # "good", "stale", "partial", "unavailable"
    features: Dict = field(default_factory=dict)  # raw features for logging


@dataclass
class EnsembleResult:
    """Combined probability from multiple calculators."""
    probability: float
    confidence: float
    estimates: List[ProbabilityEstimate]
    method: str = "ensemble"
    reasoning: str = ""


# Market domain classification patterns
WEATHER_PATTERNS = [
    r"KXHIGH\w+",   # High temperature markets
    r"KXLOW\w+",    # Low temperature markets
    r"KXRAIN\w+",   # Rainfall markets
    r"KXSNOW\w+",   # Snowfall markets
    r"SNOW\w+",     # Daily snow markets
    r"RAIN\w+",     # Daily rain markets
]


def classify_market(ticker: str, title: str = "", category: str = "") -> str:
    """
    Classify a market into a domain for routing to the right calculator.

    Returns: "weather" or "unknown"
    """
    ticker_upper = ticker.upper()
    title_lower = title.lower()
    category_lower = category.lower()

    # Ticker-based classification (most reliable)
    for pattern in WEATHER_PATTERNS:
        if re.match(pattern, ticker_upper):
            return "weather"

    # Category-based fallback
    if category_lower in ("climate and weather", "weather", "climate"):
        return "weather"

    # Title-based fallback
    weather_words = ["temperature", "temp", "high temp", "low temp", "rain", "snow", "weather"]
    for word in weather_words:
        if word in title_lower:
            return "weather"

    return "unknown"


class ProbabilityEngine:
    """
    Main entry point for probability calculation.

    Routes weather markets to the weather calculator and produces
    ensemble estimates.
    """

    def __init__(self):
        self._weather_calc = None
        logger.info("ProbabilityEngine initialized")

    @property
    def weather(self):
        if self._weather_calc is None:
            from src.probability.weather import WeatherCalculator
            self._weather_calc = WeatherCalculator()
        return self._weather_calc

    def estimate(
        self,
        ticker: str,
        title: str = "",
        category: str = "",
        current_price_cents: int = 50,
        close_time: Optional[datetime] = None,
        context: Optional[Dict] = None,
    ) -> EnsembleResult:
        """
        Compute probability estimate for a Kalshi market.

        Args:
            ticker: Market ticker (e.g., "KXHIGHNY-26FEB07-B28.5")
            title: Market title/question
            category: Market category from Kalshi
            current_price_cents: Current YES price in cents (1-99)
            close_time: When the market closes/resolves
            context: Additional context (volume, open_interest, etc.)

        Returns:
            EnsembleResult with combined probability and individual estimates
        """
        context = context or {}
        domain = classify_market(ticker, title, category)
        estimates: List[ProbabilityEstimate] = []

        # 1. Domain-specific calculator
        try:
            if domain == "weather":
                est = self.weather.calculate(ticker, title, close_time, context)
                if est:
                    estimates.append(est)
        except Exception as e:
            logger.warning(f"Domain calculator ({domain}) failed for {ticker}: {e}")

        # 2. Price anchor baseline (always available)
        price_prob = current_price_cents / 100.0
        estimates.append(ProbabilityEstimate(
            probability=price_prob,
            confidence=0.3,  # Low confidence — market could be wrong
            method="price_anchor",
            reasoning=f"Market price implies {price_prob:.0%} probability",
            features={"market_price_cents": current_price_cents},
        ))

        # 3. Combine into ensemble
        return self._ensemble(estimates, domain)

    def _ensemble(
        self, estimates: List[ProbabilityEstimate], domain: str
    ) -> EnsembleResult:
        """
        Combine multiple probability estimates using confidence-weighted average.

        Domain-specific calculators get higher weight than generic ones.
        """
        if not estimates:
            return EnsembleResult(
                probability=0.5,
                confidence=0.0,
                estimates=[],
                reasoning="No calculators produced an estimate",
            )

        # Weight by confidence, with domain calculators getting a bonus
        total_weight = 0.0
        weighted_sum = 0.0
        reasoning_parts = []

        for est in estimates:
            # Domain-specific methods get 2x weight bonus
            is_domain = est.method not in ("price_anchor",)
            weight = est.confidence * (2.0 if is_domain else 1.0)

            # Skip unavailable data
            if est.data_quality == "unavailable":
                continue

            weighted_sum += est.probability * weight
            total_weight += weight
            reasoning_parts.append(
                f"{est.method}: {est.probability:.1%} (conf={est.confidence:.0%})"
            )

        if total_weight == 0:
            ensemble_prob = 0.5
            ensemble_conf = 0.0
        else:
            ensemble_prob = weighted_sum / total_weight
            max_conf = max(e.confidence for e in estimates)
            probs = [e.probability for e in estimates if e.data_quality != "unavailable"]
            if len(probs) > 1:
                spread = max(probs) - min(probs)
                agreement = max(0, 1.0 - spread * 2)
            else:
                agreement = 0.5
            ensemble_conf = max_conf * (0.5 + 0.5 * agreement)

        # Clamp probability to reasonable range
        ensemble_prob = max(0.01, min(0.99, ensemble_prob))

        return EnsembleResult(
            probability=ensemble_prob,
            confidence=ensemble_conf,
            estimates=estimates,
            method=f"ensemble_{domain}",
            reasoning=" | ".join(reasoning_parts),
        )


# Singleton
_engine: Optional[ProbabilityEngine] = None


def get_probability_engine() -> ProbabilityEngine:
    global _engine
    if _engine is None:
        _engine = ProbabilityEngine()
    return _engine
