"""Virtual charger against a real CSMS, under virtual time (ManualTime): fast and exact."""

import asyncio
from contextlib import suppress
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from ocpp.routing import on
from ocpp.v16 import ChargePoint, call_result
from ocpp.v16.enums import Action, ConfigurationStatus, RegistrationStatus, ResetStatus, ResetType
from ocpp.v16.enums import ChargePointErrorCode as E
from ocpp.v16.enums import ChargePointStatus as S
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from fakes import HEARTBEAT_INTERVAL, OCPP16, CsmsHarness, running_csms, wait_until
from manual_time import ManualTime
from ocpp_ops.charger.charger import VirtualCharger
from ocpp_ops.charger.state_machine import InvalidTransitionError
from ocpp_ops.csms.registry import ChargerState, ConfigurationKey
from ocpp_ops.message_log import LogRecord, MessageLog
from sim import REBOOT_DELAY, make_charger, running

CONNECTORS = 2


def _state(csms: CsmsHarness, charger_id: str = "CP_1") -> ChargerState:
    state = csms.registry.get(charger_id)
    assert state is not None, f"{charger_id} not in registry"
    return state


def _is_booted(csms: CsmsHarness, booted_at: datetime | None = None) -> bool:
    """Booted (at `booted_at`, if given) and every connector, 0 included, reported."""
    state = csms.registry.get("CP_1")
    if state is None or state.boot_time is None or len(state.connectors) != CONNECTORS + 1:
        return False
    return booted_at is None or state.boot_time == booted_at


async def _wait_heartbeat_at(csms: CsmsHarness, expected: datetime) -> None:
    await wait_until(lambda: _state(csms).last_heartbeat == expected)


async def _wait_idle(time: ManualTime, charger: VirtualCharger, *, booted: bool) -> None:
    """Wait until the charger is (not) booted and asleep on exactly one timer."""
    await wait_until(lambda: charger.booted == booted and time.sleeper_count == 1)


def _read_log(path: Path) -> list[LogRecord]:
    return [LogRecord.model_validate_json(line) for line in path.read_text().splitlines()]


# --- Boot and heartbeats ---------------------------------------------------------------


async def test_boot_then_status_for_every_connector(
    time: ManualTime, sim_csms: CsmsHarness
) -> None:
    async with running(make_charger(sim_csms.url, time, connectors=CONNECTORS)) as charger:
        await wait_until(lambda: _is_booted(sim_csms))

        state = _state(sim_csms)
        assert (state.vendor, state.model) == ("OcppOps", "VirtualCharger")
        assert {i: c.status for i, c in state.connectors.items()} == {
            0: S.available,
            1: S.available,
            2: S.available,
        }
        # BootNotification.conf's interval became the charger's HeartbeatInterval.
        assert charger.config.heartbeat_interval == HEARTBEAT_INTERVAL


async def test_heartbeats_at_the_interval_from_boot(
    time: ManualTime, sim_csms: CsmsHarness
) -> None:
    async with running(make_charger(sim_csms.url, time)) as charger:
        await wait_until(lambda: _is_booted(sim_csms))
        booted_at = time()
        for n in (1, 2, 3):
            await _wait_idle(time, charger, booted=True)
            assert time.advance_to_next() == HEARTBEAT_INTERVAL
            await _wait_heartbeat_at(
                sim_csms, booted_at + timedelta(seconds=n * HEARTBEAT_INTERVAL)
            )


async def test_charger_side_view_for_the_status_table(
    time: ManualTime, sim_csms: CsmsHarness
) -> None:
    """What the fleet grid shows with an external CSMS: the charger-side view."""
    charger = make_charger(sim_csms.url, time, connectors=CONNECTORS)
    assert charger.connector_ids == [0, 1, 2]
    assert (charger.connected, charger.booted, charger.last_heartbeat) == (False, False, None)
    assert charger.boot_time is None
    async with running(charger):
        await wait_until(lambda: _is_booted(sim_csms))
        await _wait_idle(time, charger, booted=True)
        assert (charger.connected, charger.booted, charger.last_heartbeat) == (True, True, None)
        assert charger.boot_time == time()
        time.advance_to_next()
        await _wait_idle(time, charger, booted=True)
        # Set when the CSMS answered, by the charger's clock.
        assert charger.last_heartbeat == time()
    # Stopped: no longer connected, but "last seen" is kept.
    assert (charger.connected, charger.booted, charger.last_heartbeat) == (False, False, time())


# --- Configuration through the CSMS ----------------------------------------------------


async def test_heartbeat_interval_change_takes_effect_now(
    time: ManualTime, sim_csms: CsmsHarness
) -> None:
    async with running(make_charger(sim_csms.url, time)) as charger:
        await wait_until(lambda: _is_booted(sim_csms))
        await _wait_idle(time, charger, booted=True)

        result = await sim_csms.central_system.change_configuration(
            "CP_1", "HeartbeatInterval", "10"
        )

        assert result.status == ConfigurationStatus.accepted
        await _wait_idle(time, charger, booted=True)  # old heartbeat timer replaced
        changed_at = time()
        assert time.advance_to_next() == 10
        await _wait_heartbeat_at(sim_csms, changed_at + timedelta(seconds=10))


async def test_heartbeat_interval_zero_stops_heartbeats(
    time: ManualTime, sim_csms: CsmsHarness
) -> None:
    async with running(make_charger(sim_csms.url, time)) as charger:
        await wait_until(lambda: _is_booted(sim_csms))
        await _wait_idle(time, charger, booted=True)

        result = await sim_csms.central_system.change_configuration(
            "CP_1", "HeartbeatInterval", "0"
        )
        # A round trip after the change: the charger has fully handled it.
        await sim_csms.central_system.get_configuration("CP_1", ["HeartbeatInterval"])

        assert result.status == ConfigurationStatus.accepted
        assert time.sleeper_count == 0  # no heartbeat timer at all, and no busy loop
        assert _state(sim_csms).last_heartbeat is None


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("NumberOfConnectors", "4", ConfigurationStatus.rejected),
        ("NoSuchKey", "1", ConfigurationStatus.not_supported),
        ("HeartbeatInterval", "abc", ConfigurationStatus.rejected),
    ],
)
async def test_change_configuration_refusals(
    time: ManualTime, sim_csms: CsmsHarness, key: str, value: str, expected: ConfigurationStatus
) -> None:
    async with running(make_charger(sim_csms.url, time)):
        await wait_until(lambda: _is_booted(sim_csms))

        result = await sim_csms.central_system.change_configuration("CP_1", key, value)

        assert result.status == expected


async def test_get_configuration_returns_keys_with_readonly_flags(
    time: ManualTime, sim_csms: CsmsHarness
) -> None:
    async with running(make_charger(sim_csms.url, time)) as charger:
        await wait_until(lambda: _is_booted(sim_csms))

        result = await sim_csms.central_system.get_configuration("CP_1")

        by_key = {item.key: item for item in result.configuration}
        assert set(by_key) == set(charger.config.get()[0])
        assert by_key["NumberOfConnectors"] == ConfigurationKey(
            key="NumberOfConnectors", readonly=True, value=str(CONNECTORS)
        )
        assert by_key["HeartbeatInterval"] == ConfigurationKey(
            key="HeartbeatInterval", readonly=False, value=str(HEARTBEAT_INTERVAL)
        )
        assert result.unknown_keys == []


# --- Reset -----------------------------------------------------------------------------


async def _reset_and_reboot(time: ManualTime, csms: CsmsHarness, charger: VirtualCharger) -> None:
    result = await csms.central_system.reset("CP_1", ResetType.hard)
    assert result.status == ResetStatus.accepted
    await _wait_idle(time, charger, booted=False)  # rebooting
    assert time.advance_to_next() == REBOOT_DELAY
    await wait_until(lambda: _is_booted(csms, booted_at=time()))


async def test_reset_disconnects_reconnects_and_boots_again(
    time: ManualTime, sim_csms: CsmsHarness
) -> None:
    async with running(make_charger(sim_csms.url, time)) as charger:
        await wait_until(lambda: _is_booted(sim_csms))

        await _reset_and_reboot(time, sim_csms, charger)

    records = _read_log(sim_csms.log_path)
    lifecycle = [
        r.message_type if r.direction is None else r.action
        for r in records
        if r.direction is None or (r.message_type == "CALL" and r.action == "BootNotification")
    ]
    assert lifecycle[:5] == [
        "CONNECTED",
        "BootNotification",
        "DISCONNECTED",
        "CONNECTED",
        "BootNotification",
    ]
    first_disconnect = next(r for r in records if r.message_type == "DISCONNECTED")
    assert first_disconnect.payload["reason"] == "clean"  # the charger closed it properly


async def test_config_survives_reset(time: ManualTime, sim_csms: CsmsHarness) -> None:
    async with running(make_charger(sim_csms.url, time)) as charger:
        await wait_until(lambda: _is_booted(sim_csms))
        central_system = sim_csms.central_system
        await central_system.change_configuration("CP_1", "MeterValueSampleInterval", "15")
        await central_system.change_configuration("CP_1", "HeartbeatInterval", "30")

        await _reset_and_reboot(time, sim_csms, charger)

        result = await central_system.get_configuration(
            "CP_1", ["MeterValueSampleInterval", "HeartbeatInterval"]
        )
        values = {item.key: item.value for item in result.configuration}
        # Non-volatile config is kept across the reboot...
        assert values["MeterValueSampleInterval"] == "15"
        # ...but OCPP 1.6 §4.2: BootNotification.conf's interval *is* the heartbeat interval,
        # so the new boot re-imposes the CSMS's value.
        assert values["HeartbeatInterval"] == str(HEARTBEAT_INTERVAL)


# --- Connection loss -------------------------------------------------------------------


async def test_reconnects_with_backoff_after_csms_restart(time: ManualTime, tmp_path: Path) -> None:
    task: asyncio.Task[None] | None = None
    try:
        async with running_csms(time, tmp_path / "first.jsonl") as first:
            charger = make_charger(first.url, time)
            task = asyncio.create_task(charger.run())
            await wait_until(lambda: _is_booted(first))
            port = first.port
        # CSMS gone. First retry after 0.5-1 s; nobody listens, so it fails...
        await _wait_idle(time, charger, booted=False)
        assert 0.5 <= time.advance_to_next() <= 1.0
        # ...and the next retry waits longer (1-2 s).
        await _wait_idle(time, charger, booted=False)
        async with running_csms(time, tmp_path / "second.jsonl", port=port) as second:
            assert 1.0 <= time.advance_to_next() <= 2.0
            await wait_until(lambda: _is_booted(second))
    finally:
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


# --- set_status ------------------------------------------------------------------------


async def test_set_status_is_reported_to_the_csms(time: ManualTime, sim_csms: CsmsHarness) -> None:
    async with running(make_charger(sim_csms.url, time)) as charger:
        await wait_until(lambda: _is_booted(sim_csms))

        await charger.set_status(1, S.preparing)
        assert _state(sim_csms).connectors[1].status == S.preparing

        await charger.set_status(2, S.faulted, E.ground_failure, info="RCD tripped")
        connector = _state(sim_csms).connectors[2]
        assert (connector.status, connector.error_code, connector.info) == (
            S.faulted,
            E.ground_failure,
            "RCD tripped",
        )


async def test_invalid_set_status_raises_and_changes_nothing(
    time: ManualTime, sim_csms: CsmsHarness
) -> None:
    async with running(make_charger(sim_csms.url, time)) as charger:
        await wait_until(lambda: _is_booted(sim_csms))

        with pytest.raises(InvalidTransitionError):
            await charger.set_status(1, S.finishing)  # Available -> Finishing is not allowed
        with pytest.raises(ValueError, match="no connector 9"):
            await charger.set_status(9, S.unavailable)

        assert charger.connector(1).status == S.available
        assert _state(sim_csms).connectors[1].status == S.available


async def test_status_changed_offline_is_reported_after_reconnect(
    time: ManualTime, tmp_path: Path
) -> None:
    task: asyncio.Task[None] | None = None
    try:
        async with running_csms(time, tmp_path / "first.jsonl") as first:
            charger = make_charger(first.url, time)
            task = asyncio.create_task(charger.run())
            await wait_until(lambda: _is_booted(first))
            port = first.port
        await _wait_idle(time, charger, booted=False)

        await charger.set_status(1, S.faulted, E.ground_failure)  # offline: stored only

        assert charger.connector(1).status == S.faulted
        async with running_csms(time, tmp_path / "second.jsonl", port=port) as second:
            time.advance_to_next()
            await wait_until(lambda: _is_booted(second))
            connector = _state(second).connectors[1]
            assert (connector.status, connector.error_code) == (S.faulted, E.ground_failure)
    finally:
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


# --- Boot Pending ----------------------------------------------------------------------


class PendingOnceCsms(ChargePoint):
    """Tiny CSMS that answers Pending to the first BootNotification, Accepted afterwards."""

    def __init__(self, charger_id: str, connection: ServerConnection) -> None:
        super().__init__(charger_id, connection)
        self.boots = 0
        self.statuses = 0

    @on(Action.boot_notification)
    def on_boot_notification(self, **_: Any) -> call_result.BootNotification:
        self.boots += 1
        status = RegistrationStatus.pending if self.boots == 1 else RegistrationStatus.accepted
        return call_result.BootNotification(
            current_time="2026-11-24T12:00:00+00:00", interval=30, status=status
        )

    @on(Action.status_notification)
    def on_status_notification(self, **_: Any) -> call_result.StatusNotification:
        self.statuses += 1
        return call_result.StatusNotification()

    @on(Action.heartbeat)
    def on_heartbeat(self) -> call_result.Heartbeat:
        return call_result.Heartbeat(current_time="2026-11-24T12:00:00+00:00")


async def test_boot_pending_is_retried_after_interval(time: ManualTime) -> None:
    sessions: list[PendingOnceCsms] = []

    async def handler(connection: ServerConnection) -> None:
        csms_side = PendingOnceCsms("csms", connection)
        sessions.append(csms_side)
        with suppress(ConnectionClosed):
            await csms_side.start()

    async with serve(handler, "127.0.0.1", 0, subprotocols=[OCPP16]) as server:
        port = server.sockets[0].getsockname()[1]
        async with running(make_charger(f"ws://127.0.0.1:{port}", time)) as charger:
            await wait_until(lambda: len(sessions) == 1 and sessions[0].boots == 1)
            await _wait_idle(time, charger, booted=False)
            # §4.2: while Pending, the charger sends nothing else.
            assert sessions[0].statuses == 0

            assert time.advance_to_next() == 30  # the interval from the Pending answer
            await wait_until(lambda: charger.booted and sessions[0].statuses == CONNECTORS + 1)

            assert sessions[0].boots == 2
            assert len(sessions) == 1  # retried on the same connection


# --- Reproducibility -------------------------------------------------------------------


async def _scenario(seed: int, run_dir: Path) -> tuple[list[tuple[Any, ...]], list[float]]:
    """Boot, two heartbeats, a status change, CSMS restart, reconnect. Returns the charger's
    own log and every sleep it asked for."""
    time = ManualTime()
    task: asyncio.Task[None] | None = None
    with MessageLog(run_dir / "charger.jsonl") as log:
        try:
            async with running_csms(time, run_dir / "first.jsonl") as first:
                charger = make_charger(first.url, time, seed=seed, log=log)
                task = asyncio.create_task(charger.run())
                await wait_until(lambda: _is_booted(first))
                for _ in range(2):
                    await _wait_idle(time, charger, booted=True)
                    time.advance_to_next()
                    await wait_until(lambda: _state(first).last_heartbeat == time())
                await charger.set_status(1, S.preparing)
                port = first.port
            await _wait_idle(time, charger, booted=False)
            async with running_csms(time, run_dir / "second.jsonl", port=port) as second:
                time.advance_to_next()
                await wait_until(lambda: _is_booted(second))
        finally:
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
    records = [
        (r.timestamp, r.direction, r.message_type, r.message_id, r.action, r.payload)
        for r in _read_log(run_dir / "charger.jsonl")
    ]
    return records, time.requested


async def test_same_seed_same_run(tmp_path: Path) -> None:
    log_a, sleeps_a = await _scenario(7, tmp_path / "a")
    log_b, sleeps_b = await _scenario(7, tmp_path / "b")
    log_c, sleeps_c = await _scenario(8, tmp_path / "c")

    assert len(log_a) > 10
    assert log_a == log_b  # every field, message ids and timestamps included
    assert sleeps_a == sleeps_b
    # A different seed changes the message ids and the reconnect jitter.
    assert [r[3] for r in log_a] != [r[3] for r in log_c]
    assert sleeps_a != sleeps_c
