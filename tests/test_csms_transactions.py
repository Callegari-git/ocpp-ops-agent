"""CSMS side of charging sessions: a real server, a fake charger over a real WebSocket.

The fake charger sends exactly what each test scripts, so these tests also cover orders
and mistakes our own virtual charger never makes.
"""

from typing import Any

import pytest
from ocpp.v16.datatypes import MeterValue, SampledValue
from ocpp.v16.enums import AuthorizationStatus, Measurand, Reason, UnitOfMeasure
from ocpp.v16.enums import ChargePointStatus as S

from fakes import (
    ACCEPTED_TAG,
    BLOCKED_TAG,
    EXPIRED_TAG,
    CsmsHarness,
    FakeChargePoint,
    fake_charger,
    wait_until,
)
from ocpp_ops.csms.registry import ChargerState

T_START = "2026-11-24T11:00:00+00:00"
T_SAMPLE = "2026-11-24T11:01:00+00:00"
T_STOP = "2026-11-24T11:30:00+00:00"


def _charger(csms: CsmsHarness, charger_id: str = "CP_1") -> ChargerState:
    charger = csms.registry.get(charger_id)
    assert charger is not None, f"{charger_id} not in registry"
    return charger


def _energy(value: int) -> list[MeterValue]:
    return [
        MeterValue(
            timestamp=T_SAMPLE,
            sampled_value=[
                SampledValue(
                    value=str(value),
                    measurand=Measurand.energy_active_import_register,
                    unit=UnitOfMeasure.wh,
                )
            ],
        )
    ]


@pytest.mark.parametrize(
    ("id_tag", "expected"),
    [
        (ACCEPTED_TAG, AuthorizationStatus.accepted),
        (ACCEPTED_TAG.lower(), AuthorizationStatus.accepted),  # IdToken ignores case (§7.28)
        (BLOCKED_TAG, AuthorizationStatus.blocked),
        (EXPIRED_TAG, AuthorizationStatus.expired),
        ("NOBODY", AuthorizationStatus.invalid),
    ],
)
async def test_authorize(csms: CsmsHarness, id_tag: str, expected: AuthorizationStatus) -> None:
    async with fake_charger(csms.url) as cp:
        await cp.boot()
        response = await cp.authorize(id_tag)

    assert response.id_tag_info["status"] == expected


async def test_start_meter_values_stop(csms: CsmsHarness) -> None:
    async with fake_charger(csms.url) as cp:
        await cp.boot()
        started = await cp.start_transaction(1, ACCEPTED_TAG, meter_start=1000, timestamp=T_START)
        transaction_id = started.transaction_id

        assert transaction_id == 1
        assert started.id_tag_info["status"] == AuthorizationStatus.accepted
        transaction = _charger(csms).transaction_on(1)
        assert transaction is not None
        assert (transaction.id, transaction.id_tag, transaction.meter_start) == (
            transaction_id,
            ACCEPTED_TAG,
            1000,
        )

        await cp.meter_values(1, _energy(1180), transaction_id)
        assert transaction.energy_wh == 180

        stopped = await cp.stop_transaction(
            transaction_id, 1500, T_STOP, Reason.local, id_tag=ACCEPTED_TAG
        )

    assert stopped.id_tag_info["status"] == AuthorizationStatus.accepted
    charger = _charger(csms)
    assert charger.active_transactions == {}
    [finished] = charger.finished_transactions
    assert (finished.meter_stop, finished.energy_wh, finished.duration_s) == (1500, 500, 1800)
    assert finished.stop_reason == Reason.local
    assert charger.anomalies == []


async def test_start_with_refused_tag_still_gets_a_transaction_id(csms: CsmsHarness) -> None:
    """§4.8: the answer always carries a transaction id; the charger has already started."""
    async with fake_charger(csms.url) as cp:
        await cp.boot()
        started = await cp.start_transaction(1, BLOCKED_TAG, meter_start=0, timestamp=T_START)

    assert started.id_tag_info["status"] == AuthorizationStatus.blocked
    transaction = _charger(csms).transaction_on(1)
    assert transaction is not None and transaction.id == started.transaction_id
    assert transaction.id_tag_status == AuthorizationStatus.blocked


async def test_stop_without_reason_or_tag_means_local(csms: CsmsHarness) -> None:
    async with fake_charger(csms.url) as cp:
        await cp.boot()
        started = await cp.start_transaction(1, ACCEPTED_TAG, meter_start=0, timestamp=T_START)
        stopped = await cp.stop_transaction(started.transaction_id, 10, T_STOP)

    assert stopped.id_tag_info is None  # nobody to report on
    assert _charger(csms).finished_transactions[0].stop_reason == Reason.local  # §4.10


async def test_stop_of_unknown_transaction_is_answered_and_recorded(csms: CsmsHarness) -> None:
    async with fake_charger(csms.url) as cp:
        await cp.boot()
        stopped = await cp.stop_transaction(77, 10, T_STOP, Reason.local, id_tag=ACCEPTED_TAG)

    # §4.10: sanity checks never stop the CSMS from answering.
    assert stopped.id_tag_info["status"] == AuthorizationStatus.accepted
    charger = _charger(csms)
    assert charger.finished_transactions == []
    [anomaly] = charger.anomalies
    assert (anomaly.kind, anomaly.action, anomaly.transaction_id) == (
        "unknown_transaction",
        "StopTransaction",
        77,
    )


async def test_meter_values_for_unknown_transaction_are_answered_and_recorded(
    csms: CsmsHarness,
) -> None:
    async with fake_charger(csms.url) as cp:
        await cp.boot()
        await cp.meter_values(1, _energy(1180), transaction_id=-1)

    charger = _charger(csms)
    assert charger.connectors[1].last_meter_value is not None
    [anomaly] = charger.anomalies
    assert (anomaly.action, anomaly.transaction_id) == ("MeterValues", -1)


# --- The CSMS does not depend on the order of statuses and transaction messages ---------


async def _session(cp: FakeChargePoint, transaction_first: bool) -> None:
    """One session. Our virtual charger reports each status before the transaction message;
    some real chargers do the opposite (StartTransaction before Charging, StopTransaction
    after Finishing)."""
    await cp.boot()
    await cp.status(1, S.preparing)

    async def start() -> int:
        started = await cp.start_transaction(1, ACCEPTED_TAG, meter_start=1000, timestamp=T_START)
        return started.transaction_id

    if transaction_first:
        transaction_id = await start()
        await cp.status(1, S.charging)
    else:
        await cp.status(1, S.charging)
        transaction_id = await start()
    await cp.meter_values(1, _energy(1180), transaction_id)

    async def stop() -> None:
        await cp.stop_transaction(transaction_id, 1500, T_STOP, Reason.local, ACCEPTED_TAG)

    if transaction_first:
        await cp.status(1, S.finishing)
        await stop()
    else:
        await stop()
        await cp.status(1, S.finishing)
    await cp.status(1, S.available)


def _comparable(state: ChargerState) -> dict[str, Any]:
    """The registry state, without what legitimately differs between two chargers."""
    return state.model_dump(
        exclude={"id": True, "finished_transactions": {"__all__": {"id"}}},
    )


async def test_registry_does_not_depend_on_message_order(csms: CsmsHarness) -> None:
    async with fake_charger(csms.url, "CP_STATUS_FIRST") as cp:
        await _session(cp, transaction_first=False)
    async with fake_charger(csms.url, "CP_TX_FIRST") as cp:
        await _session(cp, transaction_first=True)
    await wait_until(lambda: not _charger(csms, "CP_TX_FIRST").connected)

    status_first = _charger(csms, "CP_STATUS_FIRST")
    transaction_first = _charger(csms, "CP_TX_FIRST")
    assert _comparable(status_first) == _comparable(transaction_first)
    # And that common state is the expected one.
    assert status_first.connectors[1].status == S.available
    [finished] = status_first.finished_transactions
    assert (finished.energy_wh, finished.duration_s) == (500, 1800)
    assert status_first.anomalies == []
