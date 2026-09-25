"""
Unit tests for market data collector.
"""
import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.api.models import Market
from src.data.collector import MarketDataCollector
from src.data.models import MarketDB, PriceDB


@pytest.fixture
def sample_markets():
    """Sample market data for testing."""
    return [
        Market(
            ticker="BTC-25JAN26-100K",
            title="Will Bitcoin hit $100K by Jan 25, 2026?",
            category="crypto",
            status="active",
            yes_bid=45,
            yes_ask=47,
            no_bid=53,
            no_ask=55,
            volume=1000,
            open_interest=500,
            close_time=datetime(2026, 1, 25, 0, 0, 0),
            strike_date=None,
            result=None,
        ),
        Market(
            ticker="SPX-31DEC26-5000",
            title="Will S&P 500 reach 5000 by end of 2026?",
            category="stocks",
            status="active",
            yes_bid=60,
            yes_ask=62,
            no_bid=38,
            no_ask=40,
            volume=2000,
            open_interest=1000,
            close_time=datetime(2026, 12, 31, 0, 0, 0),
            strike_date=None,
            result=None,
        ),
    ]


@pytest.fixture
def collector():
    """Create a collector instance for testing."""
    return MarketDataCollector(
        markets_interval=300,
        prices_interval=60,
        track_all_active=True,
    )


@pytest.mark.asyncio
async def test_collector_initialization():
    """Test collector initialization."""
    collector = MarketDataCollector(
        markets_interval=300,
        prices_interval=60,
        track_all_active=False,
        tracked_tickers={"BTC-25JAN26-100K", "SPX-31DEC26-5000"},
    )

    assert collector.markets_interval == 300
    assert collector.prices_interval == 60
    assert not collector.track_all_active
    assert len(collector.tracked_tickers) == 2
    assert "BTC-25JAN26-100K" in collector.tracked_tickers
    assert not collector.running


@pytest.mark.asyncio
async def test_add_remove_tracked_ticker(collector):
    """Test adding and removing tracked tickers."""
    collector.add_tracked_ticker("TEST-TICKER-1")
    assert "TEST-TICKER-1" in collector.tracked_tickers

    collector.add_tracked_ticker("TEST-TICKER-2")
    assert len(collector.tracked_tickers) == 2

    collector.remove_tracked_ticker("TEST-TICKER-1")
    assert "TEST-TICKER-1" not in collector.tracked_tickers
    assert len(collector.tracked_tickers) == 1


@pytest.mark.asyncio
async def test_fetch_markets_with_retry(collector, sample_markets):
    """Test market fetching with retry logic."""
    mock_client = AsyncMock()
    # get_markets returns a tuple of (List[Market], Optional[str] cursor)
    mock_client.get_markets = AsyncMock(return_value=(sample_markets, None))

    result = await collector._fetch_markets_with_retry(mock_client)

    assert len(result) == 2
    assert result[0].ticker == "BTC-25JAN26-100K"
    assert result[1].ticker == "SPX-31DEC26-5000"
    mock_client.get_markets.assert_called_once_with(status="open", limit=1000)


@pytest.mark.asyncio
async def test_collect_markets(collector, sample_markets):
    """Test market collection and storage."""
    mock_client = AsyncMock()
    # get_markets returns a tuple of (List[Market], Optional[str] cursor)
    mock_client.get_markets = AsyncMock(return_value=(sample_markets, None))

    with patch("src.data.collector.get_db_session") as mock_session_gen:
        mock_db_session = MagicMock()
        # get_db_session returns a generator, so we need to mock next() to return the session
        mock_session_gen.return_value.__next__ = MagicMock(return_value=mock_db_session)
        mock_db_session.query.return_value.filter_by.return_value.first.return_value = (
            None
        )
        mock_db_session.__enter__ = MagicMock(return_value=mock_db_session)
        mock_db_session.__exit__ = MagicMock(return_value=False)

        await collector._collect_markets(mock_client)

        # Verify database operations
        mock_client.get_markets.assert_called_once()
        assert mock_db_session.add.call_count == 2
        mock_db_session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_collect_markets_update_existing(collector, sample_markets):
    """Test updating existing markets in database."""
    mock_client = AsyncMock()
    # get_markets returns a tuple of (List[Market], Optional[str] cursor)
    mock_client.get_markets = AsyncMock(return_value=(sample_markets, None))

    # Mock existing market in database
    existing_market = MagicMock(spec=MarketDB)
    existing_market.ticker = "BTC-25JAN26-100K"
    existing_market.category = "crypto"
    existing_market.result = None

    with patch("src.data.collector.get_db_session") as mock_session_gen:
        mock_db_session = MagicMock()
        mock_session_gen.return_value.__next__ = MagicMock(return_value=mock_db_session)
        mock_db_session.__enter__ = MagicMock(return_value=mock_db_session)
        mock_db_session.__exit__ = MagicMock(return_value=False)

        # First call returns existing market, second returns None (new market)
        mock_db_session.query.return_value.filter_by.return_value.first.side_effect = [
            existing_market,
            None,
        ]

        await collector._collect_markets(mock_client)

        # Should update existing market and add new market
        assert mock_db_session.add.call_count == 1
        mock_db_session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_fetch_market_with_retry(collector, sample_markets):
    """Test single market fetching with retry."""
    mock_client = AsyncMock()
    mock_client.get_market = AsyncMock(return_value=sample_markets[0])

    result = await collector._fetch_market_with_retry(mock_client, "BTC-25JAN26-100K")

    assert result.ticker == "BTC-25JAN26-100K"
    assert result.yes_bid == 45
    mock_client.get_market.assert_called_once_with("BTC-25JAN26-100K", use_cache=False)


@pytest.mark.asyncio
async def test_collect_prices_with_tracked_tickers(sample_markets):
    """Test price collection for specific tracked tickers."""
    collector = MarketDataCollector(
        markets_interval=300,
        prices_interval=60,
        track_all_active=False,
        tracked_tickers={"BTC-25JAN26-100K"},
    )

    mock_client = AsyncMock()
    mock_client.get_market = AsyncMock(return_value=sample_markets[0])

    with patch("src.data.collector.get_db_session") as mock_session_gen:
        mock_db_session = MagicMock()
        mock_session_gen.return_value.__next__ = MagicMock(return_value=mock_db_session)
        mock_db_session.__enter__ = MagicMock(return_value=mock_db_session)
        mock_db_session.__exit__ = MagicMock(return_value=False)

        await collector._collect_prices(mock_client)

        # Should fetch price for tracked ticker
        mock_client.get_market.assert_called_once()
        mock_db_session.add.assert_called_once()
        mock_db_session.commit.assert_called_once()


@pytest.mark.asyncio
async def test_collect_prices_with_all_active(sample_markets):
    """Test price collection for all active markets."""
    collector = MarketDataCollector(
        markets_interval=300,
        prices_interval=60,
        track_all_active=True,
    )

    mock_client = AsyncMock()
    mock_client.get_market = AsyncMock(side_effect=sample_markets)

    # Mock database sessions — each get_db_session() call returns a new generator
    mock_db_session = MagicMock()
    mock_db_session.__enter__ = MagicMock(return_value=mock_db_session)
    mock_db_session.__exit__ = MagicMock(return_value=False)

    # Active markets returned by query chain (.query().filter().all())
    mock_market_1 = MagicMock(spec=MarketDB)
    mock_market_1.ticker = "BTC-25JAN26-100K"
    mock_market_2 = MagicMock(spec=MarketDB)
    mock_market_2.ticker = "SPX-31DEC26-5000"

    mock_db_session.query.return_value.filter.return_value.all.return_value = [
        mock_market_1,
        mock_market_2,
    ]

    def make_gen():
        yield mock_db_session

    with patch("src.data.collector.get_db_session", side_effect=make_gen):
        await collector._collect_prices(mock_client)

        # Should fetch prices for both active markets
        assert mock_client.get_market.call_count == 2


@pytest.mark.asyncio
async def test_start_stop_collector(collector):
    """Test starting and stopping the collector."""
    # Mock the collection loops to exit immediately
    async def mock_loop():
        await asyncio.sleep(0.1)

    with patch.object(
        collector, "_markets_collection_loop", side_effect=mock_loop
    ), patch.object(collector, "_prices_collection_loop", side_effect=mock_loop):

        await collector.start()
        assert collector.running
        assert collector.markets_task is not None
        assert collector.prices_task is not None

        await asyncio.sleep(0.2)

        await collector.stop()
        assert not collector.running


@pytest.mark.asyncio
async def test_get_collection_stats(collector):
    """Test getting collection statistics."""
    with patch("src.data.collector.get_db_session") as mock_session_gen:
        mock_db_session = MagicMock()
        mock_session_gen.return_value.__next__ = MagicMock(return_value=mock_db_session)
        mock_db_session.__enter__ = MagicMock(return_value=mock_db_session)
        mock_db_session.__exit__ = MagicMock(return_value=False)

        # Mock database queries
        # First query: session.query(MarketDB).count()
        mock_query_1 = MagicMock()
        mock_query_1.count.return_value = 10

        # Second query: session.query(MarketDB).filter_by(status="active").count()
        mock_query_2 = MagicMock()
        mock_query_2_filtered = MagicMock()
        mock_query_2_filtered.count.return_value = 8
        mock_query_2.filter_by.return_value = mock_query_2_filtered

        # Third query: session.query(PriceDB).count()
        mock_query_3 = MagicMock()
        mock_query_3.count.return_value = 150

        # Fourth query: session.query(PriceDB).order_by(...).first()
        mock_query_4 = MagicMock()
        mock_latest_price = MagicMock(spec=PriceDB)
        mock_latest_price.timestamp = datetime(2026, 1, 26, 12, 0, 0)
        mock_query_4.order_by.return_value.first.return_value = mock_latest_price

        # Set up side_effect to return different query objects
        mock_db_session.query.side_effect = [mock_query_1, mock_query_2, mock_query_3, mock_query_4]

        stats = collector.get_collection_stats()

        assert stats["running"] is False
        assert stats["total_markets"] == 10
        assert stats["active_markets"] == 8
        assert stats["total_price_snapshots"] == 150
        assert stats["latest_price_snapshot"] == "2026-01-26T12:00:00"
        assert stats["markets_interval"] == 300
        assert stats["prices_interval"] == 60


@pytest.mark.asyncio
async def test_collect_markets_handles_empty_response(collector):
    """Test that collector handles empty market list gracefully."""
    mock_client = AsyncMock()
    # get_markets returns a tuple of (List[Market], Optional[str] cursor)
    mock_client.get_markets = AsyncMock(return_value=([], None))

    with patch("src.data.collector.get_db_session") as mock_session_gen:
        mock_db_session = MagicMock()
        mock_session_gen.return_value.__next__ = MagicMock(return_value=mock_db_session)
        mock_db_session.__enter__ = MagicMock(return_value=mock_db_session)
        mock_db_session.__exit__ = MagicMock(return_value=False)

        await collector._collect_markets(mock_client)

        # Should not attempt to store anything
        mock_db_session.add.assert_not_called()


@pytest.mark.asyncio
async def test_collect_prices_empty_tracked_list():
    """Test price collection with no tracked tickers."""
    collector = MarketDataCollector(
        markets_interval=300,
        prices_interval=60,
        track_all_active=False,
        tracked_tickers=set(),
    )

    mock_client = AsyncMock()

    await collector._collect_prices(mock_client)

    # Should not make any API calls
    mock_client.get_market.assert_not_called()
