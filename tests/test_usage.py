"""Simulated drivers (fleet/usage.py): seeded visits, and sessions against a real CSMS under
virtual time (ManualTime)."""

import asyncio
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from ocpp.v16.enums import AuthorizationStatus, ResetType
from ocpp.v16.enums import ChargePointStatus as S

from fakes import (
    ACCEPTED_TAG,
    BLOCKED_TAG,
    EXPIRED_TAG,
    TEST_AUTHORIZATION,
    CsmsHarness,
    running_csms,
    wait_until,
)
from manual_time import ManualTime
from ocpp_ops.charger.charger import VirtualCharger
from ocpp_ops.csms.authorization import AuthorizationList
from ocpp_ops.fleet.config import ChargerTemplate, Fleet, MinMax, Usage
from ocpp_ops.fleet.launcher import FleetMember, build_fleet, run_fleet
from ocpp_ops.fleet.status_rows import rows_from_chargers
from ocpp_ops.fleet.usage import Driver, TagPool, plan_visit, usage_seed
from sim import REBOOT_DELAY, assert_closed_cleanly, make_charger, read_log, running

USAGE = Usage(
    mean_idle_minutes=1,
    charge_minutes=MinMax(min=1, max=3),
    driver_stop_share=0.5,
    stay_after_stop_minutes=MinMax(min=0.1, max=0.5),
    refused_tag_share=0.3,
)
TAGS = TagPool.from_authorization(TEST_AUTHORIZATION)


# --- Drawing visits (pure) -----------------------------------------------------------------


def _visits(seed: int, count: int = 20, usage: Usage = USAGE) -> list[Any]:
    rng = random.Random(seed)
    return [plan_visit(usage, TAGS, rng) for _ in range(count)]


def test_tag_pool_from_the_authorization_list() -> None:
    assert TAGS == TagPool(accepted=(ACCEPTED_TAG,), refused=(BLOCKED_TAG, EXPIRED_TAG))
    with pytest.raises(ValueError, match="no Accepted tag"):
        TagPool.from_authorization(AuthorizationList({"T-1": "Blocked"}))


def test_same_seed_same_visits() -> None:
    assert _visits(1) == _visits(1)
    assert _visits(1) != _visits(2)


def test_visits_stay_within_the_profile() -> None:
    for visit in _visits(3, count=200):
        assert visit.idle_s > 0
        assert 5 <= visit.authorize_delay_s <= 30
        assert 60 <= visit.charge_s <= 180
        assert 6 <= visit.stay_after_stop_s <= 30
    tags = {visit.id_tag for visit in _visits(3, count=200)}
    assert tags == {ACCEPTED_TAG, BLOCKED_TAG, EXPIRED_TAG}


@pytest.mark.parametrize(("share", "expected"), [(0.0, {ACCEPTED_TAG}), (1.0, set(TAGS.refused))])
def test_refused_tag_share(share: float, expected: set[str]) -> None:
    usage = USAGE.model_copy(update={"refused_tag_share": share})
    assert {visit.id_tag for visit in _visits(4, count=50, usage=usage)} == expected


def test_refused_share_without_refused_tags_uses_accepted_ones() -> None:
    tags = TagPool(accepted=("ONLY",), refused=())
    usage = USAGE.model_copy(update={"refused_tag_share": 1.0})
    assert {plan_visit(usage, tags, random.Random(0)).id_tag for _ in range(5)} == {"ONLY"}


def test_each_connector_has_its_own_stream() -> None:
    seeds = {usage_seed(7, charger, connector) for charger in ("A", "B") for connector in (1, 2)}
    assert len(seeds) == 4
    assert usage_seed(7, "A", 1) == usage_seed(7, "A", 1)  # stable: sha256, not hash()
    assert usage_seed(7, "A", 1) != usage_seed(8, "A", 1)


def _template(prefix: str, connectors: int, current: str = "AC") -> ChargerTemplate:
    return ChargerTemplate(
        id_prefix=prefix, vendor="V", model="M", connectors=connectors, count=1, current=current
    )


def _fleet(seed: int = 7, extra: bool = False) -> Fleet:
    """A-001 (2 connectors, AC) and B-001 (1 connector, DC); X-001 first if `extra`."""
    templates = [_template("A-", 2), _template("B-", 1, "DC")]
    if extra:
        templates.insert(0, _template("X-", 3))
    return Fleet(
        seed=seed,
        heartbeat_interval=60,
        startup_jitter_seconds=0,
        templates=templates,
        usage=USAGE,
    )


def _build(fleet: Fleet, url: str, time: ManualTime) -> list[FleetMember]:
    return build_fleet(fleet, url, now=time, sleep=time.sleep, tags=TAGS)


def test_one_driver_per_connector_with_the_template_power() -> None:
    members = _build(_fleet(), "ws://unused", ManualTime())
    assert [(m.charger.id, len(m.drivers), m.charger.max_power_w) for m in members] == [
        ("A-001", 2, 11_000),
        ("B-001", 1, 50_000),
    ]
    assert [d.connector_id for d in members[0].drivers] == [1, 2]


def test_adding_a_charger_leaves_the_other_drivers_unchanged() -> None:
    def first_visits(members: list[FleetMember]) -> dict[tuple[str, int], list[Any]]:
        return {
            (m.charger.id, d.connector_id): [plan_visit(USAGE, TAGS, d.rng) for _ in range(10)]
            for m in members
            for d in m.drivers
        }

    base = first_visits(_build(_fleet(), "ws://unused", ManualTime()))
    more = first_visits(_build(_fleet(extra=True), "ws://unused", ManualTime()))
    assert set(more) - set(base) == {("X-001", 1), ("X-001", 2), ("X-001", 3)}
    assert {key: more[key] for key in base} == base


def test_usage_needs_tags() -> None:
    with pytest.raises(ValueError, match="needs the tags"):
        build_fleet(_fleet(), "ws://unused", now=ManualTime(), sleep=ManualTime().sleep)


def test_no_usage_no_drivers() -> None:
    fleet = _fleet().model_copy(update={"usage": None})
    members = build_fleet(fleet, "ws://unused", now=ManualTime(), sleep=ManualTime().sleep)
    assert all(member.drivers == () for member in members)


# --- Sessions against a real CSMS ----------------------------------------------------------


def _quiet(time: ManualTime, chargers: list[VirtualCharger], drivers: int) -> bool:
    """Everything delivered and every task asleep: a heartbeat per booted charger, a sampler
    per transaction, one sleep per driver. Only then is it safe to move time."""
    expected = drivers + sum(c.booted + c.active_transactions for c in chargers)
    return all(c.queued_messages == 0 for c in chargers) and time.sleeper_count == expected


@asynccontextmanager
async def _running_fleet(
    members: list[FleetMember], time: ManualTime, csms: CsmsHarness
) -> AsyncIterator[list[VirtualCharger]]:
    """Run the fleet until every charger has booted and settled; stop it on exit (every
    charger still connected must close with 1001)."""
    chargers = [m.charger for m in members]
    drivers = sum(len(m.drivers) for m in members)
    task = asyncio.create_task(run_fleet(members, time.sleep))
    try:
        await wait_until(lambda: time.sleeper_count == len(members))  # start-up delays (0 s)
        for _ in members:
            time.advance_to_next()
        booted = lambda: all(c.booted for c in chargers)  # noqa: E731
        await wait_until(lambda: booted() and _quiet(time, chargers, drivers))
        yield chargers
    finally:
        connected = [c.id for c in chargers if c.connected]
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
    for charger_id in connected:
        await assert_closed_cleanly(csms, charger_id)


async def _run_for(time: ManualTime, members: list[FleetMember], minutes: float) -> None:
    chargers = [m.charger for m in members]
    drivers = sum(len(m.drivers) for m in members)
    until = time() + timedelta(minutes=minutes)
    while (deadline := time.next_deadline) is not None and deadline <= until:
        time.advance_to_next()
        await wait_until(lambda: _quiet(time, chargers, drivers))


def _session_lines(log_path: Path) -> list[tuple[Any, ...]]:
    """What drivers did, as the CSMS saw it: no transaction ids (one counter for the whole
    CSMS, so another charger's sessions would shift them)."""
    lines = []
    for r in read_log(log_path):
        if r.message_type != "CALL" or r.action not in (
            "Authorize",
            "StartTransaction",
            "StopTransaction",
        ):
            continue
        payload = {k: v for k, v in r.payload.items() if k != "transactionId"}
        lines.append((r.timestamp, r.charger_id, r.action, payload))
    return lines


async def _scenario(seed: int, extra: bool, run_dir: Path) -> list[tuple[Any, ...]]:
    time = ManualTime()
    async with running_csms(time, run_dir / "csms.jsonl") as csms:
        members = _build(_fleet(seed, extra), csms.url, time)
        async with _running_fleet(members, time, csms):
            await _run_for(time, members, minutes=12)
    return _session_lines(run_dir / "csms.jsonl")


async def test_same_seed_same_sessions(tmp_path: Path) -> None:
    run_a = await _scenario(7, False, tmp_path / "a")
    run_b = await _scenario(7, False, tmp_path / "b")
    run_c = await _scenario(8, False, tmp_path / "c")

    actions = {line[2] for line in run_a}
    assert actions == {"Authorize", "StartTransaction", "StopTransaction"}
    reasons = {line[3].get("reason") for line in run_a if line[2] == "StopTransaction"}
    assert reasons == {"Local", "EVDisconnected"}  # both ways a driver leaves
    assert run_a == run_b
    assert run_a != run_c


async def test_adding_a_charger_leaves_the_other_sessions_unchanged(tmp_path: Path) -> None:
    base = await _scenario(7, False, tmp_path / "base")
    more = await _scenario(7, True, tmp_path / "more")

    assert {line[1] for line in more} - {line[1] for line in base} == {"X-001"}
    assert [line for line in more if line[1] != "X-001"] == base


async def test_driver_leaves_cleanly_after_a_reset_mid_session(
    time: ManualTime, sim_csms: CsmsHarness
) -> None:
    """A hard reset ends the transaction under the driver: when the charging time is up,
    stop() has nothing to stop, so the driver just takes the cable (Finishing -> Available)
    and the next visits go on."""
    usage = USAGE.model_copy(
        update={"driver_stop_share": 1.0, "refused_tag_share": 0.0, "mean_idle_minutes": 0.5}
    )
    charger = make_charger(sim_csms.url, time, connectors=1)
    driver = Driver(charger, 1, usage, TAGS, random.Random(0))
    async with running(charger, csms=sim_csms):
        task = asyncio.create_task(driver.run(time.sleep))
        try:
            await wait_until(lambda: charger.booted and _quiet(time, [charger], 1))
            while charger.transaction(1) is None:
                time.advance_to_next()
                await wait_until(lambda: _quiet(time, [charger], 1))
            [row] = rows_from_chargers([charger])  # the charger-side view of the grid
            assert (row.active_sessions, row.power_w) == (1, charger.max_power_w)

            result = await sim_csms.central_system.reset("CP_1", ResetType.hard)
            assert result.status.value == "Accepted"
            await wait_until(lambda: not charger.connected and time.sleeper_count == 2)
            assert time.advance_to_next() == REBOOT_DELAY
            await wait_until(lambda: charger.booted and _quiet(time, [charger], 1))
            assert charger.connector(1).status == S.finishing

            # Heartbeats, then the end of the charging time: the driver takes the cable.
            while charger.connector(1).status != S.available:
                time.advance_to_next()
                await wait_until(lambda: _quiet(time, [charger], 1))
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    state = sim_csms.registry.get("CP_1")
    assert state is not None
    [finished] = state.finished_transactions
    assert finished.stop_reason.value == "HardReset"
    assert finished.id_tag_status == AuthorizationStatus.accepted
