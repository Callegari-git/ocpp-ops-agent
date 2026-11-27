"""Fleet rows from both sides, connector counts, and the attention rules."""

import random

from ocpp.v16.enums import ChargePointErrorCode as E
from ocpp.v16.enums import ChargePointStatus as S

from fakes import FakeClock
from manual_time import ManualTime
from ocpp_ops.charger.charger import VirtualCharger
from ocpp_ops.csms.registry import Registry
from ocpp_ops.fleet.config import ChargerSpec
from ocpp_ops.fleet.status_rows import (
    ChargerRow,
    ConnectorView,
    Severity,
    attention,
    connector_counts,
    rows_from_chargers,
    rows_from_registry,
)
from status_helpers import INTERVAL, NOW, row


def _spec(charger_id: str, connectors: int = 1) -> ChargerSpec:
    return ChargerSpec(id=charger_id, vendor="V", model="M", connectors=connectors)


# --- Building rows -----------------------------------------------------------------------


def test_rows_from_registry() -> None:
    clock = FakeClock()
    registry = Registry(now=clock)
    registry.connect("CP-1")
    registry.record_boot("CP-1", "V", "M")
    registry.record_status("CP-1", 0, S.available, E.no_error)
    registry.record_status("CP-1", 1, S.faulted, E.ground_failure)
    registry.record_heartbeat("CP-1")

    rows = rows_from_registry([_spec("CP-1", 2), _spec("CP-2", 1)], registry, INTERVAL)

    assert rows == [
        ChargerRow(
            "CP-1",
            True,
            True,
            True,
            {
                0: ConnectorView(S.available, E.no_error),
                1: ConnectorView(S.faulted, E.ground_failure),
                2: ConnectorView(None),  # not reported yet
            },
            clock(),
            clock(),
            INTERVAL,
        ),
        # Never seen by the CSMS: still has its connectors, from the fleet file.
        ChargerRow(
            "CP-2", False, False, False, {0: ConnectorView(None), 1: ConnectorView(None)}, None,
            None, INTERVAL,
        ),
    ]  # fmt: skip


def test_rows_from_registry_disconnected_charger_is_not_booted() -> None:
    registry = Registry(now=FakeClock())
    token = registry.connect("CP-1")
    registry.record_boot("CP-1", "V", "M")
    registry.disconnect("CP-1", token)
    [charger] = rows_from_registry([_spec("CP-1")], registry, INTERVAL)
    assert (charger.seen, charger.connected, charger.booted) == (True, False, False)


def test_rows_from_chargers_before_connecting() -> None:
    time = ManualTime()
    charger = VirtualCharger(
        "CP-9", "ws://unused", 1, now=time, sleep=time.sleep, rng=random.Random(0)
    )
    [view] = rows_from_chargers([charger])
    assert view == ChargerRow(
        "CP-9",
        True,
        False,
        False,
        {0: ConnectorView(S.available, E.no_error), 1: ConnectorView(S.available, E.no_error)},
        None,
        None,
        charger.config.heartbeat_interval,
    )


def test_connector_counts_only_physical_connectors_of_online_chargers() -> None:
    rows = [
        row("A", (S.available, S.charging, S.available)),
        row("B", (S.faulted, S.faulted), errors={0: E.ground_failure}),
        row("C", (S.available, S.charging), connected=False),  # offline: possibly stale
        row("D", (S.available, None)),  # never reported
    ]
    assert connector_counts(rows) == {S.available: 1, S.charging: 1, S.faulted: 1}


# --- Attention ---------------------------------------------------------------------------


def _describe(rows: list[ChargerRow]) -> list[tuple[str, str]]:
    return [(issue.charger_id, issue.describe()) for issue in attention(rows, NOW)]


def test_healthy_fleet_needs_no_attention() -> None:
    assert attention([row("A"), row("B", heartbeat_ago=5)], NOW) == []


def test_each_reason() -> None:
    rows = [
        row("NEVER", seen=False, connected=False, booted=False),
        row("OFF", connected=False, booted=False),
        row("NOBOOT", booted=False),
        row("FAULT", (S.faulted, S.available, S.faulted), errors={0: E.high_temperature}),
        row("FAULT-NOCODE", (S.available, S.faulted)),
        row("STALE", heartbeat_ago=61),
        row("SILENT", boot_ago=100),  # booted, never heartbeated
    ]
    assert sorted(_describe(rows)) == [
        ("FAULT", "connector 0 Faulted (HighTemperature); connector 2 Faulted"),
        ("FAULT-NOCODE", "connector 1 Faulted"),
        ("NEVER", "offline (never connected)"),
        ("NOBOOT", "connected, not booted"),
        ("OFF", "offline"),
        ("SILENT", "no heartbeat since boot 100 s ago (interval 30 s)"),
        ("STALE", "last heartbeat 61 s ago (interval 30 s)"),
    ]


def test_stale_heartbeat_threshold_is_twice_the_interval() -> None:
    assert attention([row(heartbeat_ago=2 * INTERVAL)], NOW) == []
    assert attention([row(heartbeat_ago=2 * INTERVAL + 1)], NOW) != []
    assert attention([row(heartbeat_ago=1000, interval=0)], NOW) == []  # heartbeats off


def test_after_a_reboot_the_old_heartbeat_does_not_count() -> None:
    # Last heartbeat from before the reboot (1000 s ago), booted 10 s ago: not late.
    assert attention([row(boot_ago=10, heartbeat_ago=1000)], NOW) == []


def test_offline_or_not_booted_hides_stale_statuses() -> None:
    faulted = (S.faulted, S.faulted)
    assert _describe([row("A", faulted, connected=False), row("B", faulted, booted=False)]) == [
        ("A", "offline"),
        ("B", "connected, not booted"),
    ]


def test_most_severe_first_then_by_id() -> None:
    rows = [
        row("Z-STALE", heartbeat_ago=500),
        row("B-NOBOOT", booted=False),
        row("C-FAULT", (S.available, S.faulted), heartbeat_ago=500),  # faulted AND stale
        row("A-FAULT", (S.faulted, S.available)),
        row("Y-OFF", connected=False),
        row("X-OFF", connected=False),
    ]
    issues = attention(rows, NOW)
    assert [issue.charger_id for issue in issues] == [
        "X-OFF",
        "Y-OFF",
        "A-FAULT",
        "C-FAULT",
        "B-NOBOOT",
        "Z-STALE",
    ]
    assert [issue.severity for issue in issues] == [
        Severity.OFFLINE,
        Severity.OFFLINE,
        Severity.FAULTED,
        Severity.FAULTED,
        Severity.NOT_BOOTED,
        Severity.STALE_HEARTBEAT,
    ]
    # One line per charger, every reason kept.
    assert issues[3].describe() == ("connector 1 Faulted; last heartbeat 500 s ago (interval 30 s)")
