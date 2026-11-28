from datetime import UTC, datetime, timedelta

import pytest
from ocpp.v16.enums import AuthorizationStatus, ChargePointErrorCode, ChargePointStatus, Reason

from fakes import FakeClock
from ocpp_ops.csms.registry import (
    MAX_ANOMALIES,
    MAX_FINISHED_TRANSACTIONS,
    ChargerState,
    MeterReading,
    Registry,
    SampledValue,
    energy_register_wh,
)


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


# --- Transactions ----------------------------------------------------------------------

T0 = datetime(2026, 11, 24, 11, 0, tzinfo=UTC)  # charger clock


def _reading(*sampled: SampledValue) -> MeterReading:
    return MeterReading(timestamp=T0, sampled_values=list(sampled))


def _booted_registry() -> Registry:
    registry = Registry(FakeClock())
    registry.connect("CP_1")
    return registry


def _start(registry: Registry, connector_id: int = 1, meter_start: int = 1000) -> int:
    return registry.start_transaction(
        "CP_1",
        connector_id,
        id_tag="TAG",
        id_tag_status=AuthorizationStatus.accepted,
        meter_start=meter_start,
        started_at=T0,
    )


def _charger(registry: Registry) -> ChargerState:
    charger = registry.get("CP_1")
    assert charger is not None
    return charger


def test_transaction_ids_are_unique_and_positive_across_chargers() -> None:
    registry = _booted_registry()
    registry.connect("CP_2")
    first = _start(registry)
    second = registry.start_transaction(
        "CP_2", 1, "TAG", AuthorizationStatus.accepted, meter_start=0, started_at=T0
    )
    assert (first, second) == (1, 2)


def test_meter_values_update_the_energy_so_far() -> None:
    registry = _booted_registry()
    transaction_id = _start(registry, meter_start=1000)

    reading = _reading(
        SampledValue(value="1250", measurand="Energy.Active.Import.Register", unit="Wh"),
        SampledValue(value="11000", measurand="Power.Active.Import", unit="W"),
    )
    registry.record_meter_value("CP_1", 1, reading, transaction_id)

    transaction = _charger(registry).transaction_on(1)
    assert transaction is not None
    assert transaction.last_meter_value == reading
    assert transaction.energy_wh == 250
    assert _charger(registry).connectors[1].last_meter_value == reading


@pytest.mark.parametrize(
    ("sampled", "expected"),
    [
        (SampledValue(value="1500"), 1500.0),  # §7.43 defaults: energy register, in Wh
        (SampledValue(value="1.5", unit="kWh"), 1500.0),
        (SampledValue(value="7000", measurand="Power.Active.Import", unit="W"), None),
        (SampledValue(value="500", phase="L1"), None),  # per phase, not the overall register
        (SampledValue(value="abc", format="SignedData"), None),
    ],
)
def test_energy_register_reading(sampled: SampledValue, expected: float | None) -> None:
    assert energy_register_wh(_reading(sampled)) == expected


def test_stop_moves_the_transaction_to_the_history() -> None:
    registry = _booted_registry()
    transaction_id = _start(registry, meter_start=1000)

    registry.stop_transaction(
        "CP_1",
        transaction_id,
        meter_stop=1600,
        stopped_at=T0 + timedelta(minutes=30),
        reason=Reason.local,
    )

    charger = _charger(registry)
    assert charger.active_transactions == {}
    assert charger.transaction_on(1) is None
    [finished] = charger.finished_transactions
    assert (finished.id, finished.meter_stop, finished.energy_wh) == (transaction_id, 1600, 600)
    assert (finished.duration_s, finished.stop_reason) == (1800, Reason.local)
    assert charger.anomalies == []


def test_history_is_bounded() -> None:
    registry = _booted_registry()
    for _ in range(MAX_FINISHED_TRANSACTIONS + 5):
        transaction_id = _start(registry)
        registry.stop_transaction("CP_1", transaction_id, 1000, T0, Reason.local)

    history = _charger(registry).finished_transactions
    assert len(history) == MAX_FINISHED_TRANSACTIONS
    assert history[-1].id == MAX_FINISHED_TRANSACTIONS + 5  # the oldest ones went


def test_stop_of_unknown_transaction_is_an_anomaly() -> None:
    registry = _booted_registry()
    registry.stop_transaction("CP_1", 99, 1000, T0, Reason.local)

    [anomaly] = _charger(registry).anomalies
    assert (anomaly.kind, anomaly.action, anomaly.transaction_id) == (
        "unknown_transaction",
        "StopTransaction",
        99,
    )
    assert anomaly.detail == "not an active transaction of this charger"
    assert anomaly.at == FakeClock()()  # by our clock


def test_second_stop_is_an_anomaly_that_says_already_finished() -> None:
    registry = _booted_registry()
    transaction_id = _start(registry)
    registry.stop_transaction("CP_1", transaction_id, 1000, T0, Reason.local)
    registry.stop_transaction("CP_1", transaction_id, 1000, T0, Reason.local)

    [anomaly] = _charger(registry).anomalies
    assert anomaly.detail == "already finished"
    assert len(_charger(registry).finished_transactions) == 1


def test_meter_values_for_unknown_transaction_are_kept_and_flagged() -> None:
    registry = _booted_registry()
    reading = _reading(SampledValue(value="1500"))
    registry.record_meter_value("CP_1", 1, reading, transaction_id=-1)

    assert _charger(registry).connectors[1].last_meter_value == reading
    [anomaly] = _charger(registry).anomalies
    assert (anomaly.kind, anomaly.action) == ("unknown_transaction", "MeterValues")


def test_start_on_busy_connector_keeps_both_transactions() -> None:
    registry = _booted_registry()
    first = _start(registry)
    second = _start(registry)

    charger = _charger(registry)
    assert set(charger.active_transactions) == {first, second}
    transaction = charger.transaction_on(1)
    assert transaction is not None and transaction.id == second  # the most recent
    [anomaly] = charger.anomalies
    assert (anomaly.kind, anomaly.transaction_id) == ("start_on_busy_connector", first)


def test_anomalies_are_bounded() -> None:
    registry = _booted_registry()
    for transaction_id in range(MAX_ANOMALIES + 5):
        registry.stop_transaction("CP_1", transaction_id, 0, T0, Reason.local)

    anomalies = _charger(registry).anomalies
    assert len(anomalies) == MAX_ANOMALIES
    assert anomalies[-1].transaction_id == MAX_ANOMALIES + 4
