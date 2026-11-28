"""`python -m ocpp_ops.csms` as a real process: it serves, and Ctrl+C shuts it down cleanly."""

import asyncio
import re
import signal
import sys
from pathlib import Path

import pytest

from fakes import DEFAULT_ID_TAGS, fake_charger
from ocpp_ops.csms.__main__ import main
from ocpp_ops.message_log import LogRecord


async def _wait_for_port(stderr: asyncio.StreamReader) -> int:
    """Read the CSMS's log output until it says which port it listens on."""
    async with asyncio.timeout(10):
        while line := (await stderr.readline()).decode():
            if match := re.search(r"listening on ws://[\w.]+:(\d+)", line):
                return int(match.group(1))
    raise AssertionError("CSMS exited before listening")


async def test_ctrl_c_closes_server_and_log(tmp_path: Path) -> None:
    log_file = tmp_path / "csms.jsonl"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "ocpp_ops.csms",
        "--port",
        "0",
        "--log-file",
        str(log_file),
        "--id-tags",
        str(DEFAULT_ID_TAGS),
        cwd=tmp_path,
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stderr is not None
    try:
        port = await _wait_for_port(process.stderr)
        async with fake_charger(f"ws://127.0.0.1:{port}") as cp:
            await cp.boot()
            process.send_signal(signal.SIGINT)  # what Ctrl+C sends
            async with asyncio.timeout(10):
                await process.wait()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()

    remaining_output = (await process.stderr.read()).decode()
    assert process.returncode == 0, remaining_output
    assert "CSMS stopped" in remaining_output
    records = [LogRecord.model_validate_json(line) for line in log_file.read_text().splitlines()]
    assert [(r.message_type, r.action) for r in records] == [
        ("CONNECTED", None),
        ("CALL", "BootNotification"),
        ("CALLRESULT", "BootNotification"),
        ("DISCONNECTED", None),
    ]
    # The server closed the connection on shutdown with 1001 (going away).
    assert records[-1].payload["reason"] == "clean"
    assert records[-1].payload["close_code"] == 1001


def test_missing_id_tags_file_exits_with_the_error(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--id-tags", str(tmp_path / "nope.yaml")])
    message = str(exit_info.value.code)
    assert message.startswith("Invalid authorization file:")
    assert "nope.yaml" in message
