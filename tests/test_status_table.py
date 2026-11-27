"""The live status table: rows from either side, rendering, and the refresh loop."""

import asyncio
import io
import random
from contextlib import suppress
from datetime import timedelta

from ocpp.v16.enums import ChargePointErrorCode as E
from ocpp.v16.enums import ChargePointStatus as S

from fakes import FakeClock, wait_until
from manual_time import ManualTime
from ocpp_ops.charger.charger import VirtualCharger
from ocpp_ops.csms.registry import Registry
from ocpp_ops.fleet.status_table import (
    ChargerRow,
    render,
    rows_from_chargers,
    rows_from_registry,
    show_status,
    summary,
)


def _registry(clock: FakeClock) -> Registry:
    registry = Registry(now=clock)
    registry.connect("CP-1")
    registry.record_boot("CP-1", "V", "M")
    registry.record_status("CP-1", 0, S.available, E.no_error)
    registry.record_status("CP-1", 1, S.faulted, E.ground_failure)
    registry.record_heartbeat("CP-1")
    # CP-2 connected but has not booted yet.
    registry.connect("CP-2")
    return registry


def test_rows_from_registry_include_chargers_not_seen_yet() -> None:
    clock = FakeClock()
    rows = rows_from_registry(["CP-1", "CP-2", "CP-3"], _registry(clock))

    assert rows == [
        ChargerRow(
            "CP-1", True, True, True, {0: "Available", 1: "Faulted(GroundFailure)"}, clock()
        ),
        ChargerRow("CP-2", True, True, False, {}, None),
        ChargerRow("CP-3", False, False, False, {}, None),
    ]


def test_rows_from_registry_disconnected_charger_is_not_booted() -> None:
    clock = FakeClock()
    registry = _registry(clock)
    registry.disconnect("CP-1", token=1)
    [row] = rows_from_registry(["CP-1"], registry)
    assert (row.connected, row.booted) == (False, False)


def test_rows_from_chargers_before_connecting() -> None:
    time = ManualTime()
    charger = VirtualCharger(
        "CP-9", "ws://unused", 2, now=time, sleep=time.sleep, rng=random.Random(0)
    )
    assert rows_from_chargers([charger]) == [
        ChargerRow(
            "CP-9", True, False, False, {0: "Available", 1: "Available", 2: "Available"}, None
        )
    ]


def test_render_table_and_summary() -> None:
    clock = FakeClock()
    rows = rows_from_registry(["CP-1", "CP-2", "CP-3"], _registry(clock))
    clock.advance(12)

    lines = render(rows, clock()).splitlines()
    assert lines[0].split() == ["CHARGER", "CONN", "BOOT", "HB", "AGO", "CONNECTORS"]
    assert lines[1].split() == [
        "CP-1",
        "yes",
        "Accepted",
        "12s",
        "0:Available",
        "1:Faulted(GroundFailure)",
    ]
    assert lines[2].split() == ["CP-2", "yes", "-", "-"]
    assert lines[3].split() == ["CP-3", "no", "-", "-", "not", "seen"]
    assert lines[-1] == "12:00:12 UTC  2/3 connected, 1 booted"
    assert summary(rows, clock()) == lines[-1]


async def test_show_status_prints_a_summary_per_refresh_when_not_a_terminal() -> None:
    time = ManualTime()
    out = io.StringIO()  # isatty() is False, like a pipe
    rows = [ChargerRow("CP-1", True, True, True, {0: "Available"}, None)]
    task = asyncio.create_task(show_status(lambda: rows, time, time.sleep, out))
    for _ in range(2):
        await _wait_for_sleeper(time)
        time.advance_to_next()
    await _wait_for_sleeper(time)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    assert out.getvalue().splitlines() == [
        "12:00:00 UTC  1/1 connected, 1 booted",
        "12:00:01 UTC  1/1 connected, 1 booted",
        "12:00:02 UTC  1/1 connected, 1 booted",
    ]
    assert time.requested == [1.0, 1.0, 1.0]


class _Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


async def test_show_status_redraws_in_place_on_a_terminal() -> None:
    time = ManualTime()
    out = _Terminal()
    rows = [ChargerRow("CP-1", True, True, True, {0: "Available"}, time() - timedelta(seconds=3))]
    task = asyncio.create_task(show_status(lambda: rows, time, time.sleep, out))
    await _wait_for_sleeper(time)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task

    text = out.getvalue()
    assert text.startswith("\x1b[?1049h\x1b[H")  # alternate screen, cursor home
    assert "CP-1" in text and "3s" in text
    # On exit: back to the normal screen, with the last summary left behind.
    assert text.endswith("\x1b[?1049l12:00:00 UTC  1/1 connected, 1 booted\n")


async def _wait_for_sleeper(time: ManualTime) -> None:
    await wait_until(lambda: time.sleeper_count > 0)
