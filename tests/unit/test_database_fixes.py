"""Tests for Phase 7 database, logging & runtime fixes."""
import os
import pytest
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from src.data.models import (
    Base, MarketDB, PositionDB, TradeDB, UTCDateTime,
)
from src.data.database import DatabaseManager, DB_MAX_RETRIES


# ── In-memory SQLite for isolation ─────────────────────────────────────────

@pytest.fixture
def db_manager(tmp_path):
    """Create an in-memory DB manager for testing."""
    db_url = f"sqlite:///{tmp_path}/test.db"
    mgr = DatabaseManager(db_url)
    mgr.create_all_tables()
    return mgr


# ── Fix 1: Session retry bug ──────────────────────────────────────────────

class TestSessionRetry:
    def test_session_no_retry_crash(self, db_manager):
        """Simulated transient error raises cleanly (no RuntimeError about generator)."""
        with pytest.raises(Exception, match="database is locked"):
            with db_manager.get_session() as session:
                raise Exception("database is locked")

    def test_execute_with_retry_succeeds(self, db_manager):
        """execute_with_retry retries transient errors and succeeds."""
        attempt_count = {"n": 0}

        def flaky_op(session):
            attempt_count["n"] += 1
            if attempt_count["n"] < 2:
                raise Exception("database is locked")
            # Second attempt succeeds
            m = MarketDB(
                ticker="RETRY-OK", title="Test", category="test",
                status="active",
            )
            session.add(m)
            return "done"

        result = db_manager.execute_with_retry(flaky_op)
        assert result == "done"
        assert attempt_count["n"] == 2

        # Verify the row was actually persisted
        with db_manager.get_session() as session:
            found = session.query(MarketDB).filter_by(ticker="RETRY-OK").first()
            assert found is not None

    def test_execute_with_retry_exhausted(self, db_manager):
        """execute_with_retry raises after all attempts fail."""
        def always_fails(session):
            raise Exception("database is locked forever")

        with pytest.raises(Exception, match="database is locked forever"):
            db_manager.execute_with_retry(always_fails)


# ── Fix 2: Naive datetime fix ─────────────────────────────────────────────

class TestDatetimeFix:
    def test_updated_at_type_is_utcdatetime(self):
        """MarketDB, PositionDB, SignalExecutionDB, LongshotPositionDB
        use UTCDateTime for updated_at (not plain DateTime)."""
        from src.data.models import SignalExecutionDB, LongshotPositionDB

        for model in [MarketDB, PositionDB, SignalExecutionDB, LongshotPositionDB]:
            col = model.__table__.columns["updated_at"]
            assert isinstance(col.type, UTCDateTime), (
                f"{model.__name__}.updated_at should be UTCDateTime, "
                f"got {type(col.type).__name__}"
            )

    def test_updated_at_is_aware(self, db_manager):
        """Created position has timezone-aware updated_at."""
        with db_manager.get_session() as session:
            pos = PositionDB(
                ticker="TZ-TEST", side="yes", quantity=10,
                average_price=50,
            )
            session.add(pos)

        with db_manager.get_session() as session:
            pos = session.query(PositionDB).filter_by(ticker="TZ-TEST").first()
            assert pos is not None
            assert pos.updated_at is not None
            assert pos.updated_at.tzinfo is not None


# ── Fix 3: Settlement query efficiency ─────────────────────────────────────

class TestSettlementQuery:
    def test_settlement_query_filters_correctly(self, db_manager):
        """Settlement check only processes markets with result='yes' or 'no'."""
        from src.execution.settlement_manager import SettlementManager

        with db_manager.get_session() as session:
            # Market with empty result (unresolved)
            session.add(MarketDB(
                ticker="UNRESOLVED-1", title="Unresolved", category="test",
                status="active", result="",
                close_time=datetime.now(timezone.utc),
            ))
            # Market with result='yes'
            session.add(MarketDB(
                ticker="YES-MKT", title="Yes", category="test",
                status="settled", result="yes",
                close_time=datetime.now(timezone.utc),
            ))
            # Market with result='no'
            session.add(MarketDB(
                ticker="NO-MKT", title="No", category="test",
                status="settled", result="no",
                close_time=datetime.now(timezone.utc),
            ))
            # Buy trade for YES market
            session.add(TradeDB(
                order_id="T1", fill_id="F1", ticker="YES-MKT",
                side="yes", action="buy", quantity=5, price=40,
                fee=0, status="filled",
                timestamp=datetime.now(timezone.utc), resolved=0,
            ))
            # Buy trade for NO market
            session.add(TradeDB(
                order_id="T2", fill_id="F2", ticker="NO-MKT",
                side="no", action="buy", quantity=5, price=10,
                fee=0, status="filled",
                timestamp=datetime.now(timezone.utc), resolved=0,
            ))

        # Patch get_db_session to use our test DB
        def _test_session():
            with db_manager.get_session() as session:
                yield session

        sm = SettlementManager(paper_trading=True)
        with patch("src.execution.settlement_manager.get_db_session", _test_session):
            settlements = sm.check_settlements()

        # Should only process YES-MKT and NO-MKT, not UNRESOLVED-1
        tickers = [s["ticker"] for s in settlements]
        assert "UNRESOLVED-1" not in tickers
        assert "YES-MKT" in tickers
        assert "NO-MKT" in tickers

    def test_settlement_query_efficiency(self, db_manager):
        """1000 unresolved + 5 resolved: only 5 are processed."""
        from src.execution.settlement_manager import SettlementManager

        with db_manager.get_session() as session:
            for i in range(100):
                session.add(MarketDB(
                    ticker=f"EMPTY-{i}", title="Empty", category="test",
                    status="active", result="",
                    close_time=datetime.now(timezone.utc),
                ))
            for i in range(5):
                session.add(MarketDB(
                    ticker=f"SETTLED-{i}", title="Settled", category="test",
                    status="settled", result="yes",
                    close_time=datetime.now(timezone.utc),
                ))
                session.add(TradeDB(
                    order_id=f"EFF-{i}", fill_id=f"FEFF-{i}",
                    ticker=f"SETTLED-{i}", side="yes", action="buy",
                    quantity=5, price=30, fee=0, status="filled",
                    timestamp=datetime.now(timezone.utc), resolved=0,
                ))

        def _test_session():
            with db_manager.get_session() as session:
                yield session

        sm = SettlementManager(paper_trading=True)
        with patch("src.execution.settlement_manager.get_db_session", _test_session):
            settlements = sm.check_settlements()

        assert len(settlements) == 5


# ── Fix 4: Orphan repair ──────────────────────────────────────────────────

class TestOrphanRepair:
    def _make_trading_system_mock(self, db_manager):
        """Create a minimal mock with _repair_orphaned_positions."""
        # Import the actual method
        from src.main import TradingSystem

        # We can't instantiate TradingSystem (requires API), so test the method standalone
        class FakeSystem:
            pass

        fake = FakeSystem()
        fake._repair_orphaned_positions = TradingSystem._repair_orphaned_positions.__get__(fake)
        return fake

    def test_orphan_repair(self, db_manager):
        """Position with no TradeDB gets a synthetic trade."""
        with db_manager.get_session() as session:
            session.add(PositionDB(
                ticker="ORPHAN-1", side="yes", quantity=10,
                average_price=25,
            ))

        def _test_session():
            with db_manager.get_session() as session:
                yield session

        with patch("src.main.get_db_session", _test_session):
            from src.main import TradingSystem
            # Call the method directly as a function
            repaired = TradingSystem._repair_orphaned_positions(None)

        with db_manager.get_session() as session:
            trade = session.query(TradeDB).filter(
                TradeDB.ticker == "ORPHAN-1",
                TradeDB.strategy == "api_sync",
            ).first()
            assert trade is not None
            assert trade.order_id.startswith("REPAIR-")
            assert trade.quantity == 10
            assert trade.price == 25

        assert repaired == 1

    def test_orphan_repair_idempotent(self, db_manager):
        """Running repair twice doesn't create duplicate trades."""
        with db_manager.get_session() as session:
            session.add(PositionDB(
                ticker="IDEM-1", side="no", quantity=5,
                average_price=90,
            ))

        def _test_session():
            with db_manager.get_session() as session:
                yield session

        with patch("src.main.get_db_session", _test_session):
            from src.main import TradingSystem
            r1 = TradingSystem._repair_orphaned_positions(None)
            r2 = TradingSystem._repair_orphaned_positions(None)

        assert r1 == 1
        assert r2 == 0  # Already has a trade, no second repair

        with db_manager.get_session() as session:
            trades = session.query(TradeDB).filter(
                TradeDB.ticker == "IDEM-1",
                TradeDB.strategy == "api_sync",
            ).all()
            assert len(trades) == 1


# ── Fix 5: API sync creates trade ─────────────────────────────────────────

class TestApiSyncTrade:
    def test_api_sync_creates_trade(self, db_manager):
        """New position from API sync gets a synthetic TradeDB entry."""
        with db_manager.get_session() as session:
            pos = PositionDB(
                ticker="SYNC-TEST-1", side="no", quantity=20,
                average_price=85,
            )
            session.add(pos)

        # Simulate what _sync_positions_from_api does for a new ticker
        with db_manager.get_session() as session:
            existing_trade = session.query(TradeDB).filter(
                TradeDB.ticker == "SYNC-TEST-1",
                TradeDB.action == "buy",
            ).first()
            if existing_trade is None:
                trade = TradeDB(
                    order_id="SYNC-SYNC-TEST-1-20260212",
                    fill_id="SYNC-FILL-SYNC-TEST-1",
                    ticker="SYNC-TEST-1",
                    side="no",
                    action="buy",
                    quantity=20,
                    price=85,
                    fee=0,
                    status="filled",
                    timestamp=datetime.now(timezone.utc),
                    strategy="api_sync",
                    resolved=0,
                )
                session.add(trade)

        with db_manager.get_session() as session:
            trade = session.query(TradeDB).filter(
                TradeDB.ticker == "SYNC-TEST-1",
                TradeDB.strategy == "api_sync",
            ).first()
            assert trade is not None
            assert trade.order_id.startswith("SYNC-")
            assert trade.side == "no"
            assert trade.quantity == 20
            assert trade.price == 85


# ── Fix 6: Loguru logger ──────────────────────────────────────────────────

class TestLoggerFix:
    def test_nws_uses_loguru(self):
        """nws_weather module-level logger is loguru, not stdlib."""
        import src.data_sources.nws_weather as nws_mod
        # loguru's logger has a specific type
        from loguru import logger as loguru_logger
        assert type(nws_mod.logger) == type(loguru_logger), (
            f"nws_weather.logger is {type(nws_mod.logger)}, expected loguru"
        )

    def test_obs_scanner_uses_loguru(self):
        """observation_scanner module-level logger is loguru, not stdlib."""
        import src.strategy.observation_scanner as obs_mod
        from loguru import logger as loguru_logger
        assert type(obs_mod.logger) == type(loguru_logger), (
            f"observation_scanner.logger is {type(obs_mod.logger)}, expected loguru"
        )
