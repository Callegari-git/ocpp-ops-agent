"""Clock and sleep abstractions.

Code that needs the current time takes a `Clock` (a function returning "now") instead of
calling `datetime.now()` itself, and code that waits takes a `Sleep` instead of calling
`asyncio.sleep()`. Tests pass versions they control; a simulated clock can replace real time
later without touching the callers.
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

Clock = Callable[[], datetime]
Sleep = Callable[[float], Awaitable[None]]


def utc_now() -> datetime:
    """Real wall-clock time, timezone-aware, in UTC."""
    return datetime.now(UTC)
