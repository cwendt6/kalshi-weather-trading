"""
Configuration management using pydantic-settings.

Loads settings from environment variables and .env file.
"""
from pathlib import Path
from typing import Dict, List, Literal, Optional, Tuple

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables and .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Kalshi API Credentials
    kalshi_api_key: str = Field(..., description="Kalshi API key (UUID)")
    kalshi_private_key_path: Optional[Path] = Field(
        None, description="Path to Kalshi private key PEM file"
    )
    kalshi_private_key: Optional[str] = Field(
        None, description="Base64-encoded Kalshi private key"
    )
    kalshi_environment: Literal["production", "demo"] = Field(
        default="demo", description="Kalshi environment to use"
    )

    # Database
    database_url: str = Field(
        default="sqlite:///./data/kalshi_trading.db",
        description="SQLAlchemy database URL",
    )

    # Logging
    log_level: str = Field(default="INFO", description="Logging level")
    log_file: Path = Field(
        default=Path("logs/trading.log"), description="Log file path"
    )

    # News APIs
    finnhub_api_key: Optional[str] = Field(
        default=None, description="Finnhub API key for news (optional)"
    )

    # AI/LLM (for forecasting)
    anthropic_api_key: Optional[str] = Field(
        None, description="Anthropic API key for Claude"
    )
    openai_api_key: Optional[str] = Field(
        None, description="OpenAI API key for GPT"
    )
    use_llm_forecaster: bool = Field(
        default=False,
        description="Enable LLM-powered forecasting (requires ANTHROPIC_API_KEY)",
    )

    # Risk Parameters
    max_position_pct: float = Field(
        default=0.10,
        ge=0.0,
        le=1.0,
        description="Max percentage of bankroll per position",
    )
    max_daily_loss_pct: float = Field(
        default=0.05,
        ge=0.0,
        le=1.0,
        description="Stop trading after this daily loss percentage",
    )
    max_total_exposure_pct: float = Field(
        default=0.50,
        ge=0.0,
        le=1.0,
        description="Max percentage of bankroll at risk",
    )
    kelly_fraction: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Kelly fraction for position sizing",
    )
    min_edge_threshold: float = Field(
        default=0.02,
        ge=0.0,
        description="Minimum edge required to trade (paper mode: 2%)",
    )

    # Alerts (optional)
    discord_webhook_url: Optional[str] = Field(
        None, description="Discord webhook URL for alerts"
    )
    slack_webhook_url: Optional[str] = Field(
        None, description="Slack webhook URL for alerts"
    )

    # Paper Trading Mode
    paper_trading: bool = Field(
        default=True, description="Use paper trading mode (no real money)"
    )

    # Focus Mode (weather-only)
    weather_only_mode: bool = Field(
        default=True,
        description="Weather-only mode is always active",
    )

    # Additional risk parameters
    max_drawdown_pct: float = Field(
        default=1.0,
        ge=0.0,
        description="Max drawdown before pausing trading (1.0 = disabled)",
    )
    initial_bankroll: float = Field(
        default=10000.0,
        ge=0.0,
        description="Initial bankroll for paper trading (live mode overwritten by API sync)",
    )

    # Daily trade limits
    max_daily_trades: int = Field(
        default=500, description="Max trades per day (paper mode: high volume)"
    )

    # Strategy allocation (percentage of bankroll)
    alloc_weather_strategy: float = Field(
        default=1.0, ge=0.0, le=1.0, description="Weather strategy allocation (100% in weather-only mode)"
    )

    # Scan intervals (seconds)
    scan_interval_weather: int = Field(
        default=600, description="Weather scan interval (seconds)"
    )

    # Position Management
    position_check_interval: int = Field(
        default=30, description="Position check interval (seconds)"
    )
    trailing_stop_enabled: bool = Field(
        default=True, description="Enable trailing stop exits"
    )
    take_profit_enabled: bool = Field(
        default=True, description="Enable take-profit ladder exits"
    )
    time_decay_exit_enabled: bool = Field(
        default=True, description="Enable time-decay exits for stale positions"
    )
    max_position_age_hours: float = Field(
        default=168.0, ge=1.0, description="Max position age in hours (default 7 days)"
    )

    # ── Weather Probability Model: σ Schedule ──────────────────────────
    # Standard deviation schedule for temperature probability model.
    # Format: list of [start_hour, σ] pairs. σ applies from start_hour
    # until the next entry. Values widened 1.5x from original aggressive
    # schedule per calibration audit.
    weather_high_std_dev_schedule: List[List[float]] = Field(
        default=[
            [0, 4.2],    # Pre-dawn (was 2.8)
            [6, 3.8],    # Early morning (was 2.5)
            [9, 3.5],    # Late morning (was 2.3)
            [12, 2.7],   # Early afternoon (was 1.8)
            [14, 1.8],   # Peak hours (was 1.2)
            [16, 1.2],   # Post-peak (was 0.8)
            [18, 0.8],   # Evening (was 0.5)
        ],
        description="HIGH temp σ by local hour (widened 1.5x from original)",
    )

    weather_low_std_dev_schedule: List[List[float]] = Field(
        default=[
            [0, 3.8],    # Overnight (was 2.5)
            [4, 3.0],    # Pre-dawn (was 2.0)
            [7, 2.3],    # Morning (was 1.5)
            [10, 1.8],   # Late morning (was 1.2)
            [13, 1.5],   # Afternoon (was 1.0)
            [16, 2.0],   # Evening (was 1.3)
            [20, 4.2],   # Late night (was 2.8)
        ],
        description="LOW temp σ by local hour (widened 1.5x from original)",
    )

    # Multi-day forecast σ: {days_out: [high_σ, low_σ]}
    weather_multi_day_std_dev: Dict[str, List[float]] = Field(
        default={
            "1": [4.5, 6.0],   # Tomorrow (was 3.0, 4.0)
            "2": [6.0, 7.5],   # 2 days (was 4.0, 5.0)
            "3": [7.5, 7.5],   # 3 days (was 5.0, 5.0)
        },
        description="Multi-day σ [high, low] by days out (widened 1.5x)",
    )
    weather_multi_day_default: List[float] = Field(
        default=[10.5, 10.5],
        description="Default σ [high, low] for 4+ days out (was 7.0, 7.0)",
    )

    # NWS confidence multipliers applied on top of base σ
    weather_confidence_multipliers: Dict[str, float] = Field(
        default={
            "high": 1.0,
            "medium": 1.15,
            "low": 1.4,
        },
        description="Multiplier for NWS confidence levels",
    )

    def get_strategy_allocation(self) -> dict:
        """Get strategy allocation as a dictionary."""
        return {
            "weather_strategy": self.alloc_weather_strategy,
        }

    @field_validator("kalshi_private_key_path")
    @classmethod
    def validate_private_key_path(cls, v: Optional[Path]) -> Optional[Path]:
        """Validate that private key file exists if path is provided."""
        if v is not None and not v.exists():
            raise ValueError(f"Private key file not found: {v}")
        return v

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Validate log level is one of the standard levels."""
        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        v_upper = v.upper()
        if v_upper not in valid_levels:
            raise ValueError(
                f"Invalid log level: {v}. Must be one of {valid_levels}"
            )
        return v_upper

    def validate_private_key_config(self) -> None:
        """Validate that either private key path or private key is provided."""
        if self.kalshi_private_key_path is None and self.kalshi_private_key is None:
            raise ValueError(
                "Either kalshi_private_key_path or kalshi_private_key must be provided"
            )

    def get_kalshi_base_url(self) -> str:
        """Get the Kalshi API base URL based on environment."""
        if self.kalshi_environment == "production":
            return "https://trading-api.kalshi.com"
        return "https://demo-api.kalshi.co"


# Global settings instance
settings = Settings()  # type: ignore[call-arg]

# Validate private key configuration on load
settings.validate_private_key_config()
