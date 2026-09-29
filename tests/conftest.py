"""Test environment: throwaway credentials and a temp SQLite database.

config.settings validates credentials at import time, so the environment is set here,
before any test module imports src. The RSA key is generated per run and never saved.
Tests never touch data/kalshi_trading.db or a real Kalshi account.
"""

import base64
import os
import tempfile

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_pem = _key.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.TraditionalOpenSSL,
    serialization.NoEncryption(),
)
_tmp = tempfile.mkdtemp(prefix="kalshi-tests-")

os.environ["KALSHI_API_KEY"] = "00000000-0000-0000-0000-000000000000"
os.environ["KALSHI_PRIVATE_KEY"] = base64.b64encode(_pem).decode()
os.environ.pop("KALSHI_PRIVATE_KEY_PATH", None)
os.environ["KALSHI_ENVIRONMENT"] = "demo"
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/test.db"
os.environ["LOG_FILE"] = f"{_tmp}/test.log"


@pytest.fixture(scope="session", autouse=True)
def _create_tables():
    from src.data.database import init_db

    init_db()
