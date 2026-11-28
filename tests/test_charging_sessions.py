"""Charging sessions on a virtual charger against a real CSMS, under virtual time (ManualTime).

Steps and timer counts are in session_helpers.
"""

import asyncio
from contextlib import AsyncExitStack
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from ocpp.v16.enums import AuthorizationStatus, Reason
from ocpp.v16.enums import ChargePointErrorCode as E
from ocpp.v16.enums import ChargePointStatus as S

from fakes import (
    ACCEPTED_TAG,
    BLOCKED_TAG,
    EXPIRED_TAG,
    HEARTBEAT_INTERVAL,
    CsmsHarness,
    hold_validation,
    running_csms,
    wait_until,
)
from manual_time import ManualTime
from ocpp_ops.charger.charger import SessionError, VirtualCharger
from ocpp_ops.csms.server import start_server
from ocpp_ops.message_log import LogRecord, MessageLog
from session_helpers import (
    CHARGING,
    IDLE,
    POWER_W,
    SAMPLE_INTERVAL,
    WH_PER_SAMPLE,
    active_transaction,
    advance_to,
    boot,
    calls,
    charger_state,
    settle,
    start_session,
)
from sim import drop_connection, make_charger, read_log, running


def _sample(record: LogRecord) -> dict[str, str]:
    """One MeterValues CALL as {measurand: value}, checking the fields we always send."""
    [meter_value] = record.payload["meterValue"]
    sampled = meter_value["sampledValue"]
    assert {s["context"] for s in sampled} == {"Sample.Periodic"}
    assert {(s["measurand"], s["unit"]) for s in sampled} == {
        ("Energy.Active.Import.Register", "Wh"),
        ("Power.Active.Import", "W"),
    }
    return {s["measurand"]: s["value"] for s in sampled}


def _energy_values(records: list[LogRecord]) -> list[int]:
    return [int(_sample(r)["Energy.Active.Import.Register"]) for r in calls(records, "MeterValues")]


# --- A full session --------------------------------------------------------------------


async def test_full_session(time: ManualTime, sim_csms: CsmsHarness) -> None:
    charger = make_charger(sim_csms.url, time, max_power_w=POWER_W)
    async with running(charger, csms=sim_csms):
        await boot(time, sim_csms, charger)

        await charger.plug_in(1)
        assert charger_state(sim_csms).connectors[1].status == S.preparing

        assert await charger.authorize(1, ACCEPTED_TAG) == AuthorizationStatus.accepted
        started_at = time()
        transaction = await charger.start(1)
        await settle(time, charger, CHARGING)

        assert transaction.transaction_id == 1  # from StartTransaction.conf
        assert charger_state(sim_csms).connectors[1].status == S.charging
        active = active_transaction(sim_csms)
        assert (active.id, active.id_tag, active.meter_start) == (1, ACCEPTED_TAG, 0)
        assert active.started_at == started_at

        # Two samples, at the interval from the start (a heartbeat comes in between).
        await advance_to(
            time,
            started_at + timedelta(seconds=2 * SAMPLE_INTERVAL),
            lambda: settle(time, charger, CHARGING),
        )
        samples = calls(read_log(sim_csms.log_path), "MeterValues")
        assert [r.timestamp for r in samples] == [
            started_at + timedelta(seconds=SAMPLE_INTERVAL),
            started_at + timedelta(seconds=2 * SAMPLE_INTERVAL),
        ]
        assert {r.payload["transactionId"] for r in samples} == {1}
        assert [_sample(r) for r in samples] == [
            {"Energy.Active.Import.Register": "120", "Power.Active.Import": "7200"},
            {"Energy.Active.Import.Register": "240", "Power.Active.Import": "7200"},
        ]
        assert active_transaction(sim_csms).energy_wh == 240

        stopped_at = time()
        await charger.stop(1)
        await settle(time, charger, IDLE)  # sampling stopped

        state = charger_state(sim_csms)
        assert state.connectors[1].status == S.finishing
        assert state.active_transactions == {}
        [finished] = state.finished_transactions
        assert (finished.id, finished.meter_stop, finished.energy_wh) == (1, 240, 240)
        assert finished.stopped_at == stopped_at
        assert (finished.duration_s, finished.stop_reason) == (120, Reason.local)

        await charger.unplug(1)
        assert charger_state(sim_csms).connectors[1].status == S.available
        assert charger.transaction(1) is None

    # The whole session as the CSMS logged it, in order (after the 3 boot statuses): each
    # status before its transaction message, as our charger sends them.
    session = [
        (r.action, r.payload.get("status"))
        for r in calls(read_log(sim_csms.log_path))
        if r.action not in ("BootNotification", "Heartbeat")
    ][3:]
    assert session == [
        ("StatusNotification", "Preparing"),
        ("Authorize", None),
        ("StatusNotification", "Charging"),
        ("StartTransaction", None),
        ("MeterValues", None),
        ("MeterValues", None),
        ("StatusNotification", "Finishing"),
        ("StopTransaction", None),
        ("StatusNotification", "Available"),
    ]
    stop_call = calls(read_log(sim_csms.log_path), "StopTransaction")[0]
    assert stop_call.payload == {
        "meterStop": 240,
        "timestamp": stopped_at.isoformat(),
        "transactionId": 1,
        "reason": "Local",
        "idTag": ACCEPTED_TAG,  # the driver who stopped it
    }


async def test_ev_unplugged_while_charging(time: ManualTime, sim_csms: CsmsHarness) -> None:
    charger = make_charger(sim_csms.url, time, max_power_w=POWER_W)
    async with running(charger, csms=sim_csms):
        await boot(time, sim_csms, charger)
        await start_session(time, charger)
        started_at = time()
        await advance_to(
            time,
            started_at + timedelta(seconds=SAMPLE_INTERVAL),
            lambda: settle(time, charger, CHARGING),
        )

        await charger.unplug(1)
        await settle(time, charger, IDLE)

        state = charger_state(sim_csms)
        assert state.connectors[1].status == S.available
        [finished] = state.finished_transactions
        assert (finished.stop_reason, finished.meter_stop) == (Reason.ev_disconnected, 120)

    records = read_log(sim_csms.log_path)
    [stop_call] = calls(records, "StopTransaction")
    assert stop_call.payload["reason"] == "EVDisconnected"
    assert "idTag" not in stop_call.payload  # nobody presented a tag to stop it
    # §4.9: Finishing with info "EV side disconnected", then Available.
    statuses = [
        (r.payload["status"], r.payload.get("info"))
        for r in calls(records, "StatusNotification")
        if r.payload["connectorId"] == 1
    ]
    assert statuses[-2:] == [("Finishing", "EV side disconnected"), ("Available", None)]


@pytest.mark.parametrize(
    ("id_tag", "expected"),
    [
        (BLOCKED_TAG, AuthorizationStatus.blocked),
        (EXPIRED_TAG, AuthorizationStatus.expired),
        ("NOBODY", AuthorizationStatus.invalid),
    ],
)
async def test_refused_tag_starts_nothing(
    time: ManualTime, sim_csms: CsmsHarness, id_tag: str, expected: AuthorizationStatus
) -> None:
    charger = make_charger(sim_csms.url, time)
    async with running(charger, csms=sim_csms):
        await boot(time, sim_csms, charger)
        await charger.plug_in(1)

        assert await charger.authorize(1, id_tag) == expected

        assert charger_state(sim_csms).connectors[1].status == S.available
        with pytest.raises(SessionError, match="is Available"):
            await charger.start(1)

    assert charger_state(sim_csms).active_transactions == {}
    assert calls(read_log(sim_csms.log_path), "StartTransaction") == []


async def test_session_actions_check_the_connector_state(
    time: ManualTime, sim_csms: CsmsHarness
) -> None:
    charger = make_charger(sim_csms.url, time)
    async with running(charger, csms=sim_csms):
        await boot(time, sim_csms, charger)

        with pytest.raises(SessionError, match="is Available"):
            await charger.authorize(1, ACCEPTED_TAG)  # nothing plugged in
        with pytest.raises(SessionError, match="is Available"):
            await charger.unplug(1)
        await charger.plug_in(1)
        with pytest.raises(SessionError, match="no accepted idTag"):
            await charger.start(1)
        with pytest.raises(SessionError, match="no transaction"):
            await charger.stop(1)
        with pytest.raises(ValueError, match="only implements reason Local"):
            await charger.stop(1, Reason.remote)
        with pytest.raises(ValueError, match="no connector 0"):
            await charger.plug_in(0)  # connector 0 is the charge point, not an outlet


# --- Wire order: a status never overtakes its connector's transaction messages ----------


@pytest.mark.parametrize("slow", [None, "StopTransaction", "StatusNotification"])
async def test_stop_then_unplug_order_on_the_wire(
    time: ManualTime, sim_csms: CsmsHarness, monkeypatch: pytest.MonkeyPatch, slow: str | None
) -> None:
    """stop() then unplug() at once: Finishing, StopTransaction, Available, as a real charger
    sends them, whatever validation is slower (ocpp validates in a thread)."""
    charger = make_charger(sim_csms.url, time, max_power_w=POWER_W)
    async with running(charger, csms=sim_csms):
        await boot(time, sim_csms, charger)
        await start_session(time, charger)
        if slow is not None:
            hold_validation(monkeypatch, lambda c: c.action == slow, lambda: asyncio.sleep(0.05))

        await charger.stop(1)
        await charger.unplug(1)
        await settle(time, charger, IDLE)

    tail = [
        (r.action, r.payload.get("status"))
        for r in calls(read_log(sim_csms.log_path), "StatusNotification", "StopTransaction")
    ][-3:]
    assert tail == [
        ("StatusNotification", "Finishing"),
        ("StopTransaction", None),
        ("StatusNotification", "Available"),
    ]


# --- Energy ----------------------------------------------------------------------------


async def test_meter_keeps_counting_across_sessions(
    time: ManualTime, sim_csms: CsmsHarness
) -> None:
    charger = make_charger(sim_csms.url, time, max_power_w=POWER_W)
    async with running(charger, csms=sim_csms):
        await boot(time, sim_csms, charger)
        await start_session(time, charger)
        await advance_to(
            time, time() + timedelta(seconds=90), lambda: settle(time, charger, CHARGING)
        )
        await charger.stop(1)  # after 84 s (two heartbeats, one sample at 60 s): 168 Wh
        await charger.unplug(1)
        await settle(time, charger, IDLE)
        # Idle for a while: no power, no energy.
        await advance_to(time, time() + timedelta(seconds=200), lambda: settle(time, charger, 1))
        assert charger.meter_wh(1) == 168

        await start_session(time, charger)

        second = active_transaction(sim_csms)
        # §7.31: the register is not reset at the start of a transaction.
        assert second.meter_start == 168
        assert charger.meter_wh(2) == 0  # each connector has its own meter


async def test_two_connectors_charge_at_once(time: ManualTime, sim_csms: CsmsHarness) -> None:
    charger = make_charger(sim_csms.url, time, max_power_w=POWER_W)
    async with running(charger, csms=sim_csms):
        await boot(time, sim_csms, charger)
        await start_session(time, charger, connector_id=1, sleepers=2)
        await start_session(time, charger, connector_id=2, sleepers=3)
        start = time()
        await advance_to(
            time,
            start + timedelta(seconds=SAMPLE_INTERVAL),
            lambda: settle(time, charger, 3),
        )

        # No load sharing: each connector draws the full power.
        first, second = active_transaction(sim_csms, 1), active_transaction(sim_csms, 2)
        assert (first.energy_wh, second.energy_wh) == (120, 120)
        assert {first.id, second.id} == {1, 2}


# --- MeterValueSampleInterval ----------------------------------------------------------


async def test_sample_interval_change_applies_at_once(
    time: ManualTime, sim_csms: CsmsHarness
) -> None:
    charger = make_charger(sim_csms.url, time, max_power_w=POWER_W)
    async with running(charger, csms=sim_csms):
        await boot(time, sim_csms, charger)
        await start_session(time, charger)
        started_at = time()
        time.advance_to_next()  # the first heartbeat, at 42 s
        await settle(time, charger, CHARGING)
        changed_at = time()
        assert changed_at == started_at + timedelta(seconds=HEARTBEAT_INTERVAL)

        result = await sim_csms.central_system.change_configuration(
            "CP_1", "MeterValueSampleInterval", "15"
        )
        assert result.status == "Accepted"
        await settle(time, charger, CHARGING)  # the 60 s timer was replaced

        await advance_to(
            time, changed_at + timedelta(seconds=30), lambda: settle(time, charger, CHARGING)
        )
        samples = calls(read_log(sim_csms.log_path), "MeterValues")
        # Counted from the change, not from the start (which would give 45 s, 60 s).
        assert [r.timestamp for r in samples] == [
            changed_at + timedelta(seconds=15),
            changed_at + timedelta(seconds=30),
        ]

        await sim_csms.central_system.change_configuration("CP_1", "MeterValueSampleInterval", "0")
        await settle(time, charger, IDLE)  # no sampling timer at all, only the heartbeat
        await advance_to(time, time() + timedelta(seconds=300), lambda: settle(time, charger, IDLE))
        assert len(calls(read_log(sim_csms.log_path), "MeterValues")) == 2


async def test_sample_interval_zero_sends_no_meter_values(
    time: ManualTime, sim_csms: CsmsHarness
) -> None:
    charger = make_charger(sim_csms.url, time, max_power_w=POWER_W)
    charger.config.change("MeterValueSampleInterval", "0")
    async with running(charger, csms=sim_csms):
        await boot(time, sim_csms, charger)
        await start_session(time, charger, sleepers=IDLE)
        await advance_to(time, time() + timedelta(seconds=300), lambda: settle(time, charger, IDLE))
        await charger.stop(1)
        await settle(time, charger, IDLE)

    assert calls(read_log(sim_csms.log_path), "MeterValues") == []
    # The transaction itself is still complete: the CSMS learns the energy at the stop.
    [finished] = charger_state(sim_csms).finished_transactions
    assert finished.energy_wh == 294 * 2  # the last heartbeat was at 294 s, 2 Wh/s


# --- Connection lost mid-session -------------------------------------------------------


async def _restart_listening(stack: AsyncExitStack, csms: CsmsHarness) -> None:
    """The CSMS accepts connections again, with the same state and log. The server stays up
    until `stack` closes, after running() has checked the charger's clean close."""
    server = await start_server(csms.central_system, "127.0.0.1", csms.port)
    await stack.enter_async_context(server)


async def _offline_settle(time: ManualTime, charger: VirtualCharger, sleepers: int) -> None:
    """Offline: the reconnect backoff timer plus `sleepers - 1` others."""
    await wait_until(lambda: not charger.connected and time.sleeper_count == sleepers)


async def test_messages_queued_offline_are_delivered_in_order_after_reconnect(
    time: ManualTime, sim_csms: CsmsHarness
) -> None:
    charger = make_charger(sim_csms.url, time, max_power_w=POWER_W)
    async with AsyncExitStack() as restarted, running(charger, csms=sim_csms):
        await boot(time, sim_csms, charger)
        await start_session(time, charger)
        started_at = time()
        await advance_to(
            time,
            started_at + timedelta(seconds=SAMPLE_INTERVAL),
            lambda: settle(time, charger, CHARGING),
        )

        # The CSMS stops accepting connections (it keeps its state), then the network drops.
        sim_csms.server.close(close_connections=False)
        drop_connection(charger)
        await _offline_settle(time, charger, CHARGING)  # sampling + reconnect backoff

        # Offline: reconnect attempts fail, samples go on and are queued.
        await advance_to(
            time,
            started_at + timedelta(seconds=3 * SAMPLE_INTERVAL),
            lambda: _offline_settle(time, charger, CHARGING),
        )
        stopped_at = time()
        await charger.stop(1)
        await _offline_settle(time, charger, 1)  # only the backoff timer left
        assert charger.queued_messages == 3  # MeterValues at 120 s and 180 s, StopTransaction
        assert active_transaction(sim_csms).id == 1  # the CSMS doesn't know yet

        await _restart_listening(restarted, sim_csms)
        time.advance_to_next()  # reconnect
        await wait_until(lambda: charger.booted)
        await settle(time, charger, IDLE)

        state = charger_state(sim_csms)
        assert state.active_transactions == {}
        [finished] = state.finished_transactions
        expected_wh = int((stopped_at - started_at).total_seconds()) * 2
        assert (finished.meter_stop, finished.energy_wh) == (expected_wh, expected_wh)
        # From the charger's timestamps: the real duration, not the delivery time.
        assert finished.duration_s == (stopped_at - started_at).total_seconds()
        assert state.connectors[1].status == S.finishing
        assert state.anomalies == []
        await charger.unplug(1)

    records = read_log(sim_csms.log_path)
    reconnected_at = max(r.timestamp for r in records if r.message_type == "CONNECTED")
    after_reconnect = [
        (r.action, r.payload.get("status"), r.payload.get("connectorId"))
        for r in calls(records)
        if r.timestamp >= reconnected_at and r.action != "Heartbeat"
    ]
    # §4.2 then §3.7: boot, every status, then the queue in order.
    assert after_reconnect == [
        ("BootNotification", None, None),
        ("StatusNotification", "Available", 0),
        ("StatusNotification", "Finishing", 1),
        ("StatusNotification", "Available", 2),
        ("MeterValues", None, 1),
        ("MeterValues", None, 1),
        ("StopTransaction", None, None),
        ("StatusNotification", "Available", 1),
    ]
    # Delivered late, but with the time they were taken.
    samples = calls(records, "MeterValues")
    assert [r.payload["meterValue"][0]["timestamp"] for r in samples] == [
        (started_at + timedelta(seconds=n * SAMPLE_INTERVAL)).isoformat() for n in (1, 2, 3)
    ]
    # The register only grows, across the outage too.
    assert _energy_values(records) == [120, 240, 360]
    assert [r.payload["transactionId"] for r in samples] == [1, 1, 1]
    disconnects = [r for r in records if r.message_type == "DISCONNECTED"]
    assert disconnects[0].payload["reason"] == "network_drop"


async def test_start_while_offline_gets_its_id_after_reconnect(
    time: ManualTime, sim_csms: CsmsHarness
) -> None:
    charger = make_charger(sim_csms.url, time, max_power_w=POWER_W)
    async with AsyncExitStack() as restarted, running(charger, csms=sim_csms):
        await boot(time, sim_csms, charger)
        await charger.plug_in(1)
        assert await charger.authorize(1, ACCEPTED_TAG) == AuthorizationStatus.accepted

        sim_csms.server.close(close_connections=False)
        drop_connection(charger)
        await _offline_settle(time, charger, 1)

        started_at = time()
        transaction = await charger.start(1)  # offline: StartTransaction is queued
        await _offline_settle(time, charger, CHARGING)
        await advance_to(
            time,
            started_at + timedelta(seconds=SAMPLE_INTERVAL),
            lambda: _offline_settle(time, charger, CHARGING),
        )
        assert charger.queued_messages == 2  # StartTransaction, then MeterValues
        assert transaction.transaction_id is None  # no answer yet

        await _restart_listening(restarted, sim_csms)
        # Wake timers (sampling or backoff) until the backoff one has reconnected.
        while not charger.booted:
            time.advance_to_next()
            await wait_until(lambda: charger.booted or time.sleeper_count == CHARGING)
        await settle(time, charger, CHARGING)

        assert transaction.transaction_id == 1
        active = active_transaction(sim_csms)
        assert (active.id, active.started_at) == (1, started_at)
        assert charger_state(sim_csms).anomalies == []
        # The MeterValues queued before the id was known went out with it.
        records = read_log(sim_csms.log_path)
        assert {r.payload["transactionId"] for r in calls(records, "MeterValues")} == {1}
        assert active.energy_wh == _energy_values(records)[-1]
        assert _energy_values(records)[0] == WH_PER_SAMPLE


# --- Failures to process: TransactionMessageAttempts / RetryInterval (§3.7.1) -----------


def _fail(monkeypatch: pytest.MonkeyPatch, csms: CsmsHarness, method: str, times: int) -> None:
    """The CSMS fails to process the next `times` calls of a registry method (e.g.
    "start_transaction"): ocpp answers an exception in a handler with a CALLERROR."""
    original = getattr(csms.registry, method)
    remaining = times

    def flaky(*args: Any, **kwargs: Any) -> int:
        nonlocal remaining
        if remaining > 0:
            remaining -= 1
            raise RuntimeError("simulated CSMS failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(csms.registry, method, flaky)


async def _quiet_charger(time: ManualTime, csms: CsmsHarness, charger: VirtualCharger) -> None:
    """Boot, then no heartbeats and no sampling: only retry timers are left to watch."""
    await boot(time, csms, charger)
    await csms.central_system.change_configuration("CP_1", "HeartbeatInterval", "0")
    await wait_until(lambda: time.sleeper_count == 0)


async def test_failed_start_is_retried_with_growing_waits(
    time: ManualTime, sim_csms: CsmsHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fail(monkeypatch, sim_csms, "start_transaction", times=2)
    charger = make_charger(sim_csms.url, time)
    charger.config.change("MeterValueSampleInterval", "0")
    async with running(charger, csms=sim_csms):
        await _quiet_charger(time, sim_csms, charger)
        started_at = time()
        await charger.plug_in(1)
        await charger.authorize(1, ACCEPTED_TAG)
        transaction = await charger.start(1)

        # RetryInterval (60 s) x transmissions so far: 60 s, then 120 s.
        await wait_until(lambda: time.sleeper_count == 1)
        assert time.advance_to_next() == 60
        await wait_until(lambda: time.sleeper_count == 1)
        assert time.advance_to_next() == 120
        await settle(time, charger, 0)

        assert transaction.transaction_id == 1
        assert active_transaction(sim_csms).started_at == started_at  # the original start time

    records = read_log(sim_csms.log_path)
    attempts = calls(records, "StartTransaction")
    assert [r.timestamp - started_at for r in attempts] == [
        timedelta(seconds=0),
        timedelta(seconds=60),
        timedelta(seconds=180),
    ]
    errors = [r for r in records if r.message_type == "CALLERROR"]
    assert [r.payload["errorCode"] for r in errors] == ["InternalError"] * 2


async def test_start_given_up_after_attempts_then_minus_one(
    time: ManualTime, sim_csms: CsmsHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fail(monkeypatch, sim_csms, "start_transaction", times=99)
    charger = make_charger(sim_csms.url, time)
    charger.config.change("MeterValueSampleInterval", "0")
    charger.config.change("TransactionMessageAttempts", "2")
    async with running(charger, csms=sim_csms):
        await _quiet_charger(time, sim_csms, charger)
        await charger.plug_in(1)
        await charger.authorize(1, ACCEPTED_TAG)
        transaction = await charger.start(1)
        await wait_until(lambda: time.sleeper_count == 1)
        assert time.advance_to_next() == 60
        await settle(time, charger, 0)  # second failure: discarded, no more waiting

        # Errata 3.18: no id will ever come; later messages say -1.
        assert transaction.transaction_id == -1
        await charger.stop(1)
        await settle(time, charger, 0)

    records = read_log(sim_csms.log_path)
    assert len(calls(records, "StartTransaction")) == 2
    [stop_call] = calls(records, "StopTransaction")
    assert stop_call.payload["transactionId"] == -1
    # Answered anyway, and visible to the agent.
    [anomaly] = charger_state(sim_csms).anomalies
    assert (anomaly.action, anomaly.transaction_id) == ("StopTransaction", -1)


# --- Statuses and queued transaction messages ------------------------------------------


def _status_and_stop_calls(records: list[LogRecord], since: datetime) -> list[tuple[Any, ...]]:
    return [
        (r.action, r.payload.get("connectorId"), r.payload.get("status"))
        if r.action == "StatusNotification"
        else (r.action, r.payload["transactionId"])
        for r in calls(records, "StatusNotification", "StopTransaction")
        if r.timestamp >= since
    ]


async def test_status_does_not_wait_through_a_retry_backoff(
    time: ManualTime, sim_csms: CsmsHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """unplug() right after stop(): Available waits for the StopTransaction's first attempt
    only. That attempt fails (CALLERROR), so Available goes out at once, not after the
    60 s retry backoff."""
    _fail(monkeypatch, sim_csms, "stop_transaction", times=1)
    charger = make_charger(sim_csms.url, time)
    charger.config.change("MeterValueSampleInterval", "0")
    async with running(charger, csms=sim_csms):
        await _quiet_charger(time, sim_csms, charger)
        await start_session(time, charger, sleepers=0)
        stopped_at = time()

        await charger.stop(1)
        async with asyncio.timeout(2):  # fail, not hang, if Available waits for the retry
            await charger.unplug(1)

        assert time() == stopped_at  # no virtual time passed: Available did not wait 60 s
        assert charger.queued_messages == 1  # the StopTransaction, backing off
        assert charger_state(sim_csms).connectors[1].status == S.available
        assert time.advance_to_next() == 60
        await settle(time, charger, 0)

    # The last four: virtual time never moved, so timestamps can't tell boot from stop.
    assert _status_and_stop_calls(read_log(sim_csms.log_path), stopped_at)[-4:] == [
        ("StatusNotification", 1, "Finishing"),
        ("StopTransaction", 1),  # CALLERROR
        ("StatusNotification", 1, "Available"),
        ("StopTransaction", 1),  # the retry, 60 s later
    ]


async def test_status_behind_another_connectors_retry_does_not_wait(
    time: ManualTime, sim_csms: CsmsHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Connector 2's StopTransaction fails and backs off for 60 s; connector 1's waits
    behind it, never attempted. unplug(1) still sends Available at once: a status never
    sits behind a retry backoff, whichever connector it belongs to."""
    _fail(monkeypatch, sim_csms, "stop_transaction", times=1)
    charger = make_charger(sim_csms.url, time)
    charger.config.change("MeterValueSampleInterval", "0")
    async with running(charger, csms=sim_csms):
        await _quiet_charger(time, sim_csms, charger)
        await start_session(time, charger, connector_id=1, sleepers=0)
        await start_session(time, charger, connector_id=2, sleepers=0)
        await charger.stop(2)
        await wait_until(lambda: time.sleeper_count == 1)  # its StopTransaction backs off
        await charger.stop(1)
        stopped_at = time()

        async with asyncio.timeout(2):  # fail, not hang, if Available waits for the retry
            await charger.unplug(1)

        assert time() == stopped_at
        assert charger.queued_messages == 2  # both StopTransactions still queued
        assert time.advance_to_next() == 60
        await settle(time, charger, 0)

    assert _status_and_stop_calls(read_log(sim_csms.log_path), stopped_at)[-6:] == [
        ("StatusNotification", 2, "Finishing"),
        ("StopTransaction", 2),  # CALLERROR, then 60 s backoff
        ("StatusNotification", 1, "Finishing"),
        ("StatusNotification", 1, "Available"),
        ("StopTransaction", 2),
        ("StopTransaction", 1),
    ]


@pytest.mark.parametrize("fault", [True, False], ids=["Faulted", "unplug"])
async def test_a_fault_does_not_wait_for_a_pending_stop(
    time: ManualTime, sim_csms: CsmsHarness, monkeypatch: pytest.MonkeyPatch, fault: bool
) -> None:
    """Both transactions are stopped offline. After the reconnect, connector 2's
    StopTransaction is being sent (held in validation) and connector 1's waits behind it.
    A fault on connector 1 goes out before connector 1's StopTransaction; an ordinary status
    (unplug: Available) goes out after it."""
    charger = make_charger(sim_csms.url, time)
    charger.config.change("MeterValueSampleInterval", "0")
    async with AsyncExitStack() as restarted, running(charger, csms=sim_csms):
        await boot(time, sim_csms, charger)
        await start_session(time, charger, connector_id=1, sleepers=IDLE)
        await start_session(time, charger, connector_id=2, sleepers=IDLE)
        sim_csms.server.close(close_connections=False)
        drop_connection(charger)
        await _offline_settle(time, charger, 1)
        await charger.stop(2)
        await charger.stop(1)
        assert charger.queued_messages == 2  # StopTransaction 2, then StopTransaction 1

        held, release = asyncio.Event(), asyncio.Event()

        async def hold() -> None:
            held.set()
            await release.wait()

        hold_validation(
            monkeypatch,
            lambda c: c.action == "StopTransaction" and c.payload["transactionId"] == 2,
            hold,
        )
        await _restart_listening(restarted, sim_csms)
        reconnect_at = time() + timedelta(seconds=time.advance_to_next())
        await wait_until(held.is_set)  # StopTransaction 2 is on its way, 1 is behind it

        if fault:
            change = asyncio.create_task(charger.set_status(1, S.faulted, E.ground_failure))
            target = S.faulted
        else:
            change = asyncio.create_task(charger.unplug(1))
            target = S.available
        # Stored before any wait: once visible, the change is waiting (queue or send lock).
        await wait_until(lambda: charger.connector(1).status == target)
        release.set()
        await change
        await settle(time, charger, IDLE)

    after_reconnect = _status_and_stop_calls(read_log(sim_csms.log_path), reconnect_at)[3:]
    if fault:
        assert after_reconnect == [
            ("StopTransaction", 2),
            ("StatusNotification", 1, "Faulted"),
            ("StopTransaction", 1),
        ]
    else:
        assert after_reconnect == [
            ("StopTransaction", 2),
            ("StopTransaction", 1),
            ("StatusNotification", 1, "Available"),
        ]


async def test_status_waiting_when_the_connection_drops(
    time: ManualTime, sim_csms: CsmsHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """unplug() waits for the StopTransaction, whose sending is held; the connection drops.
    The wait ends at once without sending (the new status is stored); after the reconnect,
    the boot reports Available, then the StopTransaction is delivered."""
    charger = make_charger(sim_csms.url, time)
    charger.config.change("MeterValueSampleInterval", "0")
    async with running(charger, csms=sim_csms):
        await boot(time, sim_csms, charger)
        await start_session(time, charger, sleepers=IDLE)
        held, release = asyncio.Event(), asyncio.Event()

        async def hold() -> None:
            held.set()
            await release.wait()

        hold_validation(monkeypatch, lambda c: c.action == "StopTransaction", hold)
        await charger.stop(1)
        await wait_until(held.is_set)
        unplug = asyncio.create_task(charger.unplug(1))
        await wait_until(lambda: charger.connector(1).status == S.available)  # now waiting

        drop_connection(charger)
        await _offline_settle(time, charger, 1)
        await wait_until(unplug.done)  # the session's end released it, before any reconnect
        await unplug
        assert charger.queued_messages == 1  # the StopTransaction was never sent

        release.set()
        reconnect_at = time() + timedelta(seconds=time.advance_to_next())
        await wait_until(lambda: charger.booted)
        await settle(time, charger, IDLE)

    assert _status_and_stop_calls(read_log(sim_csms.log_path), reconnect_at) == [
        ("StatusNotification", 0, "Available"),
        ("StatusNotification", 1, "Available"),  # reported by the boot
        ("StatusNotification", 2, "Available"),
        ("StopTransaction", 1),
    ]
    assert charger_state(sim_csms).finished_transactions[0].stop_reason == Reason.local


async def test_set_status_from_the_sender_task_is_refused(
    time: ManualTime, sim_csms: CsmsHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sender waiting for its own deliveries would deadlock: refused loudly instead.
    Here the sender's retry-backoff sleep tries a status change."""
    _fail(monkeypatch, sim_csms, "stop_transaction", times=1)
    errors: list[str] = []
    armed = False

    async def sleep(seconds: float) -> None:
        nonlocal armed
        if armed:  # the only sleeper after stop(): the sender, about to back off
            armed = False
            try:
                await charger.set_status(2, S.unavailable)
            except RuntimeError as error:
                errors.append(str(error))
        await time.sleep(seconds)

    charger = make_charger(sim_csms.url, time, sleep=sleep)
    charger.config.change("MeterValueSampleInterval", "0")
    async with running(charger, csms=sim_csms):
        await _quiet_charger(time, sim_csms, charger)
        await start_session(time, charger, sleepers=0)
        armed = True
        await charger.stop(1)
        await wait_until(lambda: bool(errors))

    assert errors == ["set_status() must not be called from the transaction sender"]
    assert charger.connector(2).status == S.available  # nothing changed


# --- Reproducibility -------------------------------------------------------------------


async def _scenario(seed: int, run_dir: Path) -> tuple[list[tuple[Any, ...]], ...]:
    """Two sessions, one ended by the driver and one by the EV, with a connection drop.
    Returns the charger-side and CSMS-side logs."""
    time = ManualTime()
    with MessageLog(run_dir / "charger.jsonl") as log:
        async with running_csms(time, run_dir / "csms.jsonl") as csms:
            charger = make_charger(csms.url, time, seed=seed, log=log, max_power_w=POWER_W)
            async with running(charger, csms=csms):
                await boot(time, csms, charger)
                await start_session(time, charger)
                await advance_to(
                    time, time() + timedelta(seconds=130), lambda: settle(time, charger, CHARGING)
                )
                await charger.stop(1)
                await charger.unplug(1)
                await settle(time, charger, IDLE)

                await start_session(time, charger, connector_id=2)
                drop_connection(charger)
                await _offline_settle(time, charger, CHARGING)  # sampling + backoff
                assert time.advance_to_next() < 1  # the seeded backoff: 0.5-1 s
                await wait_until(lambda: charger.booted)
                await settle(time, charger, CHARGING)
                await advance_to(
                    time, time() + timedelta(seconds=70), lambda: settle(time, charger, CHARGING)
                )
                await charger.unplug(2)
                await settle(time, charger, IDLE)

    def lines(path: Path) -> list[tuple[Any, ...]]:
        return [
            (
                r.timestamp,
                r.charger_id,
                r.direction,
                r.message_type,
                r.message_id,
                r.action,
                r.payload,
            )
            for r in read_log(path)
        ]

    return lines(run_dir / "charger.jsonl"), lines(run_dir / "csms.jsonl")


async def test_same_seed_same_sessions(tmp_path: Path) -> None:
    charger_a, csms_a = await _scenario(7, tmp_path / "a")
    charger_b, csms_b = await _scenario(7, tmp_path / "b")
    charger_c, _ = await _scenario(8, tmp_path / "c")

    actions = {line[5] for line in charger_a}
    assert {"StartTransaction", "MeterValues", "StopTransaction", "Authorize"} <= actions
    assert charger_a == charger_b  # every field: timestamps, message ids, payloads
    assert csms_a == csms_b
    assert [line[4] for line in charger_a] != [line[4] for line in charger_c]


# --- Stopping the charger mid-transaction ------------------------------------------------


async def test_stop_mid_transaction_is_clean(time: ManualTime, sim_csms: CsmsHarness) -> None:
    """run() stopped while a sampling task (lifetime TaskGroup) is sleeping: it is cancelled
    with the rest, and the connection still closes with 1001 (checked by running())."""
    charger = make_charger(sim_csms.url, time)
    async with running(charger, csms=sim_csms):
        await boot(time, sim_csms, charger)
        await start_session(time, charger)
    assert time.sleeper_count == 0  # every timer cancelled: nothing left running
