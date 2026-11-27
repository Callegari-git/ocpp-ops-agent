"""Live terminal table of the fleet: plain text and ANSI escape codes, no extra library."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TextIO

from ocpp.v16.enums import ChargePointErrorCode, ChargePointStatus

from ocpp_ops.charger.charger import VirtualCharger
from ocpp_ops.clock import Clock, Sleep
from ocpp_ops.csms.registry import Registry

# ANSI escape codes (VT100). The alternate screen is a separate buffer, as used by `top`
# or `less`: redraws don't flood the scrollback, and the shell comes back intact on exit.
_ENTER_ALT_SCREEN = "\x1b[?1049h"
_LEAVE_ALT_SCREEN = "\x1b[?1049l"
_CURSOR_HOME = "\x1b[H"
_CLEAR_TO_LINE_END = "\x1b[K"
_CLEAR_TO_SCREEN_END = "\x1b[J"


@dataclass(frozen=True)
class ChargerRow:
    """One line of the table, from whichever side we can see (CSMS or charger)."""

    charger_id: str
    seen: bool  # False: never connected to the CSMS yet
    connected: bool
    booted: bool
    connectors: dict[int, str]  # connector id -> status, e.g. "Faulted(GroundFailure)"
    last_heartbeat: datetime | None


def rows_from_registry(charger_ids: list[str], registry: Registry) -> list[ChargerRow]:
    """CSMS view (embedded CSMS). Chargers from the fleet file not seen yet are listed too."""
    rows = []
    for charger_id in charger_ids:
        state = registry.get(charger_id)
        if state is None:
            rows.append(ChargerRow(charger_id, False, False, False, {}, None))
            continue
        connectors = {
            connector_id: _status_label(connector.status, connector.error_code)
            for connector_id, connector in sorted(state.connectors.items())
        }
        # Our CSMS accepts every BootNotification, so a boot time means Accepted. Week 2
        # (Pending/Rejected faults) will need the boot status itself in the registry.
        booted = state.connected and state.boot_time is not None
        rows.append(
            ChargerRow(charger_id, True, state.connected, booted, connectors, state.last_heartbeat)
        )
    return rows


def rows_from_chargers(chargers: list[VirtualCharger]) -> list[ChargerRow]:
    """Charger-side view (external CSMS, whose registry we can't read)."""
    rows = []
    for charger in chargers:
        connectors = {
            i: _status_label(charger.connector(i).status, charger.connector(i).error_code)
            for i in charger.connector_ids
        }
        rows.append(
            ChargerRow(
                charger.id,
                True,
                charger.connected,
                charger.booted,
                connectors,
                charger.last_heartbeat,
            )
        )
    return rows


def _status_label(status: ChargePointStatus | None, error_code: ChargePointErrorCode | None) -> str:
    label = status.value if status is not None else "?"
    if error_code not in (None, ChargePointErrorCode.no_error):
        label += f"({error_code.value})"
    return label


def summary(rows: list[ChargerRow], now: datetime) -> str:
    connected = sum(row.connected for row in rows)
    booted = sum(row.booted for row in rows)
    return f"{now:%H:%M:%S} UTC  {connected}/{len(rows)} connected, {booted} booted"


def render(rows: list[ChargerRow], now: datetime) -> str:
    """The whole table, header first and summary last, as plain text."""
    id_width = max([len("CHARGER")] + [len(row.charger_id) for row in rows])
    lines = [f"{'CHARGER':<{id_width}}  CONN  BOOT      HB AGO  CONNECTORS"]
    for row in rows:
        lines.append(
            f"{row.charger_id:<{id_width}}  {'yes' if row.connected else 'no':<4}  "
            f"{'Accepted' if row.booted else '-':<8}  "
            f"{_heartbeat_age(row.last_heartbeat, now):>6}  {_connectors(row)}"
        )
    lines.append("")
    lines.append(summary(rows, now))
    return "\n".join(lines)


def _heartbeat_age(last_heartbeat: datetime | None, now: datetime) -> str:
    if last_heartbeat is None:
        return "-"
    return f"{max(0, int((now - last_heartbeat).total_seconds()))}s"


def _connectors(row: ChargerRow) -> str:
    if not row.seen:
        return "not seen"
    return " ".join(f"{i}:{label}" for i, label in row.connectors.items())


async def show_status(
    rows: Callable[[], list[ChargerRow]],
    now: Clock,
    sleep: Sleep,
    out: TextIO,
    refresh_seconds: float = 1.0,
) -> None:
    """Redraw the table every `refresh_seconds` until cancelled.

    On a terminal, the full table is redrawn in place. Otherwise (pipe, CI, log file) only
    the summary line is printed, once per refresh, which stays readable as plain lines.
    """
    interactive = out.isatty()
    if interactive:
        out.write(_ENTER_ALT_SCREEN)
    try:
        while True:
            if interactive:
                # Overwrite in place (home, clear each line's leftover) rather than clearing
                # the screen first: no flicker.
                table = render(rows(), now()).replace("\n", _CLEAR_TO_LINE_END + "\n")
                out.write(_CURSOR_HOME + table + _CLEAR_TO_LINE_END + _CLEAR_TO_SCREEN_END)
            else:
                out.write(summary(rows(), now()) + "\n")
            out.flush()
            await sleep(refresh_seconds)
    finally:
        if interactive:
            # Back to the normal screen, leaving the last summary in the scrollback.
            out.write(_LEAVE_ALT_SCREEN + summary(rows(), now()) + "\n")
            out.flush()
