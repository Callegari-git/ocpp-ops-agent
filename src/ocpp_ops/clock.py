"""Clock abstraction.

Code that needs the current time takes a `Clock` (a function returning "now") instead of
calling `datetime.now()` itself. Tests pass a fake clock they control; a simulated clock can
replace real time later without touching the callers.
"""

from collections.abc import Callable
from datetime import UTC, datetime

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    """Real wall-clock time, timezone-aware, in UTC."""
    return datetime.now(UTC)
