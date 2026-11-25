import json
from collections import deque
from datetime import timedelta
from pathlib import Path

import pytest
from ocpp.v16.enums import ResetType

from fakes import CsmsHarness, FakeClock, fake_charger, wait_until
from ocpp_ops.message_log import LoggingConnection, LogRecord, MessageLog, parse_frame

PENDING_TTL = timedelta(seconds=60)


def _read_log(path: Path) -> list[LogRecord]:
    return [LogRecord.model_validate_json(line) for line in path.read_text().splitlines()]


def _frames(records: list[LogRecord]) -> list[LogRecord]:
    return [r for r in records if r.direction is not None]


def _events(records: list[LogRecord]) -> list[LogRecord]:
    return [r for r in records if r.message_type in ("CONNECTED", "DISCONNECTED")]


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
        connection = LoggingConnection(socket, "CP_1", log, FakeClock(), PENDING_TTL)
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
        connection = LoggingConnection(socket, "CP_1", log, FakeClock(), PENDING_TTL)
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
        await LoggingConnection(socket, "CP_1", log, FakeClock(), PENDING_TTL).recv()

    assert _read_log(tmp_path / "log.jsonl")[0].action is None


# --- Against a real CSMS session ------------------------------------------------


async def test_log_has_both_directions_with_matching_ids_and_actions(csms: CsmsHarness) -> None:
    async with fake_charger(csms.url) as cp:
        await cp.boot()
        await cp.heartbeat()

    records = _frames(_read_log(csms.log_path))
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


async def test_outbound_command_is_logged_with_its_action(csms: CsmsHarness) -> None:
    async with fake_charger(csms.url) as cp:
        await cp.boot()
        await csms.central_system.reset("CP_1", ResetType.soft)

    records = _frames(_read_log(csms.log_path))[2:]  # skip the BootNotification exchange
    assert [(r.direction, r.message_type, r.action) for r in records] == [
        ("out", "CALL", "Reset"),
        ("in", "CALLRESULT", "Reset"),
    ]
    assert records[0].message_id == records[1].message_id
    assert records[0].payload == {"type": "Soft"}


# --- Pending CALLs don't accumulate -----------------------------------------------


async def test_unanswered_calls_are_forgotten_after_ttl(tmp_path: Path) -> None:
    clock = FakeClock()
    socket = ScriptedSocket([])
    with MessageLog(tmp_path / "log.jsonl") as log:
        connection = LoggingConnection(socket, "CP_1", log, clock, PENDING_TTL)
        # One unanswered CALL every 10 s for ~17 minutes.
        for i in range(100):
            await connection.send(f'[2, "id-{i}", "GetConfiguration", {{}}]')
            clock.advance(10)

        # Only the CALLs of the last 60 s (TTL) are still remembered.
        assert connection.pending_count <= 7


async def test_late_answer_within_ttl_keeps_action_after_ttl_does_not(tmp_path: Path) -> None:
    clock = FakeClock()
    socket = ScriptedSocket(['[3, "late", {}]', '[3, "too-late", {}]'])
    with MessageLog(tmp_path / "log.jsonl") as log:
        connection = LoggingConnection(socket, "CP_1", log, clock, PENDING_TTL)
        await connection.send('[2, "too-late", "Reset", {"type": "Soft"}]')
        clock.advance(50)
        await connection.send('[2, "late", "Reset", {"type": "Soft"}]')
        clock.advance(50)  # "too-late" is now 100 s old, "late" 50 s
        await connection.send('[2, "next", "Heartbeat", {}]')  # a new CALL triggers cleanup
        await connection.recv()
        await connection.recv()

    records = _read_log(tmp_path / "log.jsonl")
    assert [(r.message_id, r.action) for r in records if r.message_type == "CALLRESULT"] == [
        ("late", "Reset"),
        ("too-late", None),
    ]


# --- Connection events ------------------------------------------------------------


async def test_clean_close_logs_connected_and_disconnected(csms: CsmsHarness) -> None:
    async with fake_charger(csms.url) as cp:
        await cp.boot()
        await cp.websocket.close()
    await wait_until(lambda: len(csms.registry.disconnect_calls) == 1)

    records = _read_log(csms.log_path)
    assert records[0].message_type == "CONNECTED"
    assert records[-1].message_type == "DISCONNECTED"
    events = _events(records)
    assert [(e.message_type, e.charger_id, e.direction) for e in events] == [
        ("CONNECTED", "CP_1", None),
        ("DISCONNECTED", "CP_1", None),
    ]
    assert events[1].payload == {"reason": "clean", "close_code": 1000, "detail": None}


async def test_dropped_connection_logs_network_drop(csms: CsmsHarness) -> None:
    async with fake_charger(csms.url) as cp:
        await cp.boot()
        cp.websocket.transport.abort()  # no close frame, like a network drop
    await wait_until(lambda: len(csms.registry.disconnect_calls) == 1)

    disconnected = _events(_read_log(csms.log_path))[-1]
    assert disconnected.message_type == "DISCONNECTED"
    assert disconnected.payload["reason"] == "network_drop"
    assert disconnected.payload["close_code"] == 1006
