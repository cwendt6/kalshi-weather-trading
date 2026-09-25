"""
Kalshi fee calculation utilities.

Kalshi charges a 2% fee on WINNINGS for winning trades.
Winnings = payout - cost (NOT 2% of the full payout).

Examples (binary $1.00 contracts):
  Buy at 50¢: winnings = 50¢, fee = 1.0¢, net profit = 49.0¢
  Buy at 90¢: winnings = 10¢, fee = 0.2¢, net profit = 9.8¢
  Buy at 95¢: winnings =  5¢, fee = 0.1¢, net profit = 4.9¢
  Buy at 97¢: winnings =  3¢, fee = 0.06¢, net profit = 2.94¢

All strategies must account for this fee in their profitability calculations.
"""

# Kalshi's winner fee rate (2% of winnings on winning side)
KALSHI_WINNER_FEE_RATE = 0.02


def calculate_fee(payout: float = 1.0) -> float:
    """Calculate Kalshi fee as 2% of payout (conservative / legacy estimate).

    NOTE: This overestimates the fee. For accurate fee calculation
    that accounts for buy price, use calculate_fee_on_winnings().

    Args:
        payout: The payout amount (default $1.00 for binary contracts).

    Returns:
        Fee amount in dollars.
    """
    return payout * KALSHI_WINNER_FEE_RATE


def calculate_fee_on_winnings(buy_price: float, payout: float = 1.0) -> float:
    """Calculate Kalshi fee as 2% of actual winnings (payout - cost).

    This is the accurate fee calculation. Kalshi charges 2% of winnings,
    NOT 2% of the full payout.

    Args:
        buy_price: Price paid per contract (0.0-1.0 scale in dollars).
        payout: The payout amount (default $1.00).

    Returns:
        Fee amount in dollars.

    Examples:
        >>> calculate_fee_on_winnings(0.50)  # 50¢ buy
        0.01  # 2% of 50¢ = 1¢
        >>> calculate_fee_on_winnings(0.95)  # 95¢ buy
        0.001  # 2% of 5¢ = 0.1¢
    """
    winnings = payout - buy_price
    if winnings <= 0:
        return 0.0
    return winnings * KALSHI_WINNER_FEE_RATE


def calculate_net_profit(gross_profit: float, payout: float = 1.0) -> float:
    """Calculate profit after Kalshi's 2% winner fee (conservative).

    Uses the legacy fee calculation (2% of payout).

    Args:
        gross_profit: Profit before fees.
        payout: The payout amount (default $1.00).

    Returns:
        Net profit after fee deduction.
    """
    fee = calculate_fee(payout)
    return gross_profit - fee


def calculate_net_profit_accurate(buy_price: float, payout: float = 1.0) -> float:
    """Calculate net profit after accurate Kalshi fee (2% of winnings).

    Args:
        buy_price: Price paid per contract (0.0-1.0 scale).
        payout: The payout amount (default $1.00).

    Returns:
        Net profit after fee deduction.
    """
    winnings = payout - buy_price
    if winnings <= 0:
        return winnings  # Loss (no fee)
    fee = calculate_fee_on_winnings(buy_price, payout)
    return winnings - fee


def calculate_net_profit_pct(buy_price: float, payout: float = 1.0) -> float:
    """Calculate net profit percentage after fees.

    Args:
        buy_price: Price paid per contract (0.0-1.0 scale).
        payout: The payout amount (default $1.00).

    Returns:
        Net profit as a percentage of payout.
    """
    gross_profit = payout - buy_price
    return calculate_net_profit(gross_profit, payout) / payout


def is_profitable_after_fees(buy_price: float, payout: float = 1.0) -> bool:
    """Check if a trade will be profitable after fees.

    Uses the accurate fee calculation (2% of winnings).

    Args:
        buy_price: Price paid per contract (0.0-1.0 scale).
        payout: The payout amount (default $1.00).

    Returns:
        True if net profit > 0 after fees.
    """
    return calculate_net_profit_accurate(buy_price, payout) > 0


def max_profitable_price(payout: float = 1.0) -> float:
    """Return the maximum buy price that's still profitable after fees.

    With 2% fee on winnings, ANY price below the payout is technically
    profitable: at 99¢ buy, winnings = 1¢, fee = 0.02¢, profit = 0.98¢.

    The real constraint is minimum meaningful profit. We use 98¢ as the
    practical max (guarantees ≥1.96¢ profit per contract).

    Returns:
        Maximum buy price in dollars (0.0-1.0 scale).
    """
    return 0.98  # At 98¢: profit = 0.98 × 2¢ = 1.96¢


def fee_per_contract(num_contracts: int, payout: float = 1.0) -> float:
    """Calculate total fees for N winning contracts (conservative estimate).

    Args:
        num_contracts: Number of winning contracts.
        payout: Payout per contract.

    Returns:
        Total fee amount.
    """
    return num_contracts * calculate_fee(payout)


def guaranteed_profit_cents(no_price_cents: int) -> float:
    """Calculate guaranteed profit per contract for a dead-bracket NO trade.

    For observation-settled trades where the outcome is certain:
    - Cost = no_price_cents
    - Payout = 100¢ (binary contract)
    - Winnings = 100 - no_price_cents
    - Fee = 2% × winnings
    - Net profit = 98% × winnings

    Args:
        no_price_cents: Cost to buy one NO contract.

    Returns:
        Net profit in cents.
    """
    winnings = 100 - no_price_cents
    return winnings * (1.0 - KALSHI_WINNER_FEE_RATE)
