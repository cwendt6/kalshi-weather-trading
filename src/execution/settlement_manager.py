"""
Settlement detection and position cleanup.

Polls for resolved markets and reconciles our DB:
1. Marks TradeDB entries as resolved with correct PnL
2. Removes PositionDB rows for settled markets
3. Replenishes obs-settled pool (Phase 4 integration)
4. Fires alerts for settlements (wins and losses)
5. Updates ForecastDB resolution fields for calibration

Runs every 5 minutes in the main loop.
"""
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from src.data.database import get_db_session
from src.data.models import MarketDB, PositionDB, TradeDB
from src.utils.fees import KALSHI_WINNER_FEE_RATE
from src.utils.logging import logger


class SettlementManager:
    """Detects settled markets and reconciles positions/PnL."""

    SETTLEMENT_CHECK_INTERVAL = int(os.getenv("SETTLEMENT_CHECK_INTERVAL", "300"))  # 5 min

    def __init__(self, risk_manager=None, alert_manager=None, paper_trading: bool = True):
        self.risk_manager = risk_manager
        self.alert_manager = alert_manager
        self.paper_trading = paper_trading
        self._last_check: Optional[datetime] = None
        self._processed_tickers: set = set()  # Avoid re-processing

    def check_settlements(self) -> List[dict]:
        """Main entry: check for newly resolved markets.

        Finds markets where MarketDB.result IS NOT NULL and resolves
        matching TradeDB entries. Idempotent — won't double-count.

        Returns list of settlement summaries.
        """
        self._last_check = datetime.now(timezone.utc)
        settlements = []

        try:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=48)

            with next(get_db_session()) as session:
                # Find resolved markets not yet processed
                resolved_markets = session.query(MarketDB).filter(
                    MarketDB.result.in_(["yes", "no"]),
                    MarketDB.close_time >= cutoff,
                ).all()

                if not resolved_markets:
                    return settlements

                for market in resolved_markets:
                    ticker = str(market.ticker)
                    if ticker in self._processed_tickers:
                        continue

                    market_result = str(market.result).lower()
                    if market_result not in ("yes", "no"):
                        continue

                    # Find unresolved trades for this ticker
                    trades = session.query(TradeDB).filter(
                        TradeDB.ticker == ticker,
                        TradeDB.resolved == 0,
                        TradeDB.action == "buy",
                    ).all()

                    if not trades:
                        self._processed_tickers.add(ticker)
                        continue

                    for trade in trades:
                        side = str(trade.side or "yes")
                        quantity = int(trade.quantity or 0)
                        entry_price = int(trade.price or 0)
                        strategy = str(trade.strategy or "unknown")

                        if quantity <= 0 or entry_price <= 0:
                            continue

                        pnl_dollars, pnl_pct, outcome = self._calculate_settlement_pnl(
                            side, quantity, entry_price, market_result,
                        )

                        # Update TradeDB
                        trade.resolved = 1
                        trade.pnl = round(pnl_dollars, 2)
                        trade.pnl_percent = round(pnl_pct, 2)
                        trade.outcome = outcome
                        trade.market_result = market_result
                        trade.resolved_at = datetime.now(timezone.utc)

                        settlement = {
                            "ticker": ticker,
                            "side": side,
                            "quantity": quantity,
                            "entry_price": entry_price,
                            "result": market_result,
                            "pnl": pnl_dollars,
                            "strategy": strategy,
                            "outcome": outcome,
                        }
                        settlements.append(settlement)

                        # Phase 4 integration: replenish obs pool
                        if self.risk_manager and "obs_settled" in strategy:
                            deployed = quantity * entry_price / 100.0
                            profit = max(0.0, pnl_dollars)
                            self.risk_manager.record_obs_settlement(deployed, profit)

                        # Fire alert
                        if self.alert_manager and hasattr(self.alert_manager, "alert_settlement"):
                            try:
                                self.alert_manager.alert_settlement(
                                    ticker=ticker, side=side, result=market_result,
                                    pnl=pnl_dollars, strategy=strategy, quantity=quantity,
                                )
                            except Exception:
                                pass  # Never crash on alert failure

                        mode = "[PAPER]" if self.paper_trading else "[LIVE]"
                        emoji = "✅" if pnl_dollars > 0 else "❌"
                        logger.info(
                            f"{mode} {emoji} SETTLED: {ticker} | {side.upper()} x{quantity} "
                            f"@ {entry_price}¢ | Result: {market_result.upper()} | "
                            f"P&L: ${pnl_dollars:+.2f} ({pnl_pct:+.1f}%) | {strategy}"
                        )

                    # Cleanup position
                    self._cleanup_position_in_session(session, ticker)

                    # Update forecast resolution
                    self._update_forecast_resolution_in_session(session, ticker, market_result)

                    self._processed_tickers.add(ticker)

                session.commit()

        except Exception as e:
            logger.error(f"Settlement check failed: {e}")

        if settlements:
            total_pnl = sum(s["pnl"] for s in settlements)
            wins = sum(1 for s in settlements if s["outcome"] == "win")
            losses = len(settlements) - wins
            logger.info(
                f"📋 Settlement batch: {len(settlements)} positions | "
                f"P&L: ${total_pnl:+.2f} | Wins: {wins} Losses: {losses}"
            )

        return settlements

    def _calculate_settlement_pnl(
        self, side: str, quantity: int, entry_price: int, market_result: str,
    ) -> Tuple[float, float, str]:
        """Calculate PnL for a settled position.

        Returns (pnl_dollars, pnl_percent, outcome).
        Uses 2% winner fee on WINNINGS (not payout).
        """
        cost_cents = quantity * entry_price
        cost_dollars = cost_cents / 100.0

        # Did we win?
        won = (side == "yes" and market_result == "yes") or \
              (side == "no" and market_result == "no")

        if won:
            payout_cents = quantity * 100  # $1 per contract
            gross_pnl_cents = payout_cents - cost_cents
            gross_pnl_dollars = gross_pnl_cents / 100.0
            fee = gross_pnl_dollars * KALSHI_WINNER_FEE_RATE
            net_pnl = gross_pnl_dollars - fee
            pnl_pct = (net_pnl / cost_dollars * 100) if cost_dollars > 0 else 0.0
            return net_pnl, pnl_pct, "win"
        else:
            # Total loss — we lose what we paid
            net_pnl = -cost_dollars
            pnl_pct = -100.0
            return net_pnl, pnl_pct, "loss"

    def _cleanup_position_in_session(self, session, ticker: str) -> bool:
        """Remove PositionDB row for a settled market (within existing session)."""
        position = session.query(PositionDB).filter_by(ticker=ticker).first()
        if position:
            session.delete(position)
            logger.debug(f"Cleaned up position for settled market: {ticker}")
            return True
        return False

    def _cleanup_position(self, ticker: str) -> bool:
        """Remove PositionDB row for a settled market."""
        try:
            with next(get_db_session()) as session:
                return self._cleanup_position_in_session(session, ticker)
        except Exception as e:
            logger.debug(f"Position cleanup failed for {ticker}: {e}")
            return False

    def _update_forecast_resolution_in_session(
        self, session, ticker: str, market_result: str,
    ) -> None:
        """Update ForecastDB entries for this ticker with resolution."""
        try:
            from src.data.models import ForecastDB
            forecasts = session.query(ForecastDB).filter(
                ForecastDB.ticker == ticker,
            ).all()

            now = datetime.now(timezone.utc)
            for fc in forecasts:
                # Resolution: 1 if forecast side matches result
                fc_side = str(getattr(fc, "side", "yes") or "yes")
                if fc_side == market_result:
                    fc.resolution = 1
                else:
                    fc.resolution = 0
                fc.resolved_at = now

                # Brier score: (probability - resolution)^2
                prob = float(getattr(fc, "probability", 0.5) or 0.5)
                fc.brier_score = (prob - fc.resolution) ** 2

        except Exception as e:
            logger.debug(f"Forecast resolution update failed for {ticker}: {e}")

    def _update_forecast_resolution(self, ticker: str, market_result: str) -> None:
        """Update ForecastDB entries for this ticker with resolution."""
        try:
            with next(get_db_session()) as session:
                self._update_forecast_resolution_in_session(session, ticker, market_result)
                session.commit()
        except Exception as e:
            logger.debug(f"Forecast resolution update failed for {ticker}: {e}")

    def get_settlement_summary(self, hours: int = 24) -> dict:
        """Get summary of recent settlements."""
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
            with next(get_db_session()) as session:
                trades = session.query(TradeDB).filter(
                    TradeDB.resolved == 1,
                    TradeDB.resolved_at >= cutoff,
                ).all()

                total_pnl = 0.0
                wins = 0
                losses = 0
                by_strategy: Dict[str, dict] = {}

                for t in trades:
                    pnl = float(t.pnl or 0)
                    total_pnl += pnl
                    outcome = str(t.outcome or "")
                    strategy = str(t.strategy or "unknown")

                    if outcome == "win":
                        wins += 1
                    else:
                        losses += 1

                    if strategy not in by_strategy:
                        by_strategy[strategy] = {"count": 0, "pnl": 0.0}
                    by_strategy[strategy]["count"] += 1
                    by_strategy[strategy]["pnl"] += pnl

                return {
                    "total_settled": len(trades),
                    "total_pnl": total_pnl,
                    "wins": wins,
                    "losses": losses,
                    "by_strategy": by_strategy,
                }
        except Exception as e:
            logger.error(f"Settlement summary failed: {e}")
            return {"total_settled": 0, "total_pnl": 0.0, "wins": 0, "losses": 0, "by_strategy": {}}


# Singleton
_settlement_manager: Optional[SettlementManager] = None


def get_settlement_manager(**kwargs) -> SettlementManager:
    """Get or create the global settlement manager."""
    global _settlement_manager
    if _settlement_manager is None:
        _settlement_manager = SettlementManager(**kwargs)
    return _settlement_manager
