"""Virtual time for tests: the clock and every sleep move only when the test says so."""

import asyncio
import heapq
import itertools
from datetime import datetime, timedelta

from fakes import FakeClock


class ManualTime(FakeClock):
    """A FakeClock that can also `sleep`, for code that takes injected `now` and `sleep`.

    sleep(s) parks the caller until the test calls advance_to_next() and that sleeper is the
    earliest one. Nothing waits in real time, and timings can be asserted exactly.

    Network I/O still happens in real time, so a test advances one step, then waits (with
    wait_until) for the effect and for the code to be asleep again before the next step.
    """

    def __init__(self) -> None:
        super().__init__()
        # (deadline, insertion order, future to resolve). The order breaks ties: FIFO.
        self._sleepers: list[tuple[datetime, int, asyncio.Future[None]]] = []
        self._order = itertools.count()
        self.requested: list[float] = []  # every duration passed to sleep(), in order

    async def sleep(self, seconds: float) -> None:
        self.requested.append(seconds)
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        deadline = self.current + timedelta(seconds=seconds)
        heapq.heappush(self._sleepers, (deadline, next(self._order), future))
        await future  # cancelling the sleeping task cancels this future too

    @property
    def sleeper_count(self) -> int:
        """Sleeps still waiting (cancelled ones don't count)."""
        return sum(1 for _, _, future in self._sleepers if not future.done())

    @property
    def next_deadline(self) -> datetime | None:
        """When the earliest pending sleep ends, or None if nothing is sleeping."""
        pending = [deadline for deadline, _, future in self._sleepers if not future.done()]
        return min(pending, default=None)

    def advance_to_next(self) -> float:
        """Move time to the earliest pending deadline and wake that sleeper.

        Returns how many seconds time moved: the exact duration the code asked to sleep
        (minus any time already advanced since).
        """
        while self._sleepers and self._sleepers[0][2].done():
            heapq.heappop(self._sleepers)  # drop cancelled sleeps
        if not self._sleepers:
            raise AssertionError("advance_to_next(): nothing is sleeping")
        deadline, _, future = heapq.heappop(self._sleepers)
        elapsed = (deadline - self.current).total_seconds()
        self.current = deadline
        future.set_result(None)
        return elapsed
