"""Shared probability utilities. Single source of truth for CDF calculations."""
from math import erf, sqrt


def norm_cdf(x: float, loc: float = 0.0, scale: float = 1.0) -> float:
    """Normal distribution CDF using math.erf (no scipy dependency).

    Equivalent to scipy.stats.norm.cdf(x, loc=loc, scale=scale).
    """
    return 0.5 * (1.0 + erf((x - loc) / (scale * sqrt(2.0))))
