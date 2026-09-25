"""
Datetime utilities for timezone-aware operations.

The database stores datetimes as naive (no timezone info) but the Kalshi API
returns timezone-aware datetimes. This module provides helpers to safely mix
both by treating all naive datetimes as UTC.
"""
from datetime import datetime, timezone
from typing import Optional


def ensure_utc(dt: Optional[datetime]) -> Optional[datetime]:
    """Ensure a datetime is timezone-aware UTC.

    If the datetime is naive (no tzinfo), assumes it's UTC and attaches
    the UTC timezone. If it's already timezone-aware, returns it as-is.

    Args:
        dt: A datetime object, or None.

    Returns:
        A timezone-aware datetime in UTC, or None if input was None.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt
