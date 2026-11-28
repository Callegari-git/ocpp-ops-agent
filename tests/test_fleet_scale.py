"""Scale, in real time: the default 50-charger fleet against the embedded CSMS.

Skip locally with `pytest -m "not slow"`; CI runs it.
"""

import asyncio
import time
from contextlib import suppress
from pathlib import Path

import pytest

from fakes import TEST_AUTHORIZATION, wait_until
from ocpp_ops.clock import utc_now
from ocpp_ops.csms.central_system import CentralSystem
from ocpp_ops.csms.registry import Registry
from ocpp_ops.csms.server import start_server
from ocpp_ops.fleet.config import ChargerSpec, load_fleet
from ocpp_ops.fleet.launcher import build_fleet, run_fleet
from ocpp_ops.message_log import LogRecord, MessageLog

DEFAULT_FLEET = Path(__file__).parent.parent / "fleet.yaml"
# Generous for slow CI runners; locally it takes well under a second.
BOOT_BOUND_SECONDS = 15.0


def _all_booted(registry: Registry, chargers: list[ChargerSpec]) -> bool:
    """The CSMS has the boot and the status of every connector (0 included) of every charger."""
    for spec in chargers:
        state = registry.get(spec.id)
        if state is None or state.boot_time is None or len(state.connectors) != spec.connectors + 1:
            return False
    return True


@pytest.mark.slow
async def test_fifty_chargers_boot_within_bound(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Jitter 0: all 50 connect at once, the worst case for the CSMS.
    fleet = load_fleet(DEFAULT_FLEET).model_copy(update={"startup_jitter_seconds": 0})
    chargers = fleet.chargers()
    assert len(chargers) == 50

    with MessageLog(tmp_path / "csms.jsonl") as log:
        registry = Registry(now=utc_now)
        central_system = CentralSystem(
            registry,
            log,
            heartbeat_interval=fleet.heartbeat_interval,
            now=utc_now,
            authorization=TEST_AUTHORIZATION,
        )
        async with await start_server(central_system, "127.0.0.1", 0) as server:
            url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            members = build_fleet(fleet, url, now=utc_now, sleep=asyncio.sleep)
            started = time.perf_counter()
            task = asyncio.create_task(run_fleet(members, asyncio.sleep))
            try:
                async with asyncio.timeout(BOOT_BOUND_SECONDS):
                    while not _all_booted(registry, chargers):  # noqa: ASYNC110
                        await asyncio.sleep(0.005)
                elapsed = time.perf_counter() - started
                assert all(state.connected for state in registry.all())
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            # Still inside the server block: every charger closes on its own first.
            await wait_until(lambda: not any(state.connected for state in registry.all()))

    # A clean stop closes every connection with 1001, not 1011 (an error hidden behind
    # the cancellation, see sim.running).
    disconnects = [
        LogRecord.model_validate_json(line)
        for line in (tmp_path / "csms.jsonl").read_text().splitlines()
        if '"DISCONNECTED"' in line
    ]
    assert len(disconnects) == 50
    assert {record.payload["close_code"] for record in disconnects} == {1001}

    with capsys.disabled():  # shown in `make test` output, not captured
        print(f"\n[scale] 50 chargers booted (boot + every StatusNotification) in {elapsed:.3f} s")
