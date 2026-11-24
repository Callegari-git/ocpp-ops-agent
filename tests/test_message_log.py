import json
from collections import deque
from pathlib import Path

import pytest

from fakes import CsmsHarness, FakeClock, fake_charger
from ocpp_ops.message_log import LoggingConnection, LogRecord, MessageLog, parse_frame


def _read_log(path: Path) -> list[LogRecord]:
    return [LogRecord.model_validate_json(line) for line in path.read_text().splitlines()]


# --- parse_frame -------------------------------------------------------------


def test_parse_call() -> None:
    frame = parse_frame('[2, "id-1", "Heartbeat", {}]')
    assert (frame.message_type, frame.message_id, frame.action, frame.payload) == (
        "CALL",
        "id-1",
        "Heartbeat",
        {},
    )


def test_parse_call_result() -> None:
    frame = parse_frame('[3, "id-1", {"currentTime": "2026-11-24T12:00:00Z"}]')
    assert frame.message_type == "CALLRESULT"
    assert frame.action is None
    assert frame.payload == {"currentTime": "2026-11-24T12:00:00Z"}


def test_parse_call_error() -> None:
    frame = parse_frame('[4, "id-1", "NotImplemented", "no handler", {}]')
    assert frame.message_type == "CALLERROR"
    assert frame.payload == {
        "errorCode": "NotImplemented",
        "errorDescription": "no handler",
        "errorDetails": {},
    }


@pytest.mark.parametrize("raw", ["not json", "{}", "[]", '[9, "x"]', '[2, "x"]', "42"])
def test_parse_invalid_frame_keeps_raw_text(raw: str) -> None:
    frame = parse_frame(raw)
    assert frame.message_type == "INVALID"
    assert frame.payload == {"raw": raw}


# --- LoggingConnection, without a network ---------------------------------------


class ScriptedSocket:
    """Stands in for a websocket: recv() replays given frames, send() records them."""

    def __init__(self, incoming: list[str]) -> None:
        self.incoming = deque(incoming)
        self.sent: list[str] = []

    async def recv(self) -> str:
        return self.incoming.popleft()

    async def send(self, message: str) -> None:
        self.sent.append(message)


async def test_response_sent_gets_action_of_call_received(tmp_path: Path) -> None:
    socket = ScriptedSocket(['[2, "a", "Heartbeat", {}]'])
    with MessageLog(tmp_path / "log.jsonl") as log:
        connection = LoggingConnection(socket, "CP_1", log, FakeClock())
        await connection.recv()
        await connection.send('[4, "a", "InternalError", "boom", {}]')

    records = _read_log(tmp_path / "log.jsonl")
    assert [(r.direction, r.message_type, r.action) for r in records] == [
        ("in", "CALL", "Heartbeat"),
        ("out", "CALLERROR", "Heartbeat"),
    ]
    assert socket.sent == ['[4, "a", "InternalError", "boom", {}]']


async def test_response_received_gets_action_of_call_sent(tmp_path: Path) -> None:
    socket = ScriptedSocket(['[3, "b", {"status": "Accepted"}]'])
    with MessageLog(tmp_path / "log.jsonl") as log:
        connection = LoggingConnection(socket, "CP_1", log, FakeClock())
        await connection.send('[2, "b", "Reset", {"type": "Soft"}]')
        await connection.recv()

    records = _read_log(tmp_path / "log.jsonl")
    assert [(r.direction, r.message_type, r.action) for r in records] == [
        ("out", "CALL", "Reset"),
        ("in", "CALLRESULT", "Reset"),
    ]


async def test_response_with_unknown_id_has_no_action(tmp_path: Path) -> None:
    socket = ScriptedSocket(['[3, "never-sent", {}]'])
    with MessageLog(tmp_path / "log.jsonl") as log:
        await LoggingConnection(socket, "CP_1", log, FakeClock()).recv()

    assert _read_log(tmp_path / "log.jsonl")[0].action is None


# --- Against a real CSMS session ------------------------------------------------


async def test_log_has_both_directions_with_matching_ids_and_actions(csms: CsmsHarness) -> None:
    async with fake_charger(csms.url) as cp:
        await cp.boot()
        await cp.heartbeat()

    records = _read_log(csms.log_path)
    assert [(r.direction, r.message_type, r.action) for r in records] == [
        ("in", "CALL", "BootNotification"),
        ("out", "CALLRESULT", "BootNotification"),
        ("in", "CALL", "Heartbeat"),
        ("out", "CALLRESULT", "Heartbeat"),
    ]
    # Each request and its response share a message id; the two exchanges don't.
    assert records[0].message_id == records[1].message_id
    assert records[2].message_id == records[3].message_id
    assert records[0].message_id != records[2].message_id

    assert all(r.charger_id == "CP_1" for r in records)
    assert all(r.timestamp == csms.clock() for r in records)
    assert records[0].payload["chargePointVendor"] == "TestVendor"
    assert records[1].payload["status"] == "Accepted"


def test_log_lines_are_plain_json(tmp_path: Path) -> None:
    """Readers (the agent, jq) don't need our models to parse the file."""
    with MessageLog(tmp_path / "log.jsonl") as log:
        log.write(
            LogRecord(
                timestamp=FakeClock()(),
                charger_id="CP_1",
                direction="in",
                message_type="CALL",
                message_id="x",
                action="Heartbeat",
                payload={},
            )
        )

    line = json.loads((tmp_path / "log.jsonl").read_text())
    assert line == {
        "timestamp": "2026-11-24T12:00:00Z",
        "charger_id": "CP_1",
        "direction": "in",
        "message_type": "CALL",
        "message_id": "x",
        "action": "Heartbeat",
        "payload": {},
    }
