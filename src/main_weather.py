"""
Standalone weather trading entry point.

Runs ONLY the weather trading strategies (temperature, snow, rain markets)
without initializing crypto strategies or general strategies.

Usage:
    python -m src.main_weather          # Paper mode
    python -m src.main_weather --live   # Live mode (requires confirmation)
"""
import asyncio
import os
import sys

from dotenv import load_dotenv
load_dotenv()

from src.utils.logging import logger


async def run_weather_system(
    paper_trading: bool = True,
    auto_restart: bool = True,
    max_restarts: int = 5,
) -> None:
    """Run the weather-only trading system with auto-restart."""
    # Force weather-only mode via environment before importing TradingSystem
    os.environ["WEATHER_ONLY_MODE"] = "true"
    os.environ["CRYPTO_ENABLED"] = "false"

    from src.main import TradingSystem

    restart_count = 0
    restart_delay = 30

    while restart_count <= max_restarts:
        try:
            system = TradingSystem(
                paper_trading=paper_trading,
                crypto_only=False,
            )
            await system.run()
            break
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received, shutting down weather system")
            break
        except Exception as e:
            restart_count += 1
            logger.error(
                "Weather system crashed",
                error=str(e),
                restart_count=restart_count,
                max_restarts=max_restarts,
            )
            if not auto_restart or restart_count > max_restarts:
                raise
            logger.info(f"Auto-restarting weather in {restart_delay}s...")
            await asyncio.sleep(restart_delay)
            restart_delay = min(restart_delay * 2, 300)


def main() -> None:
    """CLI entry point for weather-only trading."""
    import argparse

    parser = argparse.ArgumentParser(description="Kalshi Weather Trading System")
    parser.add_argument(
        "--live",
        action="store_true",
        help="Run in live trading mode (default: paper trading)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging",
    )
    args = parser.parse_args()

    if args.verbose:
        import logging
        logging.getLogger().setLevel(logging.DEBUG)

    paper_trading = not args.live
    if not paper_trading:
        print("WARNING: Running WEATHER in LIVE trading mode!")
        response = input("Type 'CONFIRM' to continue: ")
        if response != "CONFIRM":
            print("Aborted.")
            sys.exit(1)

    asyncio.run(run_weather_system(paper_trading=paper_trading))


if __name__ == "__main__":
    main()
