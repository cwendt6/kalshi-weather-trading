"""
Shared data structures and types used across multiple modules.

This module contains dataclasses and types that are used by multiple
components but don't belong to a specific domain.
"""
from dataclasses import dataclass


@dataclass
class MarketMatch:
    """A matched pair of Kalshi and Polymarket markets."""

    kalshi_ticker: str
    kalshi_title: str
    polymarket_condition_id: str
    polymarket_title: str
    match_score: float  # 0-1, similarity score
