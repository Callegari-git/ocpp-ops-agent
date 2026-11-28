"""The fleet as an operator sees it, and which chargers need attention.

No display code here on purpose: attention() is the detection half of the week-4
rule-based baseline, and must be reusable there as is. Everything is pure: same rows and
same `now`, same answer.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum

from ocpp.v16.enums import ChargePointErrorCode, ChargePointStatus

from ocpp_ops.charger.charger import VirtualCharger
from ocpp_ops.csms.registry import ChargerState, ConnectorState, Registry, power_w
from ocpp_ops.fleet.config import ChargerSpec

# A heartbeat is late once this many intervals have passed without one.
STALE_HEARTBEAT_INTERVALS = 2


@dataclass(frozen=True)
class ConnectorView:
    status: ChargePointStatus | None  # None: never reported
    error_code: ChargePointErrorCode | None = None


@dataclass(frozen=True)
class ChargerRow:
    """One charger, from whichever side we can see (CSMS registry or the charger itself)."""

    charger_id: str
    seen: bool  # False: never connected
    connected: bool
    booted: bool
    # Connector 0 (the charge point as a whole) and 1..N, every one present.
    connectors: dict[int, ConnectorView]
    boot_time: datetime | None
    last_heartbeat: datetime | None
    heartbeat_interval: int  # seconds; 0 means heartbeats are off
    active_sessions: int = 0  # transactions in progress
    power_w: float = 0.0  # being drawn now, all connectors together

    @property
    def online(self) -> bool:
        """Connected and booted: its statuses are current."""
        return self.connected and self.booted


# --- Building rows -----------------------------------------------------------------------


def rows_from_registry(
    specs: list[ChargerSpec], registry: Registry, heartbeat_interval: int
) -> list[ChargerRow]:
    """CSMS view (embedded CSMS). Takes the fleet's specs, not just ids, so a charger never
    seen yet still has its connectors. `heartbeat_interval` is what our CSMS sends at boot."""
    rows = []
    for spec in specs:
        state = registry.get(spec.id)
        expected = set(range(spec.connectors + 1))
        if state is None:
            connectors = {i: ConnectorView(None) for i in sorted(expected)}
            rows.append(
                ChargerRow(spec.id, False, False, False, connectors, None, None, heartbeat_interval)
            )
            continue
        connectors = {
            i: _from_registry(state.connectors.get(i))
            for i in sorted(expected | set(state.connectors))
        }
        # Our CSMS accepts every BootNotification, so a boot time means Accepted. Week 2
        # (Pending/Rejected faults) will need the boot status itself in the registry.
        booted = state.connected and state.boot_time is not None
        rows.append(
            ChargerRow(
                spec.id,
                True,
                state.connected,
                booted,
                connectors,
                state.boot_time,
                state.last_heartbeat,
                heartbeat_interval,
                len(state.active_transactions),
                _power_w(state),
            )
        )
    return rows


def _power_w(state: ChargerState) -> float:
    """From the last MeterValues of each transaction in progress: 0 until its first sample."""
    total = 0.0
    for transaction in state.active_transactions.values():
        reading = transaction.last_meter_value
        if reading is not None:
            total += power_w(reading) or 0.0
    return total


def _from_registry(connector: ConnectorState | None) -> ConnectorView:
    if connector is None:
        return ConnectorView(None)
    return ConnectorView(connector.status, connector.error_code)


def rows_from_chargers(chargers: list[VirtualCharger]) -> list[ChargerRow]:
    """Charger-side view (external CSMS, whose registry we can't read)."""
    rows = []
    for charger in chargers:
        connectors = {
            i: ConnectorView(charger.connector(i).status, charger.connector(i).error_code)
            for i in charger.connector_ids
        }
        rows.append(
            ChargerRow(
                charger.id,
                True,
                charger.connected,
                charger.booted,
                connectors,
                charger.boot_time,
                charger.last_heartbeat,
                # What the CSMS imposed at boot (or changed since).
                charger.config.heartbeat_interval,
                charger.active_transactions,
                charger.power_w,
            )
        )
    return rows


# --- Counting ----------------------------------------------------------------------------


def sessions_and_power(rows: list[ChargerRow]) -> tuple[int, float]:
    """Transactions in progress and the power they draw (W), on online chargers only, as
    in connector_counts()."""
    online = [row for row in rows if row.online]
    return sum(row.active_sessions for row in online), sum(row.power_w for row in online)


def connector_counts(rows: list[ChargerRow]) -> dict[ChargePointStatus, int]:
    """Statuses of connectors 1..N of online chargers. An offline charger's last known
    statuses may be stale, so they are not counted."""
    counts: dict[ChargePointStatus, int] = {}
    for row in rows:
        if not row.online:
            continue
        for connector_id, connector in row.connectors.items():
            if connector_id >= 1 and connector.status is not None:
                counts[connector.status] = counts.get(connector.status, 0) + 1
    return counts


# --- Attention ---------------------------------------------------------------------------


class Severity(IntEnum):
    """Lower is more severe."""

    OFFLINE = 1  # we can't see or reach it at all
    FAULTED = 2
    NOT_BOOTED = 3
    STALE_HEARTBEAT = 4


@dataclass(frozen=True)
class Reason:
    severity: Severity
    text: str


@dataclass(frozen=True)
class Issue:
    """One charger that needs attention, with every reason found."""

    charger_id: str
    reasons: tuple[Reason, ...]

    @property
    def severity(self) -> Severity:
        return min(reason.severity for reason in self.reasons)

    def describe(self) -> str:
        return "; ".join(reason.text for reason in self.reasons)


def attention(rows: list[ChargerRow], now: datetime) -> list[Issue]:
    """Chargers that need attention, most severe first, then by id."""
    issues = []
    for row in rows:
        reasons = _reasons(row, now)
        if reasons:
            issues.append(Issue(row.charger_id, tuple(reasons)))
    return sorted(issues, key=lambda issue: (issue.severity, issue.charger_id))


def _reasons(row: ChargerRow, now: datetime) -> list[Reason]:
    if not row.connected:
        text = "offline" if row.seen else "offline (never connected)"
        # Its last known statuses may be stale: nothing more can be said.
        return [Reason(Severity.OFFLINE, text)]
    if not row.booted:
        # Same: statuses from before this connection may be stale.
        return [Reason(Severity.NOT_BOOTED, "connected, not booted")]
    reasons = [
        Reason(Severity.FAULTED, _faulted_text(connector_id, connector))
        for connector_id, connector in row.connectors.items()
        if connector.status == ChargePointStatus.faulted
    ]
    stale = _stale_heartbeat(row, now)
    if stale is not None:
        reasons.append(stale)
    return reasons


def _faulted_text(connector_id: int, connector: ConnectorView) -> str:
    text = f"connector {connector_id} Faulted"
    if connector.error_code not in (None, ChargePointErrorCode.no_error):
        text += f" ({connector.error_code.value})"
    return text


def _stale_heartbeat(row: ChargerRow, now: datetime) -> Reason | None:
    """Late if more than 2 intervals since the last heartbeat, or since boot if none came
    after it. Without the boot fallback, a charger that never heartbeats would never show.
    The latest of the two counts: after a reboot, the last heartbeat is from before it."""
    if row.heartbeat_interval <= 0 or row.boot_time is None:
        return None
    if row.last_heartbeat is not None and row.last_heartbeat >= row.boot_time:
        reference, what = row.last_heartbeat, "last heartbeat"
    else:
        reference, what = row.boot_time, "no heartbeat since boot"
    age = (now - reference).total_seconds()
    if age <= STALE_HEARTBEAT_INTERVALS * row.heartbeat_interval:
        return None
    return Reason(
        Severity.STALE_HEARTBEAT,
        f"{what} {age:.0f} s ago (interval {row.heartbeat_interval} s)",
    )
