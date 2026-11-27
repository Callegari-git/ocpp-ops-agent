"""Build ChargerRows for status tests without a CSMS or a charger."""

from datetime import UTC, datetime, timedelta
from typing import Literal

from ocpp.v16.enums import ChargePointErrorCode as E
from ocpp.v16.enums import ChargePointStatus as S

from ocpp_ops.fleet.status_rows import ChargerRow, ConnectorView

NOW = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)
INTERVAL = 30


def row(
    charger_id: str = "CP-1",
    statuses: tuple[S | None, ...] = (S.available, S.available, S.available),
    *,
    seen: bool = True,
    connected: bool = True,
    booted: bool = True,
    errors: dict[int, E] | None = None,
    boot_ago: float | None | Literal["auto"] = "auto",
    heartbeat_ago: float | None = None,
    interval: int = INTERVAL,
) -> ChargerRow:
    """`statuses[i]` is connector i's status (connector 0 first). Times are seconds before NOW.

    By default the boot was 10 s ago, or just before the last heartbeat if that is older.
    """
    if boot_ago == "auto":
        boot_ago = 10 if heartbeat_ago is None else max(10, heartbeat_ago + 5)
    errors = errors or {}
    connectors = {
        i: ConnectorView(status, errors.get(i, E.no_error if status else None))
        for i, status in enumerate(statuses)
    }
    return ChargerRow(
        charger_id=charger_id,
        seen=seen,
        connected=connected,
        booted=booted,
        connectors=connectors,
        boot_time=_ago(boot_ago),
        last_heartbeat=_ago(heartbeat_ago),
        heartbeat_interval=interval,
    )


def _ago(seconds: float | None) -> datetime | None:
    return None if seconds is None else NOW - timedelta(seconds=seconds)
