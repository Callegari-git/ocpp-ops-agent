"""Helpers to run virtual chargers against a CSMS under virtual time."""

import asyncio
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from manual_time import ManualTime
from ocpp_ops.charger.charger import VirtualCharger
from ocpp_ops.message_log import MessageLog

REBOOT_DELAY = 5.0


def make_charger(
    url: str,
    time: ManualTime,
    charger_id: str = "CP_1",
    connectors: int = 2,
    seed: int = 0,
    log: MessageLog | None = None,
) -> VirtualCharger:
    return VirtualCharger(
        charger_id,
        url,
        connectors,
        now=time,
        sleep=time.sleep,
        rng=random.Random(seed),
        log=log,
        reboot_delay=REBOOT_DELAY,
        call_timeout=2.0,
    )


@asynccontextmanager
async def running(charger: VirtualCharger) -> AsyncIterator[VirtualCharger]:
    """Run charger.run() in the background; stop it when the block exits."""
    task = asyncio.create_task(charger.run())
    try:
        yield charger
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
