"""`python -m ocpp_ops.fleet` as a real process: embedded and external CSMS, clean Ctrl+C."""

import asyncio
import signal
import sys
from pathlib import Path

import pytest

from fakes import CsmsHarness, wait_until
from ocpp_ops.fleet.__main__ import main
from ocpp_ops.message_log import LogRecord

SMALL_FLEET = """
seed: 3
heartbeat_interval: 30
startup_jitter_seconds: 0.5
templates:
  - {id_prefix: CLI-, vendor: V, model: M, connectors: 2, count: 3}
"""


def _read_log(path: Path) -> list[LogRecord]:
    return [LogRecord.model_validate_json(line) for line in path.read_text().splitlines()]


async def _run_fleet_until(tmp_path: Path, extra_args: list[str], expected_line: str) -> str:
    """Start the fleet, wait for `expected_line` on stdout, send Ctrl+C. Returns stderr."""
    fleet_file = tmp_path / "fleet.yaml"
    fleet_file.write_text(SMALL_FLEET)
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "ocpp_ops.fleet",
        "--fleet",
        str(fleet_file),
        "--log-dir",
        str(tmp_path / "logs"),
        *extra_args,
        stdout=asyncio.subprocess.PIPE,  # not a terminal: one summary line per second
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stdout is not None and process.stderr is not None
    try:
        async with asyncio.timeout(15):
            while expected_line not in (await process.stdout.readline()).decode():
                pass
        process.send_signal(signal.SIGINT)  # what Ctrl+C sends
        async with asyncio.timeout(10):
            await process.wait()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    stderr = (await process.stderr.read()).decode()
    assert process.returncode == 0, stderr
    assert "Fleet stopped" in stderr
    return stderr


async def test_embedded_csms_then_ctrl_c(tmp_path: Path) -> None:
    await _run_fleet_until(tmp_path, ["--port", "0"], "3/3 connected, 3 booted")

    [log_file] = (tmp_path / "logs").glob("csms-*.jsonl")
    records = _read_log(log_file)
    disconnects = [r for r in records if r.message_type == "DISCONNECTED"]
    # Every charger closed its own connection properly (1001), before the server went away.
    assert sorted(r.charger_id for r in disconnects) == ["CLI-001", "CLI-002", "CLI-003"]
    assert all(
        r.payload == {"reason": "clean", "close_code": 1001, "detail": None} for r in disconnects
    )


async def test_external_csms(tmp_path: Path, csms: CsmsHarness) -> None:
    await _run_fleet_until(tmp_path, ["--csms-url", csms.url], "3/3 connected, 3 booted")

    assert [state.id for state in csms.registry.all()] == ["CLI-001", "CLI-002", "CLI-003"]
    await wait_until(lambda: not any(state.connected for state in csms.registry.all()))
    # With an external CSMS, the fleet logs the charger side itself.
    [log_file] = (tmp_path / "logs").glob("fleet-*.jsonl")
    actions = {(r.direction, r.action) for r in _read_log(log_file)}
    assert ("out", "BootNotification") in actions
    assert ("in", "BootNotification") in actions


def test_invalid_fleet_file_exits_with_the_error(tmp_path: Path) -> None:
    fleet_file = tmp_path / "fleet.yaml"
    fleet_file.write_text(SMALL_FLEET.replace("count: 3", "count: 0"))
    with pytest.raises(SystemExit) as exit_info:
        main(["--fleet", str(fleet_file)])
    message = str(exit_info.value.code)
    assert message.startswith("Invalid fleet file:")
    assert "templates.0.count: Input should be greater than or equal to 1" in message
