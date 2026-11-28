"""Test doubles: a controllable clock, a registry that records disconnects, a fake charger."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import ocpp.charge_point
import pytest
from ocpp.messages import Call
from ocpp.routing import on
from ocpp.v16 import ChargePoint, call, call_result
from ocpp.v16.datatypes import MeterValue
from ocpp.v16.enums import (
    Action,
    ChargePointErrorCode,
    ChargePointStatus,
    ConfigurationStatus,
    Reason,
    ResetStatus,
)
from websockets.asyncio.client import ClientConnection, connect
from websockets.asyncio.server import Server
from websockets.exceptions import ConnectionClosed
from websockets.typing import Subprotocol

from ocpp_ops.clock import Clock
from ocpp_ops.csms.authorization import AuthorizationList
from ocpp_ops.csms.central_system import CentralSystem
from ocpp_ops.csms.registry import Registry
from ocpp_ops.csms.server import start_server
from ocpp_ops.message_log import MessageLog

OCPP16 = Subprotocol("ocpp1.6")
HEARTBEAT_INTERVAL = 42
# Short, so the timeout test is quick; a localhost round trip takes milliseconds.
CALL_TIMEOUT = 0.5
# The repo's default authorization list (the CLIs' --id-tags default).
DEFAULT_ID_TAGS = Path(__file__).parent.parent / "id_tags.yaml"
# The test CSMS's authorization list. Any other tag is Invalid.
ACCEPTED_TAG = "TAG-OK"
BLOCKED_TAG = "TAG-BLOCKED"
EXPIRED_TAG = "TAG-EXPIRED"
TEST_AUTHORIZATION = AuthorizationList(
    {ACCEPTED_TAG: "Accepted", BLOCKED_TAG: "Blocked", EXPIRED_TAG: "Expired"}
)


class FakeClock:
    """A clock that only moves when the test says so."""

    def __init__(self, start: datetime = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)) -> None:
        self.current = start

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


class RecordingRegistry(Registry):
    """Registry that also remembers every disconnect() call, so tests can wait for one."""

    def __init__(self, now: Clock) -> None:
        super().__init__(now)
        self.disconnect_calls: list[tuple[str, int]] = []

    def disconnect(self, charger_id: str, token: int) -> None:
        self.disconnect_calls.append((charger_id, token))
        super().disconnect(charger_id, token)


@dataclass
class CsmsHarness:
    """What the `csms` fixture gives a test: where to connect and what to inspect."""

    url: str
    port: int
    central_system: CentralSystem
    registry: RecordingRegistry
    clock: FakeClock
    log_path: Path
    server: Server


AnswerMode = Literal["answer", "silent", "error"]


class FakeChargePoint(ChargePoint):
    """Minimal OCPP 1.6 charger: sends requests when the test asks, answers CSMS commands.

    `mode` controls how it answers commands: normally, never ("silent", for timeouts), or
    with a CALLERROR ("error").
    """

    def __init__(self, charger_id: str, websocket: ClientConnection) -> None:
        super().__init__(charger_id, websocket)
        self.websocket = websocket
        self.mode: AnswerMode = "answer"
        self.config = {
            "HeartbeatInterval": "60",
            "NumberOfConnectors": "2",
            "WebSocketPingInterval": "0",
        }
        self.readonly_keys = {"NumberOfConnectors"}
        self.reboot_required_keys = {"WebSocketPingInterval"}
        self.resets: list[str] = []

    # --- Commands from the CSMS ---

    @on(Action.get_configuration)
    async def on_get_configuration(
        self, key: list[str] | None = None
    ) -> call_result.GetConfiguration:
        await self._before_answer()
        # OCPP 1.6 §5.8: no key list (or an empty one) means "all keys".
        wanted = key or list(self.config)
        known = [k for k in wanted if k in self.config]
        unknown = [k for k in wanted if k not in self.config]
        return call_result.GetConfiguration(
            configuration_key=[
                {"key": k, "readonly": k in self.readonly_keys, "value": self.config[k]}
                for k in known
            ],
            unknown_key=unknown or None,
        )

    @on(Action.change_configuration)
    async def on_change_configuration(
        self, key: str, value: str
    ) -> call_result.ChangeConfiguration:
        await self._before_answer()
        if key not in self.config:
            status = ConfigurationStatus.not_supported
        elif key in self.readonly_keys:
            status = ConfigurationStatus.rejected
        elif key in self.reboot_required_keys:
            status = ConfigurationStatus.reboot_required
        else:
            status = ConfigurationStatus.accepted
            self.config[key] = value
        return call_result.ChangeConfiguration(status=status)

    @on(Action.reset)
    async def on_reset(self, type: str) -> call_result.Reset:
        await self._before_answer()
        self.resets.append(type)
        return call_result.Reset(status=ResetStatus.accepted)

    async def _before_answer(self) -> None:
        if self.mode == "silent":
            await asyncio.Event().wait()  # never answers; cancelled when the test ends
        if self.mode == "error":
            # ocpp turns an exception in a handler into a CALLERROR (InternalError).
            raise RuntimeError("simulated charger failure")

    # --- Requests to the CSMS ---

    async def boot(self) -> call_result.BootNotification:
        return await self.call(
            call.BootNotification(charge_point_vendor="TestVendor", charge_point_model="TestModel")
        )

    async def heartbeat(self) -> call_result.Heartbeat:
        return await self.call(call.Heartbeat())

    async def status(
        self,
        connector_id: int,
        status: ChargePointStatus,
        error_code: ChargePointErrorCode = ChargePointErrorCode.no_error,
        info: str | None = None,
        timestamp: str | None = None,
    ) -> call_result.StatusNotification:
        return await self.call(
            call.StatusNotification(
                connector_id=connector_id,
                error_code=error_code,
                status=status,
                info=info,
                timestamp=timestamp,
            )
        )

    async def meter_values(
        self,
        connector_id: int,
        meter_value: list[MeterValue],
        transaction_id: int | None = None,
    ) -> call_result.MeterValues:
        return await self.call(
            call.MeterValues(
                connector_id=connector_id, meter_value=meter_value, transaction_id=transaction_id
            )
        )

    async def authorize(self, id_tag: str) -> call_result.Authorize:
        return await self.call(call.Authorize(id_tag=id_tag))

    async def start_transaction(
        self, connector_id: int, id_tag: str, meter_start: int, timestamp: str
    ) -> call_result.StartTransaction:
        return await self.call(
            call.StartTransaction(
                connector_id=connector_id,
                id_tag=id_tag,
                meter_start=meter_start,
                timestamp=timestamp,
            )
        )

    async def stop_transaction(
        self,
        transaction_id: int,
        meter_stop: int,
        timestamp: str,
        reason: Reason | None = None,
        id_tag: str | None = None,
    ) -> call_result.StopTransaction:
        return await self.call(
            call.StopTransaction(
                meter_stop=meter_stop,
                timestamp=timestamp,
                transaction_id=transaction_id,
                reason=reason,
                id_tag=id_tag,
            )
        )


@asynccontextmanager
async def running_csms(
    clock: FakeClock, log_path: Path, port: int = 0
) -> AsyncIterator[CsmsHarness]:
    """A real CSMS on 127.0.0.1 (port 0 = any free port), stopped when the block exits."""
    registry = RecordingRegistry(clock)
    with MessageLog(log_path) as log:
        central_system = CentralSystem(
            registry,
            log,
            HEARTBEAT_INTERVAL,
            now=clock,
            authorization=TEST_AUTHORIZATION,
            call_timeout=CALL_TIMEOUT,
        )
        server = await start_server(central_system, "127.0.0.1", port)
        async with server:
            actual_port = server.sockets[0].getsockname()[1]
            url = f"ws://127.0.0.1:{actual_port}"
            yield CsmsHarness(url, actual_port, central_system, registry, clock, log_path, server)


@asynccontextmanager
async def fake_charger(base_url: str, charger_id: str = "CP_1") -> AsyncIterator[FakeChargePoint]:
    """Connect a FakeChargePoint to the CSMS and run its receive loop in the background."""
    async with connect(f"{base_url}/{charger_id}", subprotocols=[OCPP16]) as websocket:
        charge_point = FakeChargePoint(charger_id, websocket)
        receive_loop = asyncio.create_task(charge_point.start())
        try:
            yield charge_point
        finally:
            receive_loop.cancel()
            # The loop ends with ConnectionClosed if the test closed the connection itself.
            with suppress(asyncio.CancelledError, ConnectionClosed):
                await receive_loop


WAIT_TIMEOUT_SECONDS = 2.0


async def wait_until(condition: Callable[[], bool], within: float = WAIT_TIMEOUT_SECONDS) -> None:
    """Poll until condition() is true, for things the server does after the client returns.

    Polling (not an asyncio.Event) because the state is changed by server code that has
    no event for tests to await, e.g. the registry update after a connection closes.
    """
    async with asyncio.timeout(within):
        while not condition():  # noqa: ASYNC110
            await asyncio.sleep(0.01)


def hold_validation(
    monkeypatch: pytest.MonkeyPatch,
    is_target: Callable[[Call], bool],
    hold: Callable[[], Awaitable[None]],
) -> None:
    """Make ocpp's payload validation of some CALLs take longer: await `hold()` first.

    ocpp's call() validates each payload in an executor thread *before* taking its lock
    (ocpp 2.1.0, ChargePoint.call -> messages.validate_payload -> run_in_executor), so a
    slow validation lets a later call overtake an earlier one. This makes that race
    deterministic. Both sides validate CALLs (the receiver too), so a target is held twice.
    """
    original = ocpp.charge_point.validate_payload

    async def validate(message: Any, ocpp_version: str) -> None:
        if isinstance(message, Call) and is_target(message):
            await hold()
        await original(message, ocpp_version)

    monkeypatch.setattr(ocpp.charge_point, "validate_payload", validate)
