import random

import pytest
from ocpp.v16.enums import ConfigurationStatus, Measurand

from ocpp_ops.charger.charger import reconnect_delay
from ocpp_ops.charger.config import ConfigStore

# --- ConfigStore ---------------------------------------------------------------------


def test_get_all_keys_with_readonly_flags() -> None:
    known, unknown = ConfigStore(number_of_connectors=2).get()

    assert unknown == []
    assert known["NumberOfConnectors"].value == "2"
    assert known["NumberOfConnectors"].readonly
    assert known["SupportedFeatureProfiles"].readonly
    assert not known["HeartbeatInterval"].readonly
    assert not known["MeterValueSampleInterval"].readonly
    assert not known["ConnectionTimeOut"].readonly


def test_get_selected_keys_reports_unknown_ones() -> None:
    known, unknown = ConfigStore(2).get(["HeartbeatInterval", "NoSuchKey"])

    assert list(known) == ["HeartbeatInterval"]
    assert unknown == ["NoSuchKey"]


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        ("HeartbeatInterval", "30", ConfigurationStatus.accepted),
        ("NumberOfConnectors", "4", ConfigurationStatus.rejected),  # read-only
        ("NoSuchKey", "1", ConfigurationStatus.not_supported),
        ("HeartbeatInterval", "abc", ConfigurationStatus.rejected),  # not an integer
        ("HeartbeatInterval", "-5", ConfigurationStatus.rejected),  # negative
        ("LocalAuthorizeOffline", "maybe", ConfigurationStatus.rejected),  # not a boolean
        # §3.16.4: only measurands the charger supports, else the whole change is Rejected.
        ("MeterValuesSampledData", "Power.Active.Import", ConfigurationStatus.accepted),
        ("MeterValuesSampledData", "Power.Active.Import,Voltage", ConfigurationStatus.rejected),
        ("MeterValuesSampledData", "", ConfigurationStatus.rejected),
    ],
)
def test_change(key: str, value: str, expected: ConfigurationStatus) -> None:
    assert ConfigStore(2).change(key, value) == expected


def test_sampled_measurands_default_and_change() -> None:
    config = ConfigStore(2)
    # Our default adds the power to the spec's default (energy register only).
    assert config.sampled_measurands == [
        Measurand.energy_active_import_register,
        Measurand.power_active_import,
    ]
    status = config.change(
        "MeterValuesSampledData", " Power.Active.Import , Energy.Active.Import.Register"
    )
    assert status == ConfigurationStatus.accepted
    assert config.sampled_measurands == [
        Measurand.power_active_import,
        Measurand.energy_active_import_register,
    ]


def test_transaction_keys() -> None:
    config = ConfigStore(2)
    assert config.meter_value_sample_interval == 60
    assert config.transaction_message_attempts == 3
    assert config.transaction_message_retry_interval == 60


def test_accepted_change_is_stored_and_refused_one_is_not() -> None:
    store = ConfigStore(2)
    store.change("HeartbeatInterval", "30")
    store.change("HeartbeatInterval", "abc")
    store.change("NumberOfConnectors", "4")

    assert store.heartbeat_interval == 30
    assert store.get(["NumberOfConnectors"])[0]["NumberOfConnectors"].value == "2"


def test_booleans_are_stored_lowercase() -> None:
    store = ConfigStore(2)
    store.change("LocalAuthorizeOffline", "FALSE")
    assert store.get(["LocalAuthorizeOffline"])[0]["LocalAuthorizeOffline"].value == "false"


def test_set_heartbeat_interval_from_boot() -> None:
    store = ConfigStore(2)
    store.set_heartbeat_interval(42)
    assert store.heartbeat_interval == 42


# --- reconnect_delay -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("failures", "low", "high"),
    [(0, 0.5, 1.0), (1, 1.0, 2.0), (3, 4.0, 8.0), (6, 30.0, 60.0), (50, 30.0, 60.0)],
)
def test_reconnect_delay_grows_and_is_capped(failures: int, low: float, high: float) -> None:
    rng = random.Random(0)
    for _ in range(100):
        assert low <= reconnect_delay(failures, rng) <= high


def test_reconnect_delay_same_seed_same_delays() -> None:
    def delays(seed: int) -> list[float]:
        rng = random.Random(seed)
        return [reconnect_delay(n, rng) for n in range(8)]

    assert delays(7) == delays(7)
    assert delays(7) != delays(8)
