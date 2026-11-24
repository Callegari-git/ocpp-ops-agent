from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from fakes import HEARTBEAT_INTERVAL, CsmsHarness, FakeClock, RecordingRegistry
from ocpp_ops.csms.server import start_server
from ocpp_ops.message_log import MessageLog


@pytest.fixture
async def csms(tmp_path: Path) -> AsyncIterator[CsmsHarness]:
    """A real CSMS on a free port, with a fake clock and its log in tmp_path."""
    clock = FakeClock()
    registry = RecordingRegistry(clock)
    log_path = tmp_path / "messages.jsonl"
    with MessageLog(log_path) as log:
        server = await start_server(
            "127.0.0.1", 0, registry, log, heartbeat_interval=HEARTBEAT_INTERVAL, now=clock
        )
        async with server:
            port = server.sockets[0].getsockname()[1]
            yield CsmsHarness(f"ws://127.0.0.1:{port}", registry, clock, log_path)
