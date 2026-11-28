"""The compact live grid: layout, colors, attention list, and the refresh loop."""

import asyncio
import io
import os
import re
from contextlib import suppress

import pytest
from ocpp.v16.enums import ChargePointErrorCode as E
from ocpp.v16.enums import ChargePointStatus as S

from fakes import wait_until
from manual_time import ManualTime
from ocpp_ops.fleet.status_grid import (
    BLUE,
    GRAY,
    GREEN,
    RED,
    YELLOW,
    cells_per_line,
    grid,
    render,
    show_status,
    summary,
)
from ocpp_ops.fleet.status_rows import ChargerRow
from status_helpers import NOW, row

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _plain(text: str) -> str:
    return ANSI.sub("", text)


def _paint(text: str, code: str) -> str:
    return f"\x1b[{code}m{text}\x1b[0m"


def _fleet(count: int, connectors: int = 2) -> list[ChargerRow]:
    statuses = (S.available,) * (connectors + 1)
    return [row(f"VTX-AC-{n:03d}", statuses) for n in range(1, count + 1)]


# --- Layout --------------------------------------------------------------------------------


@pytest.mark.parametrize(("width", "expected"), [(100, 6), (46, 3), (30, 2), (29, 1), (10, 1)])
def test_cells_per_line(width: int, expected: int) -> None:
    # "VTX-AC-001 ●●●" is 14 columns wide, plus a 2-column gap between cells.
    assert cells_per_line(width, cell_width=14) == expected


def test_grid_wraps_to_the_width_and_cells_line_up() -> None:
    rows = _fleet(7, connectors=3)
    lines = grid(rows, width=46, color=False)
    assert lines == [
        "VTX-AC-001 AAA  VTX-AC-002 AAA  VTX-AC-003 AAA",
        "VTX-AC-004 AAA  VTX-AC-005 AAA  VTX-AC-006 AAA",
        "VTX-AC-007 AAA",
    ]
    # With colors, the visible text is the same width: codes take no room on screen.
    colored = grid(rows, width=46, color=True)
    assert [len(_plain(line)) for line in colored] == [len(line) for line in lines]


def test_cells_are_padded_to_the_longest_id_and_most_connectors() -> None:
    rows = [row("A", (S.available, S.available)), row("LONG-ID", (S.available,) * 4)]
    assert grid(rows, width=100, color=False) == ["A       A    LONG-ID AAA"]


def test_letters_for_each_status_without_colors() -> None:
    statuses = (S.available, S.available, S.charging, S.finishing, S.unavailable, S.faulted, None)
    assert grid([row("CP", statuses)], 100, color=False) == ["CP ABBUF-"]


def test_render_fits_100_by_30_with_50_chargers() -> None:
    lines = render(_fleet(50, connectors=3), NOW, width=100, color=True).splitlines()
    assert len(lines) <= 30
    assert max(len(_plain(line)) for line in lines) <= 100


# --- Colors --------------------------------------------------------------------------------


def test_connector_dot_colors() -> None:
    statuses = (S.available, S.available, S.preparing, S.suspended_ev, S.unavailable, S.faulted)
    [line] = grid([row("CP", statuses)], 100, color=True)
    assert line == "CP " + "".join(_paint("●", code) for code in (GREEN, BLUE, BLUE, YELLOW, RED))


def test_offline_or_not_booted_charger_is_all_gray() -> None:
    for offline in (row("CP", connected=False), row("CP", booted=False)):
        [line] = grid([offline], 100, color=True)
        assert line == _paint("CP", GRAY) + " " + _paint("●", GRAY) * 2


def test_connector_0_drives_the_id_color() -> None:
    faulted = row("CP", (S.faulted, S.available), errors={0: E.ground_failure})
    unavailable = row("CP", (S.unavailable, S.available))
    available = row("CP", (S.available, S.available))
    assert grid([faulted], 100, color=True)[0].startswith(_paint("CP", RED))
    assert grid([unavailable], 100, color=True)[0].startswith(_paint("CP", YELLOW))
    # Available: default color, no code before the id.
    assert grid([available], 100, color=True)[0].startswith("CP ")


def test_no_escape_codes_at_all_without_colors() -> None:
    rows = [row("A"), row("B", connected=False), row("C", (S.faulted, S.faulted))]
    assert "\x1b" not in render(rows, NOW, width=100, color=False)


# --- Summary and attention -----------------------------------------------------------------


def test_summary_line() -> None:
    rows = [
        row("A", (S.available, S.available, S.charging), sessions=1, power_w=7_400),
        row("B", (S.available, S.faulted), errors={1: E.ground_failure}),
        # Offline: its last known sessions may be stale, so they are not counted.
        row("C", connected=False, sessions=2, power_w=22_000),
        row("D", booted=False),
    ]
    assert summary(rows, NOW) == (
        "12:00:00 UTC  3/4 connected, 2 booted  |  1 session, 7.4 kW"
        "  |  connectors: 1 Available, 1 Charging, 1 Faulted"
    )
    two = [row("A", sessions=1, power_w=11_000), row("B", sessions=1, power_w=150_000)]
    assert "|  2 sessions, 161.0 kW  |" in summary(two, NOW)
    assert summary([row(connected=False)], NOW).endswith("connectors: none online")


def test_render_layout_with_attention_list() -> None:
    rows = [
        row("CP-1"),
        row("CP-2", (S.available, S.faulted, S.available), errors={1: E.ground_failure}),
        row("CP-3", connected=False),
    ]
    lines = render(rows, NOW, width=100, color=False).splitlines()
    assert lines[0].startswith("12:00:00 UTC  2/3 connected, 2 booted")
    assert lines[1].startswith("A Available  B In use")  # legend
    assert lines[2:] == [
        "",
        "CP-1 AA  CP-2 FA  CP-3 --",
        "",
        "Attention (2)",
        "  CP-3  offline",
        "  CP-2  connector 1 Faulted (GroundFailure)",
    ]


def test_attention_list_is_truncated_after_five() -> None:
    rows = [row(f"OFF-{n}", connected=False) for n in range(1, 9)]
    lines = render(rows, NOW, width=100, color=False).splitlines()
    start = lines.index("Attention (8)")
    assert lines[start + 1 :] == [
        "  OFF-1  offline",
        "  OFF-2  offline",
        "  OFF-3  offline",
        "  OFF-4  offline",
        "  OFF-5  offline",
        "  … 3 more",
    ]


def test_all_chargers_healthy() -> None:
    assert render(_fleet(3), NOW, 100, color=False).splitlines()[-1] == "All chargers healthy"
    assert render(_fleet(3), NOW, 100, color=True).splitlines()[-1] == _paint(
        "All chargers healthy", GREEN
    )


# --- Refresh loop --------------------------------------------------------------------------


class _Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


async def _run_refreshes(
    out: io.StringIO,
    rows: list[ChargerRow],
    refreshes: int,
    widths: list[int] | None = None,
    environ: dict[str, str] | None = None,
) -> ManualTime:
    time = ManualTime()
    sizes = iter(widths or [100] * (refreshes + 1))
    task = asyncio.create_task(
        show_status(
            lambda: rows,
            time,
            time.sleep,
            out,
            terminal_size=lambda: os.terminal_size((next(sizes), 30)),
            environ=environ or {},
        )
    )
    for _ in range(refreshes - 1):
        await wait_until(lambda: time.sleeper_count > 0)
        time.advance_to_next()
    await wait_until(lambda: time.sleeper_count > 0)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
    return time


async def test_pipe_gets_one_summary_line_per_refresh() -> None:
    out = io.StringIO()  # isatty() is False, like a pipe
    time = await _run_refreshes(out, [row("CP-1")], refreshes=3)
    assert out.getvalue().splitlines() == [
        "12:00:00 UTC  1/1 connected, 1 booted  |  0 sessions, 0.0 kW  |  connectors: 2 Available",
        "12:00:01 UTC  1/1 connected, 1 booted  |  0 sessions, 0.0 kW  |  connectors: 2 Available",
        "12:00:02 UTC  1/1 connected, 1 booted  |  0 sessions, 0.0 kW  |  connectors: 2 Available",
    ]
    assert time.requested == [1.0, 1.0, 1.0]


async def test_terminal_gets_the_grid_at_its_current_width() -> None:
    out = _Terminal()
    rows = _fleet(4)
    # The window shrinks between the two refreshes.
    time = await _run_refreshes(out, rows, refreshes=2, widths=[100, 28])
    text = out.getvalue()
    assert text.startswith("\x1b[?1049h\x1b[H")  # alternate screen, cursor home
    first, second = text.split("\x1b[H")[1:]
    assert "VTX-AC-001 ●●  VTX-AC-002 ●●  VTX-AC-003 ●●  VTX-AC-004 ●●" in _plain(first)
    assert "VTX-AC-001 ●●  VTX-AC-002 ●●\n" in _plain(second)
    assert f"\x1b[{GREEN}m" in text
    # On exit: back to the normal screen, with the last summary left behind.
    assert text.endswith("\x1b[?1049l" + summary(rows, time()) + "\n")


async def test_no_color_is_respected_on_a_terminal() -> None:
    out = _Terminal()
    await _run_refreshes(out, _fleet(2), refreshes=1, environ={"NO_COLOR": "1"})
    text = out.getvalue()
    assert "VTX-AC-001 AA" in text
    assert "●" not in text
    assert f"\x1b[{GREEN}m" not in text  # cursor codes yes, color codes no
