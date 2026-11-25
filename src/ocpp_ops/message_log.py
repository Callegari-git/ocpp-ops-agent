"""JSONL log of every OCPP frame in and out, plus connection events.

One JSON line per frame or event. This file is the audit trail, and later the agent's context.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Protocol, Self

from pydantic import BaseModel

from ocpp_ops.clock import Clock

Direction = Literal["in", "out"]
FrameType = Literal["CALL", "CALLRESULT", "CALLERROR", "INVALID"]
EventType = Literal["CONNECTED", "DISCONNECTED"]
MessageType = FrameType | EventType

# OCPP-J 1.6 §4.1.3: the first element of every frame is its message type id.
_MESSAGE_TYPE_IDS: dict[int, FrameType] = {2: "CALL", 3: "CALLRESULT", 4: "CALLERROR"}


@dataclass(frozen=True)
class Frame:
    """The parts of one OCPP-J frame that we log."""

    message_type: FrameType
    message_id: str | None
    action: str | None  # only CALL frames carry their action
    payload: Any


def parse_frame(raw: str | bytes) -> Frame:
    """Split a raw OCPP-J frame into its parts.

    Never raises: a frame we can't parse is returned as INVALID with the raw text, so the
    audit trail keeps it. Rejecting it is the ocpp library's job, not the logger's.
    """
    text = raw.decode(errors="replace") if isinstance(raw, bytes) else raw
    try:
        data = json.loads(text)
        message_type = _MESSAGE_TYPE_IDS[data[0]]
        message_id = str(data[1])
        if message_type == "CALL":  # [2, id, action, payload]
            return Frame("CALL", message_id, data[2], data[3])
        if message_type == "CALLRESULT":  # [3, id, payload]
            return Frame("CALLRESULT", message_id, None, data[2])
        # [4, id, errorCode, errorDescription, errorDetails]
        error = {"errorCode": data[2], "errorDescription": data[3], "errorDetails": data[4]}
        return Frame("CALLERROR", message_id, None, error)
    except (ValueError, KeyError, IndexError, TypeError):
        return Frame("INVALID", None, None, {"raw": text})


class LogRecord(BaseModel):
    """One line of the JSONL log.

    Frames and connection events share this shape so readers parse a single format.
    For CONNECTED/DISCONNECTED lines, direction, message_id and action are null.
    """

    timestamp: datetime
    charger_id: str
    direction: Direction | None
    message_type: MessageType
    message_id: str | None
    action: str | None
    payload: Any


class MessageLog:
    """Append-only JSONL file, shared by every connection.

    Writes are synchronous and flushed at once: a line per frame is cheap, and nothing is
    lost if the process dies or someone is tailing the file. Everything runs on one asyncio
    thread, so lines from different chargers can never interleave.
    """

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._file = path.open("a", encoding="utf-8")

    def write(self, record: LogRecord) -> None:
        self._file.write(record.model_dump_json() + "\n")
        self._file.flush()

    def close(self) -> None:
        self._file.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class Connection(Protocol):
    """The two methods ocpp's ChargePoint uses on its connection."""

    async def recv(self) -> str | bytes: ...

    async def send(self, message: str) -> None: ...


@dataclass(frozen=True)
class _PendingCall:
    action: str
    sent_at: datetime


class LoggingConnection:
    """Wraps a websocket connection so every frame is logged on its way in or out.

    ocpp's ChargePoint only ever calls recv() and send() on its connection, so passing it
    this wrapper logs every frame without touching the library's internals.
    """

    def __init__(
        self,
        connection: Connection,
        charger_id: str,
        log: MessageLog,
        now: Clock,
        pending_ttl: timedelta,
    ) -> None:
        self._connection = connection
        self._charger_id = charger_id
        self._log = log
        self._now = now
        # message_id -> CALL still waiting for its response, so the response can be logged
        # with the CALL's action. One map per direction: each side picks its own ids, so
        # the two could collide.
        self._calls_sent: dict[str, _PendingCall] = {}
        self._calls_received: dict[str, _PendingCall] = {}
        # A CALL that is never answered (timeout) would otherwise stay in its map for as
        # long as the connection lives. Its entry is dropped after this long.
        self._pending_ttl = pending_ttl

    async def recv(self) -> str | bytes:
        raw = await self._connection.recv()
        self._record("in", raw)
        return raw

    async def send(self, message: str) -> None:
        # Log *before* sending: the reply can be received (and logged) before send()
        # returns, and it needs our CALL to be known already to find its action.
        self._record("out", message)
        await self._connection.send(message)

    def record_event(self, event: EventType, payload: dict[str, Any]) -> None:
        """Log a connection event (not an OCPP frame) for this charger."""
        self._log.write(
            LogRecord(
                timestamp=self._now(),
                charger_id=self._charger_id,
                direction=None,
                message_type=event,
                message_id=None,
                action=None,
                payload=payload,
            )
        )

    @property
    def pending_count(self) -> int:
        """CALLs (both directions) still remembered while waiting for their response."""
        return len(self._calls_sent) + len(self._calls_received)

    def _record(self, direction: Direction, raw: str | bytes) -> None:
        frame = parse_frame(raw)
        record = LogRecord(
            timestamp=self._now(),
            charger_id=self._charger_id,
            direction=direction,
            message_type=frame.message_type,
            message_id=frame.message_id,
            action=self._action_for(direction, frame),
            payload=frame.payload,
        )
        self._log.write(record)

    def _action_for(self, direction: Direction, frame: Frame) -> str | None:
        """A CALL names its action; a response only carries the id of the CALL it answers."""
        if frame.message_id is None or frame.message_type == "INVALID":
            return None
        if frame.message_type == "CALL":
            self._forget_stale_calls()
            pending = self._calls_sent if direction == "out" else self._calls_received
            if frame.action is not None:
                pending[frame.message_id] = _PendingCall(frame.action, self._now())
            return frame.action
        # A response going out answers a CALL that came in, and vice versa.
        pending = self._calls_received if direction == "out" else self._calls_sent
        call = pending.pop(frame.message_id, None)
        return call.action if call else None

    def _forget_stale_calls(self) -> None:
        cutoff = self._now() - self._pending_ttl
        for pending in (self._calls_sent, self._calls_received):
            stale = [message_id for message_id, call in pending.items() if call.sent_at < cutoff]
            for message_id in stale:
                del pending[message_id]
