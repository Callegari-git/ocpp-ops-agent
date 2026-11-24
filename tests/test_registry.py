from datetime import UTC, datetime

from ocpp.v16.enums import ChargePointErrorCode, ChargePointStatus

from fakes import FakeClock
from ocpp_ops.csms.registry import MeterReading, Registry, SampledValue


def test_connect_creates_charger() -> None:
    registry = Registry(FakeClock())
    registry.connect("CP_1")

    charger = registry.get("CP_1")
    assert charger is not None
    assert charger.connected
    assert charger.vendor is None


def test_disconnect_marks_charger_offline() -> None:
    registry = Registry(FakeClock())
    token = registry.connect("CP_1")
    registry.disconnect("CP_1", token)

    charger = registry.get("CP_1")
    assert charger is not None
    assert not charger.connected


def test_stale_disconnect_is_ignored() -> None:
    registry = Registry(FakeClock())
    old = registry.connect("CP_1")
    registry.connect("CP_1")  # reconnect before the old connection is closed
    registry.disconnect("CP_1", old)

    charger = registry.get("CP_1")
    assert charger is not None
    assert charger.connected


def test_heartbeat_uses_injected_clock() -> None:
    clock = FakeClock()
    registry = Registry(clock)
    registry.connect("CP_1")
    clock.advance(90)

    returned = registry.record_heartbeat("CP_1")

    charger = registry.get("CP_1")
    assert charger is not None
    assert charger.last_heartbeat == returned == clock()


def test_connector_zero_is_kept_apart_from_connector_one() -> None:
    registry = Registry(FakeClock())
    registry.connect("CP_1")
    registry.record_status(
        "CP_1", 0, ChargePointStatus.faulted, ChargePointErrorCode.high_temperature
    )
    registry.record_status("CP_1", 1, ChargePointStatus.available, ChargePointErrorCode.no_error)

    charger = registry.get("CP_1")
    assert charger is not None
    assert charger.connectors[0].status == ChargePointStatus.faulted
    assert charger.connectors[1].status == ChargePointStatus.available


def test_meter_value_keeps_connector_status() -> None:
    registry = Registry(FakeClock())
    registry.connect("CP_1")
    registry.record_status("CP_1", 1, ChargePointStatus.charging, ChargePointErrorCode.no_error)
    reading = MeterReading(
        timestamp=datetime(2026, 11, 24, 12, 0, tzinfo=UTC),
        sampled_values=[SampledValue(value="100")],
    )
    registry.record_meter_value("CP_1", 1, reading)

    charger = registry.get("CP_1")
    assert charger is not None
    assert charger.connectors[1].status == ChargePointStatus.charging
    assert charger.connectors[1].last_meter_value == reading


def test_all_is_sorted_by_id() -> None:
    registry = Registry(FakeClock())
    for charger_id in ["CP_3", "CP_1", "CP_2"]:
        registry.connect(charger_id)

    assert [c.id for c in registry.all()] == ["CP_1", "CP_2", "CP_3"]
