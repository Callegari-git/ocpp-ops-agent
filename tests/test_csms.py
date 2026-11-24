"""CSMS end to end: a real server on a free port, a fake charger over a real WebSocket."""

from datetime import UTC, datetime

import pytest
from ocpp.v16.datatypes import MeterValue, SampledValue
from ocpp.v16.enums import ChargePointErrorCode, ChargePointStatus, Measurand, RegistrationStatus
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus
from websockets.typing import Subprotocol

from fakes import HEARTBEAT_INTERVAL, OCPP16, CsmsHarness, fake_charger, wait_until
from ocpp_ops.csms.registry import ChargerState
from ocpp_ops.csms.server import charger_id_from_path


def _charger(csms: CsmsHarness, charger_id: str = "CP_1") -> ChargerState:
    charger = csms.registry.get(charger_id)
    assert charger is not None, f"{charger_id} not in registry"
    return charger


async def test_boot_is_accepted_and_charger_registered(csms: CsmsHarness) -> None:
    async with fake_charger(csms.url) as cp:
        response = await cp.boot()

        assert response.status == RegistrationStatus.accepted
        assert response.interval == HEARTBEAT_INTERVAL
        assert datetime.fromisoformat(response.current_time) == csms.clock()
        charger = _charger(csms)
        assert charger.connected
        assert (charger.vendor, charger.model) == ("TestVendor", "TestModel")
        assert charger.boot_time == csms.clock()


async def test_heartbeat_updates_last_heartbeat(csms: CsmsHarness) -> None:
    async with fake_charger(csms.url) as cp:
        await cp.boot()
        csms.clock.advance(HEARTBEAT_INTERVAL)

        response = await cp.heartbeat()

        assert _charger(csms).last_heartbeat == csms.clock()
        assert datetime.fromisoformat(response.current_time) == csms.clock()


async def test_status_notification_on_connector_1(csms: CsmsHarness) -> None:
    async with fake_charger(csms.url) as cp:
        await cp.boot()
        await cp.status(1, ChargePointStatus.charging, timestamp="2026-11-24T11:59:58+00:00")

        connector = _charger(csms).connectors[1]
        assert connector.status == ChargePointStatus.charging
        assert connector.error_code == ChargePointErrorCode.no_error
        assert connector.timestamp == datetime(2026, 11, 24, 11, 59, 58, tzinfo=UTC)
        assert connector.received_at == csms.clock()


async def test_status_notification_on_connector_0_is_the_whole_charger(
    csms: CsmsHarness,
) -> None:
    async with fake_charger(csms.url) as cp:
        await cp.boot()
        await cp.status(
            0,
            ChargePointStatus.faulted,
            ChargePointErrorCode.high_temperature,
            info="cabinet 85C",
        )

        charger = _charger(csms)
        assert set(charger.connectors) == {0}
        assert charger.connectors[0].status == ChargePointStatus.faulted
        assert charger.connectors[0].error_code == ChargePointErrorCode.high_temperature
        assert charger.connectors[0].info == "cabinet 85C"
        assert charger.connectors[0].timestamp is None  # optional; the charger didn't send it


async def test_meter_values_keep_the_latest_reading(csms: CsmsHarness) -> None:
    def reading(time: str, wh: str) -> MeterValue:
        sample = SampledValue(value=wh, measurand=Measurand.energy_active_import_register)
        return MeterValue(timestamp=time, sampled_value=[sample])

    async with fake_charger(csms.url) as cp:
        await cp.boot()
        await cp.meter_values(
            1,
            [
                reading("2026-11-24T12:00:30+00:00", "1500"),
                reading("2026-11-24T12:00:00+00:00", "1000"),
            ],
        )

        last = _charger(csms).connectors[1].last_meter_value
        assert last is not None
        assert last.timestamp == datetime(2026, 11, 24, 12, 0, 30, tzinfo=UTC)
        assert last.sampled_values[0].value == "1500"
        assert last.sampled_values[0].measurand == "Energy.Active.Import.Register"


@pytest.mark.parametrize(
    "subprotocols",
    [None, [Subprotocol("ocpp2.0.1")]],
    ids=["missing", "wrong"],
)
async def test_connection_without_ocpp16_subprotocol_is_rejected(
    csms: CsmsHarness, subprotocols: list[Subprotocol] | None
) -> None:
    with pytest.raises(InvalidStatus) as rejected:
        async with connect(f"{csms.url}/CP_1", subprotocols=subprotocols):
            pass

    assert rejected.value.response.status_code == 400
    assert csms.registry.get("CP_1") is None


async def test_connection_without_charger_id_is_rejected(csms: CsmsHarness) -> None:
    with pytest.raises(InvalidStatus) as rejected:
        async with connect(f"{csms.url}/", subprotocols=[OCPP16]):
            pass

    assert rejected.value.response.status_code == 400
    assert csms.registry.all() == []


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/CP_1", "CP_1"),
        ("/ocpp/CP_1", "CP_1"),
        ("/ocpp/CP_1/", "CP_1"),
        ("/CP%201?x=1", "CP 1"),
        ("/", ""),
    ],
)
def test_charger_id_from_path(path: str, expected: str) -> None:
    assert charger_id_from_path(path) == expected


async def test_clean_close_marks_charger_disconnected(csms: CsmsHarness) -> None:
    async with fake_charger(csms.url) as cp:
        await cp.boot()
        await cp.websocket.close()

    await wait_until(lambda: not _charger(csms).connected)


async def test_dropped_connection_marks_charger_disconnected(csms: CsmsHarness) -> None:
    async with fake_charger(csms.url) as cp:
        await cp.boot()
        # Kill the TCP connection without a WebSocket close frame, like a network drop.
        cp.websocket.transport.abort()

    await wait_until(lambda: not _charger(csms).connected)


async def test_closing_an_old_connection_keeps_the_new_one_connected(csms: CsmsHarness) -> None:
    async with fake_charger(csms.url, "CP_1") as old, fake_charger(csms.url, "CP_1") as new:
        # A round trip on each guarantees both server-side handlers are running.
        await old.boot()
        await new.boot()

        await old.websocket.close()
        await wait_until(lambda: len(csms.registry.disconnect_calls) == 1)

        assert _charger(csms).connected
        await new.heartbeat()  # and the new connection still works

    await wait_until(lambda: not _charger(csms).connected)
