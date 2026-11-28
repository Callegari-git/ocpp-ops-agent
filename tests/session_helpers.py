"""Shared steps for charging-session tests: a charger "CP_1" against a real CSMS, under
virtual time (ManualTime).

Each step is followed by settle(): every transaction message delivered and the charger
asleep on a known number of timers. Only then does the test move time, so runs are exact.
"""

from collections.abc import Callable
from datetime import datetime
from typing import Any

from ocpp.v16.enums import AuthorizationStatus

from fakes import ACCEPTED_TAG, CsmsHarness, wait_until
from manual_time import ManualTime
from ocpp_ops.charger.charger import VirtualCharger
from ocpp_ops.csms.registry import ChargerState, Transaction
from ocpp_ops.message_log import LogRecord

POWER_W = 7_200  # 2 Wh per second: easy exact numbers
SAMPLE_INTERVAL = 60  # MeterValueSampleInterval default
WH_PER_SAMPLE = POWER_W * SAMPLE_INTERVAL // 3600  # 120 Wh
# Timers while idle and online: the heartbeat. During a transaction: + meter sampling.
IDLE = 1
CHARGING = 2


def charger_state(csms: CsmsHarness) -> ChargerState:
    state = csms.registry.get("CP_1")
    assert state is not None
    return state


def active_transaction(csms: CsmsHarness, connector_id: int = 1) -> Transaction:
    transaction = charger_state(csms).transaction_on(connector_id)
    assert transaction is not None, f"no active transaction on connector {connector_id}"
    return transaction


async def settle(time: ManualTime, charger: VirtualCharger, sleepers: int) -> None:
    """Wait until every transaction message is delivered and `sleepers` timers are set."""
    await wait_until(lambda: charger.queued_messages == 0 and time.sleeper_count == sleepers)


async def advance_to(time: ManualTime, until: datetime, settled: Callable[[], Any]) -> None:
    """Wake the timers one at a time up to `until`, waiting for `settled()` after each."""
    while (deadline := time.next_deadline) is not None and deadline <= until:
        time.advance_to_next()
        await settled()


async def boot(time: ManualTime, csms: CsmsHarness, charger: VirtualCharger) -> None:
    await wait_until(lambda: csms.registry.get("CP_1") is not None and charger.booted)
    await settle(time, charger, IDLE)


async def start_session(
    time: ManualTime, charger: VirtualCharger, connector_id: int = 1, sleepers: int = CHARGING
) -> None:
    await charger.plug_in(connector_id)
    assert await charger.authorize(connector_id, ACCEPTED_TAG) == AuthorizationStatus.accepted
    await charger.start(connector_id)
    await settle(time, charger, sleepers)


def calls(records: list[LogRecord], *actions: str) -> list[LogRecord]:
    """CALLs from the charger (as the CSMS logged them), optionally of some actions only."""
    return [
        r
        for r in records
        if r.direction == "in" and r.message_type == "CALL" and (not actions or r.action in actions)
    ]
