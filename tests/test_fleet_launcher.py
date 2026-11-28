"""Fleet launcher against a real CSMS, under virtual time (ManualTime): fast and exact."""

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ocpp.v16.enums import ChargePointStatus as S

from fakes import HEARTBEAT_INTERVAL, CsmsHarness, running_csms, wait_until
from manual_time import ManualTime
from ocpp_ops.fleet.config import ChargerTemplate, Fleet
from ocpp_ops.fleet.launcher import FleetMember, build_fleet, charger_seed, run_fleet
from ocpp_ops.message_log import LogRecord, MessageLog
from sim import assert_closed_cleanly

JITTER = 10.0


def _fleet(seed: int = 7, extra: bool = False) -> Fleet:
    """3 chargers (2 + 1), plus a 4th from another template if `extra`."""
    templates = [
        ChargerTemplate(id_prefix="A-", vendor="VendA", model="ModA", connectors=2, count=2),
        ChargerTemplate(id_prefix="B-", vendor="VendB", model="ModB", connectors=1, count=1),
    ]
    if extra:
        templates.insert(
            0, ChargerTemplate(id_prefix="X-", vendor="VendX", model="ModX", connectors=3, count=1)
        )
    # The CSMS harness imposes its own heartbeat interval; this one is not used here.
    return Fleet(
        seed=seed, heartbeat_interval=60, startup_jitter_seconds=JITTER, templates=templates
    )


@asynccontextmanager
async def _running(
    members: list[FleetMember], time: ManualTime, csms: CsmsHarness
) -> AsyncIterator[None]:
    """Run the fleet; stop it when the block exits. Every charger still connected at the
    stop must close with 1001 (see sim.running)."""
    task = asyncio.create_task(run_fleet(members, time.sleep))
    try:
        # Every charger is parked in its start-up sleep before the test advances time.
        await wait_until(lambda: time.sleeper_count == len(members))
        yield
    except BaseException:
        await _stop(task)
        raise
    connected = [member.charger.id for member in members if member.charger.connected]
    await _stop(task)
    for charger_id in connected:
        await assert_closed_cleanly(csms, charger_id)


async def _stop(task: asyncio.Task[None]) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def _run_until(time: ManualTime, members: list[FleetMember], until: datetime) -> None:
    """Advance virtual time one sleeper at a time up to `until`.

    After each step, wait until every charger is asleep again (one sleep each: start-up delay
    or next heartbeat). Only one charger is ever active, so the run is deterministic.
    """
    while (deadline := time.next_deadline) is not None and deadline <= until:
        time.advance_to_next()
        await wait_until(lambda: time.sleeper_count == len(members))


def _read_log(path: Path) -> list[LogRecord]:
    return [LogRecord.model_validate_json(line) for line in path.read_text().splitlines()]


# --- Seeds ----------------------------------------------------------------------------


def test_charger_seed_is_stable_and_depends_on_both_inputs() -> None:
    # A fixed value: the same seed must give the same fleet on any machine, in any process.
    assert charger_seed(42, "VTX-AC-001") == charger_seed(42, "VTX-AC-001")
    assert charger_seed(42, "VTX-AC-001") == 0x5FE89319BB5C7796
    assert charger_seed(42, "VTX-AC-001") != charger_seed(43, "VTX-AC-001")
    assert charger_seed(42, "VTX-AC-001") != charger_seed(42, "VTX-AC-002")


def test_startup_delays_are_seeded_and_within_the_window() -> None:
    time = ManualTime()
    url = "ws://unused"
    delays = [m.startup_delay for m in build_fleet(_fleet(), url, now=time, sleep=time.sleep)]
    again = [m.startup_delay for m in build_fleet(_fleet(), url, now=time, sleep=time.sleep)]
    other = [m.startup_delay for m in build_fleet(_fleet(seed=8), url, now=time, sleep=time.sleep)]
    assert delays == again
    assert delays != other
    assert all(0 <= d <= JITTER for d in delays)
    assert len(set(delays)) == len(delays)


def test_chargers_get_template_identity() -> None:
    time = ManualTime()
    members = build_fleet(_fleet(), "ws://unused", now=time, sleep=time.sleep)
    assert [(m.charger.id, m.charger.connector_ids) for m in members] == [
        ("A-001", [0, 1, 2]),
        ("A-002", [0, 1, 2]),
        ("B-001", [0, 1]),
    ]


# --- Integration: CSMS + 3 chargers ----------------------------------------------------


async def test_three_chargers_boot_and_heartbeat(time: ManualTime, sim_csms: CsmsHarness) -> None:
    start = time()
    members = build_fleet(_fleet(), sim_csms.url, now=time, sleep=time.sleep)
    async with _running(members, time, sim_csms):
        end = start + timedelta(seconds=JITTER + 2 * HEARTBEAT_INTERVAL)
        await _run_until(time, members, end)

        for member in members:
            charger = member.charger
            state = sim_csms.registry.get(charger.id)
            assert state is not None and state.connected
            assert charger.booted  # only set once BootNotification is Accepted
            # Each charger booted exactly at the end of its own start-up delay...
            boot_time = start + timedelta(seconds=member.startup_delay)
            assert state.boot_time == boot_time
            assert (state.vendor, state.model) == (
                ("VendB", "ModB") if charger.id.startswith("B-") else ("VendA", "ModA")
            )
            assert {i: c.status for i, c in state.connectors.items()} == {
                i: S.available for i in charger.connector_ids
            }
            # ...and its second heartbeat came two intervals later.
            assert state.last_heartbeat == boot_time + timedelta(seconds=2 * HEARTBEAT_INTERVAL)

    boots = [
        r
        for r in _read_log(sim_csms.log_path)
        if r.action == "BootNotification" and r.message_type == "CALLRESULT"
    ]
    assert [r.payload["status"] for r in boots] == ["Accepted"] * 3


# --- Reproducibility --------------------------------------------------------------------


async def _scenario(fleet: Fleet, run_dir: Path) -> tuple[dict[str, list[Any]], list[float]]:
    """Run the fleet through boot and two heartbeats each. Returns each charger's own log
    lines (from the charger-side log) and the start-up delays."""
    time = ManualTime()
    with MessageLog(run_dir / "fleet.jsonl") as log:
        async with running_csms(time, run_dir / "csms.jsonl") as csms:
            members = build_fleet(fleet, csms.url, now=time, sleep=time.sleep, log=log)
            async with _running(members, time, csms):
                end = time() + timedelta(seconds=JITTER + 2 * HEARTBEAT_INTERVAL)
                await _run_until(time, members, end)
    per_charger: dict[str, list[Any]] = defaultdict(list)
    for r in _read_log(run_dir / "fleet.jsonl"):
        per_charger[r.charger_id].append(
            (r.timestamp, r.direction, r.message_type, r.message_id, r.action, r.payload)
        )
    return dict(per_charger), [m.startup_delay for m in members]


async def test_same_seed_same_events(tmp_path: Path) -> None:
    events_a, delays_a = await _scenario(_fleet(seed=7), tmp_path / "a")
    events_b, delays_b = await _scenario(_fleet(seed=7), tmp_path / "b")
    events_c, _ = await _scenario(_fleet(seed=8), tmp_path / "c")

    assert sorted(events_a) == ["A-001", "A-002", "B-001"]
    assert all(len(events) > 10 for events in events_a.values())
    # Every field, message ids and timestamps included.
    assert events_a == events_b
    assert delays_a == delays_b
    # Another seed: other delays (so other timestamps) and other message ids.
    assert events_a["A-001"] != events_c["A-001"]


async def test_adding_a_charger_leaves_the_others_unchanged(tmp_path: Path) -> None:
    events, _ = await _scenario(_fleet(), tmp_path / "three")
    events_with_extra, _ = await _scenario(_fleet(extra=True), tmp_path / "four")

    assert sorted(events_with_extra) == ["A-001", "A-002", "B-001", "X-001"]
    for charger_id, charger_events in events.items():
        assert events_with_extra[charger_id] == charger_events
