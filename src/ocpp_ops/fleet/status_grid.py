"""Live terminal view of the fleet: a compact grid, one cell per charger, one dot per connector.

Plain text and ANSI escape codes, no extra library. render() is pure (rows, time, width and
color in, text out); show_status() is the refresh loop around it.
"""

import os
import shutil
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import TextIO

from ocpp.v16.enums import ChargePointStatus as S

from ocpp_ops.charger.state_machine import IN_USE
from ocpp_ops.clock import Clock, Sleep
from ocpp_ops.fleet.status_rows import (
    ChargerRow,
    ConnectorView,
    Issue,
    attention,
    connector_counts,
    sessions_and_power,
)

GAP = "  "  # between grid cells
MAX_ATTENTION_LINES = 5
DOT = "●"

# ANSI SGR color codes (the basic 16 colors work in every terminal). 90 is "bright black",
# which terminals show as gray.
GREEN, BLUE, YELLOW, RED, GRAY = "32", "34", "33", "31", "90"
_RESET = "\x1b[0m"

# Order of the per-status counts in the summary line.
_STATUS_ORDER = [
    S.available,
    S.preparing,
    S.charging,
    S.suspended_ev,
    S.suspended_evse,
    S.finishing,
    S.reserved,
    S.unavailable,
    S.faulted,
]

# ANSI cursor/screen codes (VT100). The alternate screen is a separate buffer, as used by
# `top` or `less`: redraws don't flood the scrollback, and the shell comes back on exit.
_ENTER_ALT_SCREEN = "\x1b[?1049h"
_LEAVE_ALT_SCREEN = "\x1b[?1049l"
_CURSOR_HOME = "\x1b[H"
_CLEAR_TO_LINE_END = "\x1b[K"
_CLEAR_TO_SCREEN_END = "\x1b[J"


# --- Pure rendering ------------------------------------------------------------------------


def render(rows: list[ChargerRow], now: datetime, width: int, color: bool) -> str:
    """Summary, legend, grid, then the Attention list."""
    lines = [summary(rows, now), legend(color), ""]
    lines += grid(rows, width, color)
    lines.append("")
    lines += attention_lines(attention(rows, now), color)
    return "\n".join(lines)


def summary(rows: list[ChargerRow], now: datetime) -> str:
    connected = sum(row.connected for row in rows)
    booted = sum(row.online for row in rows)
    counts = connector_counts(rows)
    statuses = ", ".join(f"{counts[s]} {s.value}" for s in _STATUS_ORDER if counts.get(s))
    sessions, watts = sessions_and_power(rows)
    return (
        f"{now:%H:%M:%S} UTC  {connected}/{len(rows)} connected, {booted} booted"
        f"  |  {sessions} session{'' if sessions == 1 else 's'}, {watts / 1000:.1f} kW"
        f"  |  connectors: {statuses or 'none online'}"
    )


def legend(color: bool) -> str:
    entries = [
        (GREEN, "A", "Available"),
        (BLUE, "B", "In use (Preparing…Finishing)"),
        (YELLOW, "U", "Unavailable"),
        (RED, "F", "Faulted"),
        (GRAY, "-", "Offline / not booted"),
    ]
    return GAP.join(
        f"{_paint(DOT, code) if color else letter} {label}" for code, letter, label in entries
    )


def cells_per_line(width: int, cell_width: int) -> int:
    """As many cells as fit in `width` columns, GAP between them; always at least one."""
    return max(1, (width + len(GAP)) // (cell_width + len(GAP)))


def grid(rows: list[ChargerRow], width: int, color: bool) -> list[str]:
    if not rows:
        return []
    # Every cell has the same width, so the columns line up. Widths are counted on the
    # plain text: color codes take no room on screen.
    id_width = max(len(row.charger_id) for row in rows)
    marks_width = max(len(_physical(row)) for row in rows)
    per_line = cells_per_line(width, id_width + 1 + marks_width)
    cells = [_cell(row, id_width, marks_width, color) for row in rows]
    return [GAP.join(cells[i : i + per_line]) for i in range(0, len(cells), per_line)]


def attention_lines(issues: list[Issue], color: bool) -> list[str]:
    if not issues:
        return [_paint("All chargers healthy", GREEN) if color else "All chargers healthy"]
    id_width = max(len(issue.charger_id) for issue in issues)
    lines = [f"Attention ({len(issues)})"]
    for issue in issues[:MAX_ATTENTION_LINES]:
        lines.append(f"  {issue.charger_id:<{id_width}}  {issue.describe()}")
    if len(issues) > MAX_ATTENTION_LINES:
        lines.append(f"  … {len(issues) - MAX_ATTENTION_LINES} more")
    return lines


def _cell(row: ChargerRow, id_width: int, marks_width: int, color: bool) -> str:
    """e.g. "VTX-AC-001 ●●", padded to the common width."""
    marks = []
    for connector in _physical(row):
        code, letter = connector_style(row, connector)
        marks.append(_paint(DOT, code) if color else letter)
    id_code = id_color(row)
    charger_id = _paint(row.charger_id, id_code) if color and id_code else row.charger_id
    id_padding = " " * (id_width - len(row.charger_id))
    marks_padding = " " * (marks_width - len(marks))
    return f"{charger_id}{id_padding} {''.join(marks)}{marks_padding}"


def _physical(row: ChargerRow) -> list[ConnectorView]:
    """Connectors 1..N, in order (connector 0 is shown through the id's color)."""
    return [connector for i, connector in sorted(row.connectors.items()) if i >= 1]


def connector_style(row: ChargerRow, connector: ConnectorView) -> tuple[str, str]:
    """(color code, letter used when colors are off) for one connector's dot."""
    if not row.online or connector.status is None:
        return GRAY, "-"
    if connector.status == S.available:
        return GREEN, "A"
    if connector.status in IN_USE:
        return BLUE, "B"
    if connector.status == S.unavailable:
        return YELLOW, "U"
    if connector.status == S.faulted:
        return RED, "F"
    return GRAY, "?"  # Reserved: reservations are out of scope


def id_color(row: ChargerRow) -> str | None:
    """Color of the charger id, from connector 0. None (default color) when Available: a grid
    of green ids would be hard to read."""
    if not row.online:
        return GRAY
    status = row.connectors[0].status if 0 in row.connectors else None
    if status == S.faulted:
        return RED
    if status == S.unavailable:
        return YELLOW
    return None


def _paint(text: str, code: str) -> str:
    return f"\x1b[{code}m{text}{_RESET}"


# --- Refresh loop ----------------------------------------------------------------------------


def use_color(out: TextIO, environ: Mapping[str, str]) -> bool:
    """Colors only on a terminal, and never when NO_COLOR is set (https://no-color.org)."""
    return out.isatty() and not environ.get("NO_COLOR")


async def show_status(
    rows: Callable[[], list[ChargerRow]],
    now: Clock,
    sleep: Sleep,
    out: TextIO,
    refresh_seconds: float = 1.0,
    terminal_size: Callable[[], os.terminal_size] = shutil.get_terminal_size,
    environ: Mapping[str, str] = os.environ,
) -> None:
    """Redraw every `refresh_seconds` until cancelled.

    On a terminal, the grid is redrawn in place, at the terminal's current width (read at
    every refresh, so resizing the window works). Otherwise (pipe, CI, log file) only the
    summary line is printed, once per refresh, which stays readable as plain lines.
    """
    interactive = out.isatty()
    color = use_color(out, environ)
    if interactive:
        out.write(_ENTER_ALT_SCREEN)
    try:
        while True:
            if interactive:
                screen = render(rows(), now(), terminal_size().columns, color)
                # Overwrite in place (home, clear each line's leftover) rather than clearing
                # the screen first: no flicker.
                screen = screen.replace("\n", _CLEAR_TO_LINE_END + "\n")
                out.write(_CURSOR_HOME + screen + _CLEAR_TO_LINE_END + _CLEAR_TO_SCREEN_END)
            else:
                out.write(summary(rows(), now()) + "\n")
            out.flush()
            await sleep(refresh_seconds)
    finally:
        if interactive:
            # Back to the normal screen, leaving the last summary in the scrollback.
            out.write(_LEAVE_ALT_SCREEN + summary(rows(), now()) + "\n")
            out.flush()
