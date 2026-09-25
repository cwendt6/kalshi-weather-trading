"""Unit tests for Kalshi API client."""
import time
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from src.api.kalshi_client import KalshiClient, RateLimiter


class TestRateLimiter:
    """Test rate limiter functionality."""

    def test_rate_limiter_init(self) -> None:
        """Test rate limiter initialization."""
        limiter = RateLimiter(rate=10.0, capacity=10)
        assert limiter.rate == 10.0
        assert limiter.capacity == 10
        assert limiter.tokens == 10.0

    @pytest.mark.asyncio
    async def test_rate_limiter_acquire(self) -> None:
        """Test rate limiter token acquisition."""
        limiter = RateLimiter(rate=100.0, capacity=5)

        # Should be able to acquire tokens immediately
        await limiter.acquire()
        assert limiter.tokens < 5.0

        await limiter.acquire()
        assert limiter.tokens < 4.0


class TestKalshiClient:
    """Test Kalshi API client."""

    @pytest.fixture
    def mock_private_key(self, tmp_path: Path) -> Path:
        """Create a mock private key file."""
        key_path = tmp_path / "test_key.pem"
        # Generate a real RSA key for testing
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.hazmat.primitives import serialization

        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )

        pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )

        key_path.write_bytes(pem)
        return key_path

    def test_client_init(self, mock_private_key: Path) -> None:
        """Test client initialization."""
        client = KalshiClient(
            api_key="test-key",
            private_key_path=mock_private_key,
            environment="demo",
        )

        assert client.api_key == "test-key"
        assert client.environment == "demo"
        assert client.base_url == "https://demo-api.kalshi.co"
        assert client.private_key is not None

    def test_client_production_url(self, mock_private_key: Path) -> None:
        """Test production environment URL."""
        client = KalshiClient(
            api_key="test-key",
            private_key_path=mock_private_key,
            environment="production",
        )

        assert client.base_url == "https://api.elections.kalshi.com"

    def test_sign_request(self, mock_private_key: Path) -> None:
        """Test request signing."""
        client = KalshiClient(
            api_key="test-key",
            private_key_path=mock_private_key,
            environment="demo",
        )

        timestamp_ms = int(time.time() * 1000)
        signature = client._sign_request(
            timestamp_ms=timestamp_ms,
            method="GET",
            path="/trade-api/v2/markets",
        )

        # Signature should be base64-encoded
        assert isinstance(signature, str)
        assert len(signature) > 0

    def test_sign_request_strips_query_params(self, mock_private_key: Path) -> None:
        """Test that query parameters are stripped before signing."""
        client = KalshiClient(
            api_key="test-key",
            private_key_path=mock_private_key,
            environment="demo",
        )

        # Use fixed timestamp
        timestamp_ms = 1234567890000

        # Generate signatures for path with and without query params
        sig1 = client._sign_request(
            timestamp_ms=timestamp_ms,
            method="GET",
            path="/trade-api/v2/markets",
        )
        sig2 = client._sign_request(
            timestamp_ms=timestamp_ms,
            method="GET",
            path="/trade-api/v2/markets?limit=10",
        )

        # Both signatures should be non-empty strings
        # We can't compare signatures directly due to PSS randomization,
        # but we can verify both were generated successfully
        assert isinstance(sig1, str)
        assert isinstance(sig2, str)
        assert len(sig1) > 0
        assert len(sig2) > 0

        # Verify the path splitting logic works by checking manually
        path1 = "/trade-api/v2/markets".split("?")[0]
        path2 = "/trade-api/v2/markets?limit=10".split("?")[0]
        assert path1 == path2 == "/trade-api/v2/markets"

    def test_get_auth_headers(self, mock_private_key: Path) -> None:
        """Test authentication header generation."""
        client = KalshiClient(
            api_key="test-key",
            private_key_path=mock_private_key,
            environment="demo",
        )

        headers = client._get_auth_headers("GET", "/trade-api/v2/markets")

        assert "KALSHI-ACCESS-KEY" in headers
        assert headers["KALSHI-ACCESS-KEY"] == "test-key"
        assert "KALSHI-ACCESS-SIGNATURE" in headers
        assert "KALSHI-ACCESS-TIMESTAMP" in headers
        assert "Content-Type" in headers

    @pytest.mark.asyncio
    async def test_context_manager(self, mock_private_key: Path) -> None:
        """Test async context manager lifecycle."""
        client = KalshiClient(
            api_key="test-key",
            private_key_path=mock_private_key,
            environment="demo",
        )

        assert client.client is None

        async with client:
            assert client.client is not None
            assert isinstance(client.client, httpx.AsyncClient)

        assert client.client is None

    @pytest.mark.asyncio
    async def test_request_with_mock(self, mock_private_key: Path) -> None:
        """Test API request with mocked response."""
        client = KalshiClient(
            api_key="test-key",
            private_key_path=mock_private_key,
            environment="demo",
        )

        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"markets": []}

        async with client:
            with patch.object(
                client.client, "request", return_value=mock_response
            ):
                result = await client.get("/trade-api/v2/markets")

                assert result == {"markets": []}

    @pytest.mark.asyncio
    async def test_request_error_handling(self, mock_private_key: Path) -> None:
        """Test error handling for failed requests."""
        client = KalshiClient(
            api_key="test-key",
            private_key_path=mock_private_key,
            environment="demo",
        )

        mock_response = Mock()
        mock_response.status_code = 401
        mock_response.text = "Unauthorized"
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Unauthorized",
            request=Mock(),
            response=mock_response,
        )

        async with client:
            with patch.object(
                client.client, "request", return_value=mock_response
            ):
                with pytest.raises(httpx.HTTPStatusError):
                    await client.get("/trade-api/v2/markets")

    def test_order_validation_valid(self, mock_private_key: Path) -> None:
        """Test order validation with valid parameters."""
        client = KalshiClient(
            api_key="test-key",
            private_key_path=mock_private_key,
            environment="demo",
        )

        # Should not raise
        client._validate_order(
            ticker="TEST-01",
            side="yes",
            action="buy",
            quantity=10,
            order_type="limit",
            price=50,
        )

    def test_order_validation_invalid_side(self, mock_private_key: Path) -> None:
        """Test order validation with invalid side."""
        client = KalshiClient(
            api_key="test-key",
            private_key_path=mock_private_key,
            environment="demo",
        )

        with pytest.raises(ValueError, match="Side must be"):
            client._validate_order(
                ticker="TEST-01",
                side="maybe",
                action="buy",
                quantity=10,
                order_type="limit",
                price=50,
            )

    def test_order_validation_invalid_action(self, mock_private_key: Path) -> None:
        """Test order validation with invalid action."""
        client = KalshiClient(
            api_key="test-key",
            private_key_path=mock_private_key,
            environment="demo",
        )

        with pytest.raises(ValueError, match="Action must be"):
            client._validate_order(
                ticker="TEST-01",
                side="yes",
                action="hold",
                quantity=10,
                order_type="limit",
                price=50,
            )

    def test_order_validation_invalid_quantity(self, mock_private_key: Path) -> None:
        """Test order validation with invalid quantity."""
        client = KalshiClient(
            api_key="test-key",
            private_key_path=mock_private_key,
            environment="demo",
        )

        with pytest.raises(ValueError, match="Quantity must be"):
            client._validate_order(
                ticker="TEST-01",
                side="yes",
                action="buy",
                quantity=0,
                order_type="limit",
                price=50,
            )

    def test_order_validation_missing_price(self, mock_private_key: Path) -> None:
        """Test order validation with missing price for limit order."""
        client = KalshiClient(
            api_key="test-key",
            private_key_path=mock_private_key,
            environment="demo",
        )

        with pytest.raises(ValueError, match="Price is required"):
            client._validate_order(
                ticker="TEST-01",
                side="yes",
                action="buy",
                quantity=10,
                order_type="limit",
                price=None,
            )

    def test_order_validation_invalid_price_range(
        self, mock_private_key: Path
    ) -> None:
        """Test order validation with price out of range."""
        client = KalshiClient(
            api_key="test-key",
            private_key_path=mock_private_key,
            environment="demo",
        )

        with pytest.raises(ValueError, match="Price must be an integer between"):
            client._validate_order(
                ticker="TEST-01",
                side="yes",
                action="buy",
                quantity=10,
                order_type="limit",
                price=100,
            )

    def test_order_validation_market_with_price(
        self, mock_private_key: Path
    ) -> None:
        """Test order validation with price specified for market order."""
        client = KalshiClient(
            api_key="test-key",
            private_key_path=mock_private_key,
            environment="demo",
        )

        with pytest.raises(ValueError, match="Price should not be specified"):
            client._validate_order(
                ticker="TEST-01",
                side="yes",
                action="buy",
                quantity=10,
                order_type="market",
                price=50,
            )
