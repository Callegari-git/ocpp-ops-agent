"""`python -m ocpp_ops.charger` as a real process against an in-process CSMS."""

import asyncio
import signal
import sys

from fakes import CsmsHarness, wait_until
from ocpp_ops.message_log import LogRecord


async def test_charger_cli_boots_and_stops_cleanly(csms: CsmsHarness) -> None:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "ocpp_ops.charger",
        "--id",
        "CP_CLI",
        "--url",
        csms.url,
        "--connectors",
        "2",
        stderr=asyncio.subprocess.PIPE,
    )
    assert process.stderr is not None
    try:
        await wait_until(
            lambda: (
                (state := csms.registry.get("CP_CLI")) is not None and len(state.connectors) == 3
            ),
            within=10,
        )
        process.send_signal(signal.SIGINT)  # what Ctrl+C sends
        async with asyncio.timeout(10):
            await process.wait()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()

    output = (await process.stderr.read()).decode()
    assert process.returncode == 0, output
    assert "Charger stopped" in output
    await wait_until(lambda: not csms.registry.all()[0].connected)
    records = [
        LogRecord.model_validate_json(line) for line in csms.log_path.read_text().splitlines()
    ]
    disconnected = [r for r in records if r.message_type == "DISCONNECTED"]
    # On Ctrl+C the charger closed its connection properly, with 1001 "going away".
    assert disconnected[-1].payload == {"reason": "clean", "close_code": 1001, "detail": None}
