"""Acting on a charger during a session: Reset (§5.14) and ChangeAvailability (§5.2).

A virtual charger against a real CSMS, under virtual time (ManualTime): see session_helpers.
"""

import asyncio
from datetime import datetime, timedelta
from typing import Any

import ocpp.charge_point
import pytest
from ocpp.exceptions import InternalError
from ocpp.messages import Call, CallResult
from ocpp.v16.enums import AuthorizationStatus, AvailabilityStatus, Reason, ResetType
from ocpp.v16.enums import AvailabilityType as A
from ocpp.v16.enums import ChargePointStatus as S

from fakes import ACCEPTED_TAG, CsmsHarness, wait_until
from manual_time import ManualTime
from ocpp_ops.charger.charger import SessionError, VirtualCharger
from ocpp_ops.message_log import LogRecord
from session_helpers import (
    CHARGING,
    IDLE,
    POWER_W,
    advance_to,
    boot,
    charger_state,
    settle,
    start_session,
)
from sim import REBOOT_DELAY, make_charger, read_log, running

# --- Helpers -----------------------------------------------------------------------------


def _wire(records: list[LogRecord]) -> list[tuple[Any, ...]]:
    """CALLs both ways and connection events, compactly: ("SN", connector, status),
    (action, reason or None), or (event, close reason or None). Heartbeats left out."""
    lines: list[tuple[Any, ...]] = []
    for r in records:
        if r.direction is None:
            lines.append((r.message_type, r.payload.get("reason")))
        elif r.message_type != "CALL" or r.action == "Heartbeat":
            continue
        elif r.action == "StatusNotification":
            lines.append(("SN", r.payload["connectorId"], r.payload["status"]))
        else:
            lines.append((r.action, r.payload.get("reason")))
    return lines


def _after(lines: list[tuple[Any, ...]], first: tuple[Any, ...]) -> list[tuple[Any, ...]]:
    """`lines` from the last occurrence of `first` on."""
    start = len(lines) - 1 - lines[::-1].index(first)
    return lines[start:]


def _boot_statuses(connector_1: S, connector_2: S = S.available) -> list[tuple[Any, ...]]:
    return [
        ("BootNotification", None),
        ("SN", 0, "Available"),
        ("SN", 1, connector_1.value),
        ("SN", 2, connector_2.value),
    ]


async def _charge_for(time: ManualTime, charger: VirtualCharger, seconds: int) -> datetime:
    """Start a session on connector 1, let virtual time run; returns the start time."""
    await start_session(time, charger)
    started_at = time()
    await advance_to(
        time, started_at + timedelta(seconds=seconds), lambda: settle(time, charger, CHARGING)
    )
    return started_at


async def _reset(csms: CsmsHarness, charger: VirtualCharger, reset_type: ResetType) -> None:
    result = await csms.central_system.reset("CP_1", reset_type)
    assert result.status.value == "Accepted"


async def _reboot(time: ManualTime, csms: CsmsHarness, charger: VirtualCharger) -> None:
    """From the reset to the next boot, its statuses and the queued messages delivered."""
    await wait_until(lambda: not charger.connected and time.sleeper_count == 1)
    assert time.advance_to_next() == REBOOT_DELAY
    await wait_until(lambda: charger.booted)
    await settle(time, charger, IDLE)


def _log_mark(csms: CsmsHarness) -> int:
    """Where the CSMS log is now (virtual time stands still: timestamps can't tell)."""
    return len(read_log(csms.log_path))


def _logged_since(csms: CsmsHarness, mark: int) -> list[LogRecord]:
    return read_log(csms.log_path)[mark:]


def _status(csms: CsmsHarness, connector_id: int) -> S | None:
    return charger_state(csms).connectors[connector_id].status


# --- Reset during a session ----------------------------------------------------------------


async def test_soft_reset_stops_the_transaction_before_rebooting(
    time: ManualTime, sim_csms: CsmsHarness
) -> None:
    charger = make_charger(sim_csms.url, time, max_power_w=POWER_W)
    async with running(charger, csms=sim_csms):
        await boot(time, sim_csms, charger)
        started_at = await _charge_for(time, charger, 90)
        reset_at = time()  # the last timer before 90 s: a heartbeat at 84 s

        await _reset(sim_csms, charger, ResetType.soft)
        await wait_until(lambda: not charger.connected)
        # §5.14: StopTransaction sent (and answered) before the reboot, nothing left queued.
        assert charger.queued_messages == 0
        state = charger_state(sim_csms)
        assert state.active_transactions == {}
        [finished] = state.finished_transactions
        expected_wh = POWER_W * (reset_at - started_at).seconds // 3600  # 168 Wh in 84 s
        assert (finished.stop_reason, finished.meter_stop) == (Reason.soft_reset, expected_wh)
        assert (finished.stopped_at, finished.energy_wh) == (reset_at, expected_wh)

        await _reboot(time, sim_csms, charger)
        assert _status(sim_csms, 1) == S.finishing  # the cable is still in
        await charger.unplug(1)
        assert _status(sim_csms, 1) == S.available
        assert charger.meter_wh(1) == expected_wh  # the meter kept counting across the reboot

    lines = _after(_wire(read_log(sim_csms.log_path)), ("Reset", None))
    assert lines[:10] == [
        ("Reset", None),
        ("SN", 1, "Finishing"),
        ("StopTransaction", "SoftReset"),
        ("DISCONNECTED", "clean"),
        ("CONNECTED", None),
        *_boot_statuses(S.finishing),
        ("SN", 1, "Available"),
    ]


async def test_hard_reset_sends_the_stop_after_the_reboot(
    time: ManualTime, sim_csms: CsmsHarness
) -> None:
    charger = make_charger(sim_csms.url, time, max_power_w=POWER_W)
    async with running(charger, csms=sim_csms):
        await boot(time, sim_csms, charger)
        started_at = await _charge_for(time, charger, 90)
        reset_at = time()

        await _reset(sim_csms, charger, ResetType.hard)
        await wait_until(lambda: not charger.connected and charger.queued_messages == 1)
        # Not graceful: the CSMS still sees the transaction; the stop waits in the queue.
        assert charger_state(sim_csms).transaction_on(1) is not None
        assert charger.transaction(1) is None

        await _reboot(time, sim_csms, charger)
        state = charger_state(sim_csms)
        assert state.active_transactions == {}
        [finished] = state.finished_transactions
        expected_wh = POWER_W * (reset_at - started_at).seconds // 3600
        assert (finished.stop_reason, finished.meter_stop) == (Reason.hard_reset, expected_wh)
        # Time and meter of the reset, not of the delivery 5 s later.
        assert finished.stopped_at == reset_at
        assert finished.duration_s == (reset_at - started_at).seconds
        assert _status(sim_csms, 1) == S.finishing

    lines = _after(_wire(read_log(sim_csms.log_path)), ("Reset", None))
    # §5.14: after the reboot and an accepted boot (and its statuses, §4.2).
    assert lines[:8] == [
        ("Reset", None),
        ("DISCONNECTED", "clean"),
        ("CONNECTED", None),
        *_boot_statuses(S.finishing),
        ("StopTransaction", "HardReset"),
    ]


@pytest.mark.parametrize("after", [S.finishing, S.preparing, S.available])
async def test_status_after_reboot_with_a_cable_plugged_in(
    time: ManualTime, sim_csms: CsmsHarness, after: S
) -> None:
    """Connector 1 was charging, connector 2 plugged in but not authorized yet: after the
    reboot both report the vendor's `plugged_status_after_reboot`."""
    charger = make_charger(sim_csms.url, time, plugged_status_after_reboot=after)
    async with running(charger, csms=sim_csms):
        await boot(time, sim_csms, charger)
        await start_session(time, charger)
        await charger.plug_in(2)

        await _reset(sim_csms, charger, ResetType.hard)
        await _reboot(time, sim_csms, charger)

        assert (_status(sim_csms, 1), _status(sim_csms, 2)) == (after, after)
        if after == S.available:
            # The firmware forgot the cable: the driver can't "unplug" it any more.
            with pytest.raises(SessionError):
                await charger.unplug(1)
        else:
            await charger.unplug(1)
            assert _status(sim_csms, 1) == S.available


def test_plugged_status_after_reboot_must_be_a_plugged_status() -> None:
    with pytest.raises(ValueError, match="Charging"):
        make_charger("ws://unused", ManualTime(), plugged_status_after_reboot=S.charging)


async def test_soft_reset_does_not_wait_through_a_stop_retry(
    time: ManualTime, sim_csms: CsmsHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§5.14: a StopTransaction the CSMS fails to process stays queued; the reset goes on,
    and the stop is delivered after the reboot."""
    original = sim_csms.registry.stop_transaction
    failures = 1

    def flaky(*args: Any, **kwargs: Any) -> None:
        nonlocal failures
        if failures:
            failures -= 1
            raise RuntimeError("simulated CSMS failure")  # answered with a CALLERROR
        original(*args, **kwargs)

    monkeypatch.setattr(sim_csms.registry, "stop_transaction", flaky)
    charger = make_charger(sim_csms.url, time)
    async with running(charger, csms=sim_csms):
        await boot(time, sim_csms, charger)
        await start_session(time, charger)

        await _reset(sim_csms, charger, ResetType.soft)
        # Offline, with the stop still queued; its retry backoff was cut short by the reboot.
        await wait_until(lambda: not charger.connected and time.sleeper_count == 1)
        assert charger.queued_messages == 1
        assert time.advance_to_next() == REBOOT_DELAY
        await wait_until(lambda: charger.booted)
        await settle(time, charger, IDLE)

        [finished] = charger_state(sim_csms).finished_transactions
        assert finished.stop_reason == Reason.soft_reset


async def test_soft_reset_still_reboots_when_the_stop_gets_no_answer(
    time: ManualTime, sim_csms: CsmsHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CSMS never answers the StopTransaction: its timeout ends the session (as any
    timeout does) before the reset closes the connection. The accepted reset must still
    happen: a reboot (REBOOT_DELAY), not a plain reconnect (backoff under 1 s)."""
    original = ocpp.charge_point.validate_payload
    stop_checks = 0
    rebooted = asyncio.Event()

    async def validate(message: Any, ocpp_version: str) -> None:
        nonlocal stop_checks
        if isinstance(message, Call) and message.action == "StopTransaction":
            stop_checks += 1
            if stop_checks == 2:  # 1: the charger before sending; 2: the CSMS on arrival
                await rebooted.wait()
                # Never processed. The CALLERROR ocpp then sends hits the closed old
                # connection, which ends its handler (or the server would wait for it).
                raise InternalError(description="test: dropped")
        await original(message, ocpp_version)

    monkeypatch.setattr(ocpp.charge_point, "validate_payload", validate)
    charger = make_charger(sim_csms.url, time)
    async with running(charger, csms=sim_csms):
        await boot(time, sim_csms, charger)
        await start_session(time, charger)

        await _reset(sim_csms, charger, ResetType.soft)
        # The charger's call timeout (2 s, real time) ends the session.
        await wait_until(lambda: not charger.connected and time.sleeper_count == 1, within=5)
        assert time.advance_to_next() == REBOOT_DELAY
        await wait_until(lambda: charger.booted)
        rebooted.set()
        await settle(time, charger, IDLE)

        [finished] = charger_state(sim_csms).finished_transactions  # resent after the boot
        assert finished.stop_reason == Reason.soft_reset


async def test_lost_start_answer_and_hard_reset_make_a_ghost_session(
    time: ManualTime, sim_csms: CsmsHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Known behavior, kept on purpose (a week-2 fault candidate): the CSMS answers
    StartTransaction, but the answer is lost when a hard reset cuts the connection. The
    charger, never having had the answer, sends StartTransaction again after the reboot,
    and our CSMS (which does not deduplicate) opens a second transaction. The first one stays
    active forever: no StopTransaction will ever name it."""
    original = ocpp.charge_point.validate_payload
    start_answers = 0
    never = asyncio.Event()

    async def validate(message: Any, ocpp_version: str) -> None:
        nonlocal start_answers
        if isinstance(message, CallResult) and message.action == "StartTransaction":
            start_answers += 1
            # 1: the CSMS checks its answer before sending it. 2: the charger checks it
            # on arrival: held until the reset ends the session, so it is never used.
            if start_answers == 2:
                await never.wait()
        await original(message, ocpp_version)

    monkeypatch.setattr(ocpp.charge_point, "validate_payload", validate)
    charger = make_charger(sim_csms.url, time)
    async with running(charger, csms=sim_csms):
        await boot(time, sim_csms, charger)
        await charger.plug_in(1)
        assert await charger.authorize(1, ACCEPTED_TAG) == AuthorizationStatus.accepted
        await charger.start(1)
        await wait_until(lambda: start_answers == 2)
        assert charger_state(sim_csms).transaction_on(1) is not None  # opened by the CSMS

        await _reset(sim_csms, charger, ResetType.hard)
        await _reboot(time, sim_csms, charger)

        state = charger_state(sim_csms)
        assert list(state.active_transactions) == [1]  # the ghost
        [finished] = state.finished_transactions
        assert (finished.id, finished.stop_reason) == (2, Reason.hard_reset)
        assert [anomaly.kind for anomaly in state.anomalies] == ["start_on_busy_connector"]


# --- ChangeAvailability ------------------------------------------------------------------


async def _change(
    csms: CsmsHarness, connector_id: int, availability: A, expected: AvailabilityStatus
) -> None:
    result = await csms.central_system.change_availability("CP_1", connector_id, availability)
    assert (result.connector_id, result.type, result.status) == (
        connector_id,
        availability,
        expected,
    )


async def test_idle_connector_changes_at_once(time: ManualTime, sim_csms: CsmsHarness) -> None:
    charger = make_charger(sim_csms.url, time)
    async with running(charger, csms=sim_csms):
        await boot(time, sim_csms, charger)
        mark = _log_mark(sim_csms)

        await _change(sim_csms, 1, A.inoperative, AvailabilityStatus.accepted)
        await wait_until(lambda: _status(sim_csms, 1) == S.unavailable)
        with pytest.raises(SessionError):  # nobody can plug in
            await charger.plug_in(1)

        await _change(sim_csms, 1, A.operative, AvailabilityStatus.accepted)
        await wait_until(lambda: _status(sim_csms, 1) == S.available)

    lines = _wire(_logged_since(sim_csms, mark))
    # The answer first, then the StatusNotification (§5.2: once the change has happened).
    assert lines[:4] == [
        ("ChangeAvailability", None),
        ("SN", 1, "Unavailable"),
        ("ChangeAvailability", None),
        ("SN", 1, "Available"),
    ]
    # The charger's answer reached the CSMS before its StatusNotification.
    first_change = [
        (r.direction, r.message_type, r.action)
        for r in _logged_since(sim_csms, mark)
        if r.action in ("ChangeAvailability", "StatusNotification")
    ][:3]
    assert first_change == [
        ("out", "CALL", "ChangeAvailability"),
        ("in", "CALLRESULT", "ChangeAvailability"),
        ("in", "CALL", "StatusNotification"),
    ]


async def test_already_in_the_requested_state_is_accepted_without_status(
    time: ManualTime, sim_csms: CsmsHarness
) -> None:
    charger = make_charger(sim_csms.url, time)
    async with running(charger, csms=sim_csms):
        await boot(time, sim_csms, charger)
        mark = _log_mark(sim_csms)

        await _change(sim_csms, 2, A.operative, AvailabilityStatus.accepted)  # §5.2
        await settle(time, charger, IDLE)

    assert _wire(_logged_since(sim_csms, mark))[:1] == [("ChangeAvailability", None)]
    assert ("SN", 2, "Available") not in _wire(_logged_since(sim_csms, mark))


async def test_unknown_connector_is_rejected(time: ManualTime, sim_csms: CsmsHarness) -> None:
    charger = make_charger(sim_csms.url, time)
    async with running(charger, csms=sim_csms):
        await boot(time, sim_csms, charger)
        await _change(sim_csms, 3, A.inoperative, AvailabilityStatus.rejected)
        assert [charger.operative(i) for i in charger.connector_ids] == [True, True, True]


async def test_scheduled_during_a_session_then_applied_when_the_cable_is_removed(
    time: ManualTime, sim_csms: CsmsHarness
) -> None:
    charger = make_charger(sim_csms.url, time)
    async with running(charger, csms=sim_csms):
        await boot(time, sim_csms, charger)
        await start_session(time, charger)
        mark = _log_mark(sim_csms)

        await _change(sim_csms, 1, A.inoperative, AvailabilityStatus.scheduled)
        await settle(time, charger, CHARGING)
        assert _status(sim_csms, 1) == S.charging  # the session goes on

        await charger.stop(1)
        await charger.unplug(1)
        await settle(time, charger, IDLE)

    # Finishing -> Unavailable directly (§4.9 F8): never shown Available to a new driver.
    assert _wire(_logged_since(sim_csms, mark))[:4] == [
        ("ChangeAvailability", None),
        ("SN", 1, "Finishing"),
        ("StopTransaction", "Local"),
        ("SN", 1, "Unavailable"),
    ]


async def test_preparing_goes_through_available(time: ManualTime, sim_csms: CsmsHarness) -> None:
    """§4.9 has no Preparing -> Unavailable (B8): when the driver leaves, Available first."""
    charger = make_charger(sim_csms.url, time)
    async with running(charger, csms=sim_csms):
        await boot(time, sim_csms, charger)
        await charger.plug_in(1)
        mark = _log_mark(sim_csms)

        await _change(sim_csms, 1, A.inoperative, AvailabilityStatus.scheduled)
        await charger.unplug(1)
        await wait_until(lambda: _status(sim_csms, 1) == S.unavailable)

    assert _wire(_logged_since(sim_csms, mark))[:3] == [
        ("ChangeAvailability", None),
        ("SN", 1, "Available"),
        ("SN", 1, "Unavailable"),
    ]


async def test_connector_zero_waits_for_every_connector(
    time: ManualTime, sim_csms: CsmsHarness
) -> None:
    """Connector 0 = the charger and all its connectors (§5.2). The idle connector changes
    at once; connector 0 only once the busy one has changed too."""
    charger = make_charger(sim_csms.url, time)
    async with running(charger, csms=sim_csms):
        await boot(time, sim_csms, charger)
        await start_session(time, charger)
        mark = _log_mark(sim_csms)

        await _change(sim_csms, 0, A.inoperative, AvailabilityStatus.scheduled)
        await wait_until(lambda: _status(sim_csms, 2) == S.unavailable)
        await settle(time, charger, CHARGING)
        assert (_status(sim_csms, 0), _status(sim_csms, 1)) == (S.available, S.charging)

        await charger.unplug(1)  # the EV leaves: StopTransaction EVDisconnected
        await wait_until(lambda: _status(sim_csms, 0) == S.unavailable)
        await settle(time, charger, IDLE)

        # And back: every connector, 0 included, at once (nothing in use).
        await _change(sim_csms, 0, A.operative, AvailabilityStatus.accepted)
        await wait_until(lambda: _status(sim_csms, 0) == S.available)

    assert _wire(_logged_since(sim_csms, mark))[:10] == [
        ("ChangeAvailability", None),
        ("SN", 2, "Unavailable"),
        ("SN", 1, "Finishing"),
        ("StopTransaction", "EVDisconnected"),
        ("SN", 1, "Unavailable"),
        ("SN", 0, "Unavailable"),
        ("ChangeAvailability", None),
        ("SN", 1, "Available"),
        ("SN", 2, "Available"),
        ("SN", 0, "Available"),
    ]


async def test_unavailable_persists_across_a_reboot(
    time: ManualTime, sim_csms: CsmsHarness
) -> None:
    """§4.9: Unavailable set by ChangeAvailability "MUST be persistent across reboots". A
    change still scheduled at the reset is applied once the cable is removed."""
    charger = make_charger(sim_csms.url, time)
    async with running(charger, csms=sim_csms):
        await boot(time, sim_csms, charger)
        await start_session(time, charger)
        await _change(sim_csms, 2, A.inoperative, AvailabilityStatus.accepted)
        await _change(sim_csms, 1, A.inoperative, AvailabilityStatus.scheduled)
        await wait_until(lambda: _status(sim_csms, 2) == S.unavailable)
        await settle(time, charger, CHARGING)

        await _reset(sim_csms, charger, ResetType.hard)
        await _reboot(time, sim_csms, charger)
        assert (_status(sim_csms, 1), _status(sim_csms, 2)) == (S.finishing, S.unavailable)

        await charger.unplug(1)
        assert _status(sim_csms, 1) == S.unavailable
