"""Helpers to run virtual chargers against a CSMS under virtual time."""

import asyncio
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fakes import CsmsHarness, wait_until
from manual_time import ManualTime
from ocpp_ops.charger.charger import DEFAULT_MAX_POWER_W, VirtualCharger
from ocpp_ops.message_log import LogRecord, MessageLog

REBOOT_DELAY = 5.0


def make_charger(
    url: str,
    time: ManualTime,
    charger_id: str = "CP_1",
    connectors: int = 2,
    seed: int = 0,
    log: MessageLog | None = None,
    max_power_w: int = DEFAULT_MAX_POWER_W,
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
        max_power_w=max_power_w,
    )


def drop_connection(charger: VirtualCharger) -> None:
    """Cut the charger's TCP connection without a close frame, like a network failure.

    Test-only access to a private attribute: real chargers have no "drop" button.
    """
    websocket = charger._websocket
    assert websocket is not None, f"{charger.id} is not connected"
    websocket.transport.abort()


@asynccontextmanager
async def running(
    charger: VirtualCharger, csms: CsmsHarness | None = None
) -> AsyncIterator[VirtualCharger]:
    """Run charger.run() in the background; stop it when the block exits.

    With `csms`, a charger still connected when stopped must close with 1001 (going away).
    On Python 3.12+, an exception raised in a session task while the session is stopped is
    replaced by CancelledError: the stop looks clean, except that the connection closes
    with 1011. This check makes such a hidden error fail the test.
    """
    task = asyncio.create_task(charger.run())
    try:
        yield charger
    except BaseException:
        await _stop(task)
        raise
    was_connected = charger.connected
    await _stop(task)
    if csms is not None and was_connected:
        await assert_closed_cleanly(csms, charger.id)


async def _stop(task: asyncio.Task[None]) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def assert_closed_cleanly(csms: CsmsHarness, charger_id: str) -> None:
    """The charger's last connection to the CSMS ended with 1001 (going away)."""
    await wait_until(lambda: not _connected(csms, charger_id))
    disconnects = [
        record
        for record in read_log(csms.log_path)
        if record.message_type == "DISCONNECTED" and record.charger_id == charger_id
    ]
    assert disconnects, f"{charger_id}: no DISCONNECTED in the CSMS log"
    assert disconnects[-1].payload["close_code"] == 1001, disconnects[-1].payload


def _connected(csms: CsmsHarness, charger_id: str) -> bool:
    state = csms.registry.get(charger_id)
    return state is not None and state.connected


def read_log(path: Path) -> list[LogRecord]:
    return [LogRecord.model_validate_json(line) for line in path.read_text().splitlines()]
