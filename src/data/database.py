"""
Database connection management and session handling.

Provides database engine, session management, and initialization utilities.
"""
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Generator

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from config.settings import settings
from src.data.models import Base
from src.utils.logging import logger

# Retry configuration for transient I/O errors
DB_MAX_RETRIES = 3
DB_RETRY_DELAY = 0.5  # seconds


class DatabaseManager:
    """Manages database connections and sessions."""

    def __init__(self, database_url: str) -> None:
        """
        Initialize database manager.

        Args:
            database_url: SQLAlchemy database URL
        """
        self.database_url = database_url
        connect_args = {}
        if database_url.startswith("sqlite"):
            connect_args = {
                "check_same_thread": False,
                "timeout": 30,
            }

        self.engine = create_engine(
            database_url,
            echo=False,
            pool_pre_ping=True,
            connect_args=connect_args,
        )

        # Enable WAL mode for SQLite to reduce I/O contention
        if database_url.startswith("sqlite"):
            @event.listens_for(self.engine, "connect")
            def set_sqlite_pragma(dbapi_connection, connection_record):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")  # Flush WAL on commit (prevents corruption on crash)
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.execute("PRAGMA wal_autocheckpoint=1000")  # Checkpoint WAL every 1000 pages (~4MB)
                cursor.close()

        self.SessionLocal = sessionmaker(
            autocommit=False, autoflush=False, bind=self.engine
        )
        logger.info("Database manager initialized", database_url=database_url)

    def create_all_tables(self) -> None:
        """Create all database tables if they don't exist."""
        logger.info("Creating database tables")
        Base.metadata.create_all(bind=self.engine)
        logger.info("Database tables created successfully")

    def drop_all_tables(self) -> None:
        """Drop all database tables. USE WITH CAUTION."""
        logger.warning("Dropping all database tables")
        Base.metadata.drop_all(bind=self.engine)
        logger.info("Database tables dropped")

    @contextmanager
    def get_session(self) -> Generator[Session, None, None]:
        """Get a database session with automatic cleanup.

        Yields a session. On normal exit, commits. On exception,
        rolls back. Always closes.

        Note: Retry logic is handled by execute_with_retry()
        for operations that need it.
        """
        session = self.SessionLocal()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def execute_with_retry(
        self, operation: Callable[..., Any], *args: Any, **kwargs: Any,
    ) -> Any:
        """Execute a database operation with retry on transient errors.

        Args:
            operation: Callable that receives a session and performs DB work.
                       Should NOT commit — the wrapper handles that.

        Example:
            def update_trade(session):
                trade = session.query(TradeDB).filter_by(id=123).first()
                trade.resolved = 1

            db_manager.execute_with_retry(update_trade)
        """
        last_error = None
        for attempt in range(1, DB_MAX_RETRIES + 1):
            try:
                with self.get_session() as session:
                    result = operation(session, *args, **kwargs)
                    return result
            except Exception as e:
                last_error = e
                error_msg = str(e).lower()
                is_transient = any(
                    phrase in error_msg
                    for phrase in [
                        "i/o error", "disk i/o",
                        "database is locked", "operational error",
                    ]
                )
                if is_transient and attempt < DB_MAX_RETRIES:
                    logger.warning(
                        "Transient DB error, retrying",
                        error=str(e),
                        attempt=attempt,
                    )
                    time.sleep(DB_RETRY_DELAY * attempt)
                    continue
                raise
        raise last_error

    def ensure_database_directory(self) -> None:
        """Ensure the database directory exists (for SQLite)."""
        if self.database_url.startswith("sqlite"):
            # Extract path from sqlite:///path/to/db.db
            db_path = self.database_url.replace("sqlite:///", "")
            db_file = Path(db_path)
            db_dir = db_file.parent

            if not db_dir.exists():
                logger.info("Creating database directory", path=str(db_dir))
                db_dir.mkdir(parents=True, exist_ok=True)


# Global database manager instance
db_manager = DatabaseManager(settings.database_url)

# Ensure database directory exists
db_manager.ensure_database_directory()


def init_db() -> None:
    """
    Initialize database by creating all tables.

    This should be called once when setting up the application.
    """
    logger.info("Initializing database")
    db_manager.create_all_tables()
    logger.info("Database initialization complete")


def get_db_session() -> Generator[Session, None, None]:
    """
    Dependency function to get database session.

    Yields:
        Database session

    Example:
        >>> with get_db_session() as session:
        ...     market = session.query(MarketDB).first()
    """
    with db_manager.get_session() as session:
        yield session


# Convenience function for testing
def reset_database() -> None:
    """
    Reset database by dropping and recreating all tables.

    WARNING: This will delete all data. Only use in testing or development.
    """
    logger.warning("Resetting database - all data will be lost")
    db_manager.drop_all_tables()
    db_manager.create_all_tables()
    logger.info("Database reset complete")
