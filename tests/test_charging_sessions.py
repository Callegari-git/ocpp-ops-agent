"""Charging sessions on a virtual charger against a real CSMS, under virtual time (ManualTime).

Each step is followed by _settle(): every transaction message delivered and the charger
asleep on a known number of timers. Only then does the test move time, so runs are exact.
"""

from collections.abc import Callable
from contextlib import AsyncExitStack
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from ocpp.v16.enums import AuthorizationStatus, Reason
from ocpp.v16.enums import ChargePointStatus as S

from fakes import (
    ACCEPTED_TAG,
    BLOCKED_TAG,
    EXPIRED_TAG,
    HEARTBEAT_INTERVAL,
    CsmsHarness,
    running_csms,
    wait_until,
)
from manual_time import ManualTime
from ocpp_ops.charger.charger import SessionError, VirtualCharger
from ocpp_ops.csms.registry import ChargerState, Transaction
from ocpp_ops.csms.server import start_server
from ocpp_ops.message_log import LogRecord, MessageLog
from sim import drop_connection, make_charger, read_log, running

POWER_W = 7_200  # 2 Wh per second: easy exact numbers
SAMPLE_INTERVAL = 60  # MeterValueSampleInterval default
WH_PER_SAMPLE = POWER_W * SAMPLE_INTERVAL // 3600  # 120 Wh
# Timers while idle and online: the heartbeat. During a transaction: + meter sampling.
IDLE = 1
CHARGING = 2


def _charger(csms: CsmsHarness) -> ChargerState:
    state = csms.registry.get("CP_1")
    assert state is not None
    return state


def _active(csms: CsmsHarness, connector_id: int = 1) -> Transaction:
    transaction = _charger(csms).transaction_on(connector_id)
    assert transaction is not None, f"no active transaction on connector {connector_id}"
    return transaction


async def _settle(time: ManualTime, charger: VirtualCharger, sleepers: int) -> None:
    """Wait until every transaction message is delivered and `sleepers` timers are set."""
    await wait_until(lambda: charger.queued_messages == 0 and time.sleeper_count == sleepers)


async def _advance_to(time: ManualTime, until: datetime, settled: Callable[[], Any]) -> None:
    """Wake the timers one at a time up to `until`, waiting for `settled()` after each."""
    while (deadline := time.next_deadline) is not None and deadline <= until:
        time.advance_to_next()
        await settled()


async def _boot(time: ManualTime, csms: CsmsHarness, charger: VirtualCharger) -> None:
    await wait_until(lambda: csms.registry.get("CP_1") is not None and charger.booted)
    await _settle(time, charger, IDLE)


async def _start_session(
    time: ManualTime, charger: VirtualCharger, connector_id: int = 1, sleepers: int = CHARGING
) -> None:
    await charger.plug_in(connector_id)
    assert await charger.authorize(connector_id, ACCEPTED_TAG) == AuthorizationStatus.accepted
    await charger.start(connector_id)
    await _settle(time, charger, sleepers)


def _calls(records: list[LogRecord], *actions: str) -> list[LogRecord]:
    """CALLs from the charger (as the CSMS logged them), optionally of some actions only."""
    return [
        r
        for r in records
        if r.direction == "in" and r.message_type == "CALL" and (not actions or r.action in actions)
    ]


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
    return [
        int(_sample(r)["Energy.Active.Import.Register"]) for r in _calls(records, "MeterValues")
    ]


# --- A full session --------------------------------------------------------------------


async def test_full_session(time: ManualTime, sim_csms: CsmsHarness) -> None:
    charger = make_charger(sim_csms.url, time, max_power_w=POWER_W)
    async with running(charger, csms=sim_csms):
        await _boot(time, sim_csms, charger)

        await charger.plug_in(1)
        assert _charger(sim_csms).connectors[1].status == S.preparing

        assert await charger.authorize(1, ACCEPTED_TAG) == AuthorizationStatus.accepted
        started_at = time()
        transaction = await charger.start(1)
        await _settle(time, charger, CHARGING)

        assert transaction.transaction_id == 1  # from StartTransaction.conf
        assert _charger(sim_csms).connectors[1].status == S.charging
        active = _active(sim_csms)
        assert (active.id, active.id_tag, active.meter_start) == (1, ACCEPTED_TAG, 0)
        assert active.started_at == started_at

        # Two samples, at the interval from the start (a heartbeat comes in between).
        await _advance_to(
            time,
            started_at + timedelta(seconds=2 * SAMPLE_INTERVAL),
            lambda: _settle(time, charger, CHARGING),
        )
        samples = _calls(read_log(sim_csms.log_path), "MeterValues")
        assert [r.timestamp for r in samples] == [
            started_at + timedelta(seconds=SAMPLE_INTERVAL),
            started_at + timedelta(seconds=2 * SAMPLE_INTERVAL),
        ]
        assert {r.payload["transactionId"] for r in samples} == {1}
        assert [_sample(r) for r in samples] == [
            {"Energy.Active.Import.Register": "120", "Power.Active.Import": "7200"},
            {"Energy.Active.Import.Register": "240", "Power.Active.Import": "7200"},
        ]
        assert _active(sim_csms).energy_wh == 240

        stopped_at = time()
        await charger.stop(1)
        await _settle(time, charger, IDLE)  # sampling stopped

        state = _charger(sim_csms)
        assert state.connectors[1].status == S.finishing
        assert state.active_transactions == {}
        [finished] = state.finished_transactions
        assert (finished.id, finished.meter_stop, finished.energy_wh) == (1, 240, 240)
        assert finished.stopped_at == stopped_at
        assert (finished.duration_s, finished.stop_reason) == (120, Reason.local)

        await charger.unplug(1)
        assert _charger(sim_csms).connectors[1].status == S.available
        assert charger.transaction(1) is None

    # The whole session as the CSMS logged it, in order (after the 3 boot statuses): each
    # status before its transaction message, as our charger sends them.
    session = [
        (r.action, r.payload.get("status"))
        for r in _calls(read_log(sim_csms.log_path))
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
    stop_call = _calls(read_log(sim_csms.log_path), "StopTransaction")[0]
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
        await _boot(time, sim_csms, charger)
        await _start_session(time, charger)
        started_at = time()
        await _advance_to(
            time,
            started_at + timedelta(seconds=SAMPLE_INTERVAL),
            lambda: _settle(time, charger, CHARGING),
        )

        await charger.unplug(1)
        await _settle(time, charger, IDLE)

        state = _charger(sim_csms)
        assert state.connectors[1].status == S.available
        [finished] = state.finished_transactions
        assert (finished.stop_reason, finished.meter_stop) == (Reason.ev_disconnected, 120)

    records = read_log(sim_csms.log_path)
    [stop_call] = _calls(records, "StopTransaction")
    assert stop_call.payload["reason"] == "EVDisconnected"
    assert "idTag" not in stop_call.payload  # nobody presented a tag to stop it
    # §4.9: Finishing with info "EV side disconnected", then Available.
    statuses = [
        (r.payload["status"], r.payload.get("info"))
        for r in _calls(records, "StatusNotification")
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
        await _boot(time, sim_csms, charger)
        await charger.plug_in(1)

        assert await charger.authorize(1, id_tag) == expected

        assert _charger(sim_csms).connectors[1].status == S.available
        with pytest.raises(SessionError, match="is Available"):
            await charger.start(1)

    assert _charger(sim_csms).active_transactions == {}
    assert _calls(read_log(sim_csms.log_path), "StartTransaction") == []


async def test_session_actions_check_the_connector_state(
    time: ManualTime, sim_csms: CsmsHarness
) -> None:
    charger = make_charger(sim_csms.url, time)
    async with running(charger, csms=sim_csms):
        await _boot(time, sim_csms, charger)

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


# --- Energy ----------------------------------------------------------------------------


async def test_meter_keeps_counting_across_sessions(
    time: ManualTime, sim_csms: CsmsHarness
) -> None:
    charger = make_charger(sim_csms.url, time, max_power_w=POWER_W)
    async with running(charger, csms=sim_csms):
        await _boot(time, sim_csms, charger)
        await _start_session(time, charger)
        await _advance_to(
            time, time() + timedelta(seconds=90), lambda: _settle(time, charger, CHARGING)
        )
        await charger.stop(1)  # after 84 s (two heartbeats, one sample at 60 s): 168 Wh
        await charger.unplug(1)
        await _settle(time, charger, IDLE)
        # Idle for a while: no power, no energy.
        await _advance_to(time, time() + timedelta(seconds=200), lambda: _settle(time, charger, 1))
        assert charger.meter_wh(1) == 168

        await _start_session(time, charger)

        second = _active(sim_csms)
        # §7.31: the register is not reset at the start of a transaction.
        assert second.meter_start == 168
        assert charger.meter_wh(2) == 0  # each connector has its own meter


async def test_two_connectors_charge_at_once(time: ManualTime, sim_csms: CsmsHarness) -> None:
    charger = make_charger(sim_csms.url, time, max_power_w=POWER_W)
    async with running(charger, csms=sim_csms):
        await _boot(time, sim_csms, charger)
        await _start_session(time, charger, connector_id=1, sleepers=2)
        await _start_session(time, charger, connector_id=2, sleepers=3)
        start = time()
        await _advance_to(
            time,
            start + timedelta(seconds=SAMPLE_INTERVAL),
            lambda: _settle(time, charger, 3),
        )

        # No load sharing: each connector draws the full power.
        assert (_active(sim_csms, 1).energy_wh, _active(sim_csms, 2).energy_wh) == (120, 120)
        assert {_active(sim_csms, 1).id, _active(sim_csms, 2).id} == {1, 2}


# --- MeterValueSampleInterval ----------------------------------------------------------


async def test_sample_interval_change_applies_at_once(
    time: ManualTime, sim_csms: CsmsHarness
) -> None:
    charger = make_charger(sim_csms.url, time, max_power_w=POWER_W)
    async with running(charger, csms=sim_csms):
        await _boot(time, sim_csms, charger)
        await _start_session(time, charger)
        started_at = time()
        time.advance_to_next()  # the first heartbeat, at 42 s
        await _settle(time, charger, CHARGING)
        changed_at = time()
        assert changed_at == started_at + timedelta(seconds=HEARTBEAT_INTERVAL)

        result = await sim_csms.central_system.change_configuration(
            "CP_1", "MeterValueSampleInterval", "15"
        )
        assert result.status == "Accepted"
        await _settle(time, charger, CHARGING)  # the 60 s timer was replaced

        await _advance_to(
            time, changed_at + timedelta(seconds=30), lambda: _settle(time, charger, CHARGING)
        )
        samples = _calls(read_log(sim_csms.log_path), "MeterValues")
        # Counted from the change, not from the start (which would give 45 s, 60 s).
        assert [r.timestamp for r in samples] == [
            changed_at + timedelta(seconds=15),
            changed_at + timedelta(seconds=30),
        ]

        await sim_csms.central_system.change_configuration("CP_1", "MeterValueSampleInterval", "0")
        await _settle(time, charger, IDLE)  # no sampling timer at all, only the heartbeat
        await _advance_to(
            time, time() + timedelta(seconds=300), lambda: _settle(time, charger, IDLE)
        )
        assert len(_calls(read_log(sim_csms.log_path), "MeterValues")) == 2


async def test_sample_interval_zero_sends_no_meter_values(
    time: ManualTime, sim_csms: CsmsHarness
) -> None:
    charger = make_charger(sim_csms.url, time, max_power_w=POWER_W)
    charger.config.change("MeterValueSampleInterval", "0")
    async with running(charger, csms=sim_csms):
        await _boot(time, sim_csms, charger)
        await _start_session(time, charger, sleepers=IDLE)
        await _advance_to(
            time, time() + timedelta(seconds=300), lambda: _settle(time, charger, IDLE)
        )
        await charger.stop(1)
        await _settle(time, charger, IDLE)

    assert _calls(read_log(sim_csms.log_path), "MeterValues") == []
    # The transaction itself is still complete: the CSMS learns the energy at the stop.
    [finished] = _charger(sim_csms).finished_transactions
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
        await _boot(time, sim_csms, charger)
        await _start_session(time, charger)
        started_at = time()
        await _advance_to(
            time,
            started_at + timedelta(seconds=SAMPLE_INTERVAL),
            lambda: _settle(time, charger, CHARGING),
        )

        # The CSMS stops accepting connections (it keeps its state), then the network drops.
        sim_csms.server.close(close_connections=False)
        drop_connection(charger)
        await _offline_settle(time, charger, CHARGING)  # sampling + reconnect backoff

        # Offline: reconnect attempts fail, samples go on and are queued.
        await _advance_to(
            time,
            started_at + timedelta(seconds=3 * SAMPLE_INTERVAL),
            lambda: _offline_settle(time, charger, CHARGING),
        )
        stopped_at = time()
        await charger.stop(1)
        await _offline_settle(time, charger, 1)  # only the backoff timer left
        assert charger.queued_messages == 3  # MeterValues at 120 s and 180 s, StopTransaction
        assert _active(sim_csms).id == 1  # the CSMS doesn't know yet

        await _restart_listening(restarted, sim_csms)
        time.advance_to_next()  # reconnect
        await wait_until(lambda: charger.booted)
        await _settle(time, charger, IDLE)

        state = _charger(sim_csms)
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
        for r in _calls(records)
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
    samples = _calls(records, "MeterValues")
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
        await _boot(time, sim_csms, charger)
        await charger.plug_in(1)
        assert await charger.authorize(1, ACCEPTED_TAG) == AuthorizationStatus.accepted

        sim_csms.server.close(close_connections=False)
        drop_connection(charger)
        await _offline_settle(time, charger, 1)

        started_at = time()
        transaction = await charger.start(1)  # offline: StartTransaction is queued
        await _offline_settle(time, charger, CHARGING)
        await _advance_to(
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
        await _settle(time, charger, CHARGING)

        assert transaction.transaction_id == 1
        active = _active(sim_csms)
        assert (active.id, active.started_at) == (1, started_at)
        assert _charger(sim_csms).anomalies == []
        # The MeterValues queued before the id was known went out with it.
        records = read_log(sim_csms.log_path)
        assert {r.payload["transactionId"] for r in _calls(records, "MeterValues")} == {1}
        assert active.energy_wh == _energy_values(records)[-1]
        assert _energy_values(records)[0] == WH_PER_SAMPLE


# --- Failures to process: TransactionMessageAttempts / RetryInterval (§3.7.1) -----------


def _fail_starts(monkeypatch: pytest.MonkeyPatch, csms: CsmsHarness, times: int) -> None:
    """The CSMS fails to process the next `times` StartTransactions: ocpp answers an
    exception in a handler with a CALLERROR (InternalError)."""
    original = csms.registry.start_transaction
    remaining = times

    def flaky(*args: Any, **kwargs: Any) -> int:
        nonlocal remaining
        if remaining > 0:
            remaining -= 1
            raise RuntimeError("simulated CSMS failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(csms.registry, "start_transaction", flaky)


async def _quiet_charger(time: ManualTime, csms: CsmsHarness, charger: VirtualCharger) -> None:
    """Boot, then no heartbeats and no sampling: only retry timers are left to watch."""
    await _boot(time, csms, charger)
    await csms.central_system.change_configuration("CP_1", "HeartbeatInterval", "0")
    await wait_until(lambda: time.sleeper_count == 0)


async def test_failed_start_is_retried_with_growing_waits(
    time: ManualTime, sim_csms: CsmsHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fail_starts(monkeypatch, sim_csms, times=2)
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
        await _settle(time, charger, 0)

        assert transaction.transaction_id == 1
        assert _active(sim_csms).started_at == started_at  # the original start time

    records = read_log(sim_csms.log_path)
    attempts = _calls(records, "StartTransaction")
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
    _fail_starts(monkeypatch, sim_csms, times=99)
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
        await _settle(time, charger, 0)  # second failure: discarded, no more waiting

        # Errata 3.18: no id will ever come; later messages say -1.
        assert transaction.transaction_id == -1
        await charger.stop(1)
        await _settle(time, charger, 0)

    records = read_log(sim_csms.log_path)
    assert len(_calls(records, "StartTransaction")) == 2
    [stop_call] = _calls(records, "StopTransaction")
    assert stop_call.payload["transactionId"] == -1
    # Answered anyway, and visible to the agent.
    [anomaly] = _charger(sim_csms).anomalies
    assert (anomaly.action, anomaly.transaction_id) == ("StopTransaction", -1)


# --- Reproducibility -------------------------------------------------------------------


async def _scenario(seed: int, run_dir: Path) -> tuple[list[tuple[Any, ...]], ...]:
    """Two sessions, one ended by the driver and one by the EV, with a connection drop.
    Returns the charger-side and CSMS-side logs."""
    time = ManualTime()
    with MessageLog(run_dir / "charger.jsonl") as log:
        async with running_csms(time, run_dir / "csms.jsonl") as csms:
            charger = make_charger(csms.url, time, seed=seed, log=log, max_power_w=POWER_W)
            async with running(charger, csms=csms):
                await _boot(time, csms, charger)
                await _start_session(time, charger)
                await _advance_to(
                    time, time() + timedelta(seconds=130), lambda: _settle(time, charger, CHARGING)
                )
                await charger.stop(1)
                await charger.unplug(1)
                await _settle(time, charger, IDLE)

                await _start_session(time, charger, connector_id=2)
                drop_connection(charger)
                await _offline_settle(time, charger, CHARGING)  # sampling + backoff
                assert time.advance_to_next() < 1  # the seeded backoff: 0.5-1 s
                await wait_until(lambda: charger.booted)
                await _settle(time, charger, CHARGING)
                await _advance_to(
                    time, time() + timedelta(seconds=70), lambda: _settle(time, charger, CHARGING)
                )
                await charger.unplug(2)
                await _settle(time, charger, IDLE)

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
        await _boot(time, sim_csms, charger)
        await _start_session(time, charger)
    assert time.sleeper_count == 0  # every timer cancelled: nothing left running
