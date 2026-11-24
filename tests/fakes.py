"""Test doubles: a controllable clock, a registry that records disconnects, a fake charger."""

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ocpp.v16 import ChargePoint, call, call_result
from ocpp.v16.datatypes import MeterValue
from ocpp.v16.enums import ChargePointErrorCode, ChargePointStatus
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed
from websockets.typing import Subprotocol

from ocpp_ops.csms.registry import Registry

OCPP16 = Subprotocol("ocpp1.6")
HEARTBEAT_INTERVAL = 42


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

    def __init__(self, now: FakeClock) -> None:
        super().__init__(now)
        self.disconnect_calls: list[tuple[str, int]] = []

    def disconnect(self, charger_id: str, token: int) -> None:
        self.disconnect_calls.append((charger_id, token))
        super().disconnect(charger_id, token)


@dataclass
class CsmsHarness:
    """What the `csms` fixture gives a test: where to connect and what to inspect."""

    url: str
    registry: RecordingRegistry
    clock: FakeClock
    log_path: Path


class FakeChargePoint(ChargePoint):
    """Minimal OCPP 1.6 charger that sends requests when the test asks."""

    def __init__(self, charger_id: str, websocket: ClientConnection) -> None:
        super().__init__(charger_id, websocket)
        self.websocket = websocket

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
        self, connector_id: int, meter_value: list[MeterValue]
    ) -> call_result.MeterValues:
        return await self.call(call.MeterValues(connector_id=connector_id, meter_value=meter_value))


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


async def wait_until(condition: Callable[[], bool]) -> None:
    """Poll until condition() is true, for things the server does after the client returns.

    Polling (not an asyncio.Event) because the state is changed by server code that has
    no event for tests to await, e.g. the registry update after a connection closes.
    """
    async with asyncio.timeout(WAIT_TIMEOUT_SECONDS):
        while not condition():  # noqa: ASYNC110
            await asyncio.sleep(0.01)
