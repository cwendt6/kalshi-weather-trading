"""
City-based budget allocator for the weather trading system.

Replaces the old per-event budget system ($15/$20/$30 per event) with a
city-level allocation that respects both portfolio value and available cash.

Core principle: city_budget = min(portfolio_pct * total_portfolio, cash_pct * available_cash)
The CASH constraint always wins — we can never deploy more than we actually have.

Example with portfolio=$112, cash=$50, pct=10%:
  Portfolio alloc per city: $112 * 0.10 = $11.20
  Cash alloc per city:      $50  * 0.10 = $5.00
  City budget = min($11.20, $5.00) = $5.00

Then spread $5.00 across the best YES and NO bets for that city.
"""

import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from src.utils.logging import logger


@dataclass
class CityAllocation:
    """Budget allocation for a single city."""

    city: str
    budget_dollars: float  # min(portfolio_alloc, cash_alloc)
    portfolio_alloc: float  # portfolio_pct * total_portfolio
    cash_alloc: float  # cash_pct * available_cash
    remaining_dollars: float = 0.0  # Decremented as trades execute
    trades_placed: int = 0
    total_spent: float = 0.0

    def __post_init__(self) -> None:
        self.remaining_dollars = self.budget_dollars

    def can_afford(self, cost: float) -> bool:
        """Check if this city's budget can afford a trade."""
        return self.remaining_dollars >= cost and cost > 0

    def spend(self, cost: float) -> None:
        """Deduct a trade cost from this city's remaining budget."""
        self.remaining_dollars -= cost
        self.total_spent += cost
        self.trades_placed += 1


@dataclass
class TradeSizing:
    """Position sizing result for a single trade within a city."""

    ticker: str
    side: str  # "yes" or "no"
    contracts: int
    price_cents: int
    cost_dollars: float  # contracts * price / 100
    half_kelly: float
    edge: float
    score: float


class CityBudgetAllocator:
    """
    Allocates trading budget by city based on portfolio and cash constraints.

    Usage:
        allocator = CityBudgetAllocator()

        # At start of each scan cycle:
        allocations = allocator.allocate_for_cycle(
            total_portfolio=112.0,
            available_cash=50.0,
            cities=["NYC", "MIAMI", "CHICAGO"],
        )

        # For each city, execute trades within budget:
        for city, alloc in allocations.items():
            for trade in ranked_trades[city]:
                if alloc.can_afford(trade.cost):
                    execute(trade)
                    alloc.spend(trade.cost)
    """

    def __init__(
        self,
        portfolio_pct_per_city: Optional[float] = None,
        cash_pct_per_city: Optional[float] = None,
        max_cities_per_cycle: Optional[int] = None,
        min_city_budget: Optional[float] = None,
        max_city_pct: Optional[float] = None,
    ):
        self.portfolio_pct_per_city = portfolio_pct_per_city or float(
            os.environ.get("WEATHER_PORTFOLIO_PCT_PER_CITY", "0.10")
        )
        self.cash_pct_per_city = cash_pct_per_city or float(
            os.environ.get("WEATHER_CASH_PCT_PER_CITY", "0.10")
        )
        self.max_cities_per_cycle = max_cities_per_cycle or int(
            os.environ.get("WEATHER_MAX_CITIES_PER_CYCLE", "8")
        )
        # Minimum budget to bother trading a city (1 cent = cost of cheapest contract)
        self.min_city_budget = min_city_budget if min_city_budget is not None else float(
            os.environ.get("WEATHER_MIN_CITY_BUDGET", "0.01")
        )
        # Max percentage of total budget any single city can receive
        self.max_city_pct = max_city_pct or float(
            os.environ.get("WEATHER_MAX_CITY_PCT", "0.20")
        )

        logger.info(
            f"CityBudgetAllocator initialized: "
            f"portfolio_pct={self.portfolio_pct_per_city:.0%}, "
            f"cash_pct={self.cash_pct_per_city:.0%}, "
            f"max_cities={self.max_cities_per_cycle}, "
            f"min_budget=${self.min_city_budget:.2f}, "
            f"max_city_pct={self.max_city_pct:.0%}"
        )

    def allocate_for_cycle(
        self,
        total_portfolio: float,
        available_cash: float,
        cities: List[str],
        existing_city_exposure: Optional[Dict[str, float]] = None,
    ) -> Dict[str, CityAllocation]:
        """
        Allocate budget to each city for this scan cycle.

        Args:
            total_portfolio: Total portfolio value (cash + positions) in dollars
            available_cash: Currently available cash in dollars
            cities: List of city names with active opportunities
            existing_city_exposure: Optional dict of {city: dollars_already_deployed}
                                   Used to reduce allocation for cities we're already heavy in.

        Returns:
            Dict mapping city name to CityAllocation
        """
        if not cities:
            return {}

        if available_cash <= 0:
            logger.warning(
                f"[CITY-BUDGET] No cash available (${available_cash:.2f}), "
                f"cannot allocate to any city"
            )
            return {}

        # Calculate per-city allocations
        portfolio_alloc = total_portfolio * self.portfolio_pct_per_city
        cash_alloc = available_cash * self.cash_pct_per_city

        # The cash constraint always wins
        base_city_budget = min(portfolio_alloc, cash_alloc)

        # Cap total allocation to available cash (can't allocate more than we have)
        # If 8 cities each get $5, that's $40 — but if we only have $30 cash,
        # we need to scale down or limit cities
        num_cities = min(len(cities), self.max_cities_per_cycle)
        total_needed = base_city_budget * num_cities

        if total_needed > available_cash:
            # Scale down per-city budget so total fits in cash
            # Or just limit number of cities
            if base_city_budget * len(cities) > available_cash:
                # More cities than cash can support at minimum budget
                # Allocate to as many cities as possible at base budget
                max_affordable_cities = max(1, int(available_cash / base_city_budget))
                num_cities = min(num_cities, max_affordable_cities)
                logger.info(
                    f"[CITY-BUDGET] Cash constraint: can only afford "
                    f"{num_cities} cities at ${base_city_budget:.2f}/city "
                    f"(have ${available_cash:.2f})"
                )

        allocations: Dict[str, CityAllocation] = {}
        total_allocated = 0.0

        # Sort cities to prioritize (could be enhanced with edge-based priority)
        selected_cities = cities[:num_cities]

        for city in selected_cities:
            # Check if we still have cash to allocate
            remaining_cash = available_cash - total_allocated
            if remaining_cash < self.min_city_budget:
                logger.debug(
                    f"[CITY-BUDGET] Stopping allocation: "
                    f"${remaining_cash:.2f} remaining < min ${self.min_city_budget:.2f}"
                )
                break

            # Adjust for existing exposure in this city
            city_exposure = 0.0
            if existing_city_exposure:
                city_exposure = existing_city_exposure.get(city, 0.0)

            # Reduce allocation by existing exposure (don't over-concentrate)
            adjusted_budget = max(0, base_city_budget - city_exposure)

            # Also cap at remaining cash
            adjusted_budget = min(adjusted_budget, remaining_cash)

            if adjusted_budget < self.min_city_budget:
                logger.debug(
                    f"[CITY-BUDGET] Skipping {city}: "
                    f"adjusted budget ${adjusted_budget:.2f} < min ${self.min_city_budget:.2f} "
                    f"(exposure=${city_exposure:.2f})"
                )
                continue

            allocations[city] = CityAllocation(
                city=city,
                budget_dollars=adjusted_budget,
                portfolio_alloc=portfolio_alloc,
                cash_alloc=cash_alloc,
            )
            total_allocated += adjusted_budget

        logger.info(
            f"[CITY-BUDGET] Allocated ${total_allocated:.2f} across "
            f"{len(allocations)} cities "
            f"(portfolio=${total_portfolio:.2f}, cash=${available_cash:.2f}, "
            f"base=${base_city_budget:.2f}/city) | "
            + ", ".join(
                f"{c}=${a.budget_dollars:.2f}" for c, a in allocations.items()
            )
        )

        return allocations

    def allocate_by_edge(
        self,
        city_edges: Dict[str, float],
        total_portfolio: float,
        available_cash: float,
        existing_city_exposure: Optional[Dict[str, float]] = None,
    ) -> Dict[str, CityAllocation]:
        """
        Allocate budget proportional to each city's best available edge.

        Cities with higher edge get proportionally more capital.
        Still respects per-city caps and cash constraints.

        Args:
            city_edges: Dict of {city: best_edge} — best edge among opportunities
            total_portfolio: Total portfolio value (cash + positions) in dollars
            available_cash: Currently available cash in dollars
            existing_city_exposure: Optional dict of {city: dollars_already_deployed}

        Returns:
            Dict mapping city name to CityAllocation
        """
        if not city_edges or available_cash <= 0:
            if available_cash <= 0:
                logger.warning(
                    f"[CITY-BUDGET] No cash available (${available_cash:.2f})"
                )
            return {}

        # Filter cities with positive edge
        positive = {c: e for c, e in city_edges.items() if e > 0}
        if not positive:
            return {}

        # Limit to max cities (pick highest edge cities first)
        sorted_cities = sorted(positive.items(), key=lambda x: x[1], reverse=True)
        sorted_cities = sorted_cities[: self.max_cities_per_cycle]

        # Total deployable budget: min of portfolio% and cash%
        portfolio_budget = total_portfolio * self.portfolio_pct_per_city * len(sorted_cities)
        cash_budget = available_cash * self.cash_pct_per_city * len(sorted_cities)
        total_budget = min(portfolio_budget, cash_budget, available_cash)

        # Normalize edge scores to proportional weights
        total_edge = sum(e for _, e in sorted_cities)
        if total_edge <= 0:
            return {}

        allocations: Dict[str, CityAllocation] = {}
        total_allocated = 0.0
        max_per_city = total_budget * self.max_city_pct

        for city, edge in sorted_cities:
            remaining_cash = available_cash - total_allocated
            if remaining_cash < self.min_city_budget:
                break

            weight = edge / total_edge
            raw_budget = total_budget * weight

            # Cap per city (don't put >20% in one city)
            capped = min(raw_budget, max_per_city, remaining_cash)

            # Adjust for existing exposure
            city_exposure = 0.0
            if existing_city_exposure:
                city_exposure = existing_city_exposure.get(city, 0.0)
            adjusted = max(0.0, capped - city_exposure)

            if adjusted < self.min_city_budget:
                continue

            portfolio_alloc = total_portfolio * self.portfolio_pct_per_city
            cash_alloc = available_cash * self.cash_pct_per_city

            allocations[city] = CityAllocation(
                city=city,
                budget_dollars=adjusted,
                portfolio_alloc=portfolio_alloc,
                cash_alloc=cash_alloc,
            )
            total_allocated += adjusted

        # Log the allocation weights
        weight_str = ", ".join(
            f"{c}=${a.budget_dollars:.2f}({city_edges.get(c, 0):.0%}edge)"
            for c, a in allocations.items()
        )
        logger.info(
            f"[CITY-BUDGET] Edge-weighted allocation: ${total_allocated:.2f} across "
            f"{len(allocations)} cities "
            f"(portfolio=${total_portfolio:.2f}, cash=${available_cash:.2f}) | "
            f"{weight_str}"
        )

        return allocations

    def get_cycle_summary(
        self, allocations: Dict[str, CityAllocation]
    ) -> Dict[str, Any]:
        """Get summary of budget usage after a cycle completes."""
        total_budget = sum(a.budget_dollars for a in allocations.values())
        total_spent = sum(a.total_spent for a in allocations.values())
        total_trades = sum(a.trades_placed for a in allocations.values())

        city_details = {}
        for city, alloc in allocations.items():
            city_details[city] = {
                "budget": alloc.budget_dollars,
                "spent": alloc.total_spent,
                "remaining": alloc.remaining_dollars,
                "trades": alloc.trades_placed,
                "utilization": (
                    alloc.total_spent / alloc.budget_dollars * 100
                    if alloc.budget_dollars > 0
                    else 0
                ),
            }

        return {
            "total_budget": total_budget,
            "total_spent": total_spent,
            "total_remaining": total_budget - total_spent,
            "total_trades": total_trades,
            "cities": city_details,
            "utilization_pct": (
                total_spent / total_budget * 100 if total_budget > 0 else 0
            ),
        }
