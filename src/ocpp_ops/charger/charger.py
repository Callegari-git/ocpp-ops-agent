"""A virtual OCPP 1.6J charge point: connects, boots, heartbeats, answers commands, reconnects."""

import asyncio
import logging
import random
import uuid
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

from ocpp.exceptions import OCPPError
from ocpp.v16 import call
from ocpp.v16.enums import (
    ChargePointErrorCode,
    ChargePointStatus,
    ConfigurationStatus,
    RegistrationStatus,
    ResetType,
)
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake
from websockets.frames import CloseCode
from websockets.typing import Subprotocol

from ocpp_ops.cancellation import is_cancelling
from ocpp_ops.charger.config import ConfigStore
from ocpp_ops.charger.protocol import ChargerChargePoint
from ocpp_ops.charger.state_machine import ConnectorState, check_transition
from ocpp_ops.clock import Clock, Sleep
from ocpp_ops.message_log import Connection, LoggingConnection, MessageLog, describe_close

logger = logging.getLogger(__name__)

OCPP16 = Subprotocol("ocpp1.6")
# OCPP 1.6 §4.2: if a Pending/Rejected answer has interval 0, the charger picks its own wait.
BOOT_RETRY_FALLBACK_SECONDS = 60
BACKOFF_INITIAL_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 60.0


def reconnect_delay(failures: int, rng: random.Random) -> float:
    """Seconds to wait before reconnecting: exponential backoff with seeded jitter.

    1, 2, 4, ... seconds (capped at 60) after 0, 1, 2, ... failed attempts, scaled by a
    random 50-100%. The jitter keeps a fleet from reconnecting in lockstep after a CSMS
    restart; the seeded RNG keeps runs reproducible.
    """
    ceiling = min(BACKOFF_MAX_SECONDS, BACKOFF_INITIAL_SECONDS * 2 ** min(failures, 10))
    return ceiling * rng.uniform(0.5, 1.0)


class VirtualCharger:
    """One simulated charge point.

    Time, waiting and randomness are injected (`now`, `sleep`, `rng`) so tests control them
    and the same seed replays the same run. Every connector status change goes through
    set_status(): the single point where week 2's fault engine will hook in.
    """

    def __init__(
        self,
        charger_id: str,
        csms_url: str,
        number_of_connectors: int,
        *,
        now: Clock,
        sleep: Sleep,
        rng: random.Random,
        vendor: str = "OcppOps",
        model: str = "VirtualCharger",
        log: MessageLog | None = None,
        reboot_delay: float = 5.0,
        call_timeout: float = 30.0,
    ) -> None:
        self.id = charger_id
        self.config = ConfigStore(number_of_connectors)
        # OCPP-J 1.6 §3.1.1: the charger's identity is the last segment of the URL path.
        self._endpoint = f"{csms_url.rstrip('/')}/{quote(charger_id)}"
        self._now = now
        self._sleep = sleep
        self._rng = rng
        self._vendor = vendor
        self._model = model
        self._log = log
        self._reboot_delay = reboot_delay
        self._call_timeout = call_timeout
        # Connector 0 is the charge point as a whole; 1..N are the physical connectors.
        self._connectors = {i: ConnectorState() for i in range(number_of_connectors + 1)}
        # Session state: set while connected, cleared when the connection ends.
        self._websocket: ClientConnection | None = None
        self._charge_point: ChargerChargePoint | None = None
        self._session_tasks: asyncio.TaskGroup | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._booted = False
        self._reset_requested = False
        # When the CSMS last answered one of our Heartbeats, by our clock. Kept across
        # sessions: it is "last seen" from the charger's side.
        self._last_heartbeat: datetime | None = None
        # When the last BootNotification was Accepted, by our clock.
        self._boot_time: datetime | None = None

    # --- State, read-only from outside ---

    def connector(self, connector_id: int) -> ConnectorState:
        return self._connectors[connector_id]

    @property
    def connector_ids(self) -> list[int]:
        """0 (the charge point as a whole) first, then 1..N."""
        return list(self._connectors)

    @property
    def connected(self) -> bool:
        return self._websocket is not None

    @property
    def booted(self) -> bool:
        return self._booted

    @property
    def last_heartbeat(self) -> datetime | None:
        return self._last_heartbeat

    @property
    def boot_time(self) -> datetime | None:
        return self._boot_time

    # --- The single path for status changes ---

    async def set_status(
        self,
        connector_id: int,
        status: ChargePointStatus,
        error_code: ChargePointErrorCode = ChargePointErrorCode.no_error,
        info: str | None = None,
    ) -> None:
        """Change a connector's status. The ONLY way a status changes.

        Checks the transition (raises InvalidTransitionError), stores the new state, then
        sends a StatusNotification if booted. While offline the change is only stored: every
        status is reported after the next boot anyway.
        """
        if connector_id not in self._connectors:
            raise ValueError(f"{self.id} has no connector {connector_id}")
        current = self._connectors[connector_id]
        check_transition(connector_id, current.status, status, error_code)
        # Stored before any await, so nothing can interleave between check and update.
        self._connectors[connector_id] = ConnectorState(status, error_code, info)
        if self._booted and self._charge_point is not None:
            with suppress(ConnectionClosed):  # dropped now: reported after the next boot
                await self._send_status(self._charge_point, connector_id)

    # --- Lifecycle ---

    async def run(self) -> None:
        """Connect, boot and serve, reconnecting forever. Cancel the task to stop."""
        failures = 0
        while True:
            connected = await self._connect_and_serve()
            if self._reset_requested:
                self._reset_requested = False
                await self._sleep(self._reboot_delay)  # the simulated reboot
                failures = 0
                continue
            failures = 0 if connected else failures + 1
            await self._sleep(reconnect_delay(failures, self._rng))

    async def _connect_and_serve(self) -> bool:
        """Run one connection until it ends. Returns False if we could not connect at all."""
        try:
            # proxy=None: otherwise websockets picks up the system's proxy settings.
            async with connect(self._endpoint, subprotocols=[OCPP16], proxy=None) as websocket:
                try:
                    await self._serve(websocket)
                except asyncio.CancelledError:
                    # Stopped (e.g. Ctrl+C): close with 1001 "going away". Otherwise websockets
                    # closes with 1011 "internal error" whenever an exception leaves the block.
                    await websocket.close(CloseCode.GOING_AWAY)
                    raise
        except (OSError, InvalidHandshake, TimeoutError) as error:
            logger.info("%s: cannot connect to %s: %s", self.id, self._endpoint, error)
            return False
        return True

    async def _serve(self, websocket: ClientConnection) -> None:
        """One session, from connect to disconnect."""
        logging_connection: LoggingConnection | None = None
        connection: Connection = websocket
        if self._log is not None:
            pending_ttl = timedelta(seconds=2 * self._call_timeout)
            logging_connection = LoggingConnection(
                websocket, self.id, self._log, self._now, pending_ttl
            )
            connection = logging_connection
            logging_connection.record_event("CONNECTED", {})
        charge_point = ChargerChargePoint(
            self, connection, self._new_message_id, self._call_timeout
        )
        self._websocket = websocket
        self._charge_point = charge_point
        # Stays as is if the session is cancelled (Ctrl+C) instead of closed.
        close_info: dict[str, Any] = {"reason": "stopped", "close_code": None, "detail": None}
        try:
            close_info = await self._run_session(charge_point)
        finally:
            self._booted = False
            self._websocket = None
            self._charge_point = None
            self._session_tasks = None
            self._heartbeat_task = None
            if logging_connection is not None:
                logging_connection.record_event("DISCONNECTED", close_info)

    async def _run_session(self, charge_point: ChargerChargePoint) -> dict[str, Any]:
        """Serve until the connection ends and return why it ended.

        A TaskGroup runs the receive loop, the boot sequence and later the heartbeat loop:
        when one fails (connection closed, CSMS silent or answering with an error), the
        others are cancelled and the session ends.
        """
        close_info: dict[str, Any] = {}
        try:
            async with asyncio.TaskGroup() as tasks:
                self._session_tasks = tasks
                tasks.create_task(charge_point.start())
                tasks.create_task(self._boot(charge_point))
        except* ConnectionClosed as closed:
            close_info = describe_close(closed.exceptions[0])
        except* (TimeoutError, OCPPError) as failed:
            close_info = {
                "reason": "error",
                "close_code": None,
                "detail": repr(failed.exceptions[0]),
            }
        return close_info

    async def _boot(self, charge_point: ChargerChargePoint) -> None:
        request = call.BootNotification(
            charge_point_vendor=self._vendor, charge_point_model=self._model
        )
        response = await charge_point.send(request)
        while response.status != RegistrationStatus.accepted:
            # OCPP 1.6 §4.2: Pending/Rejected, send nothing else and retry after `interval`.
            await self._sleep(response.interval or BOOT_RETRY_FALLBACK_SECONDS)
            response = await charge_point.send(request)
        if response.interval > 0:
            # OCPP 1.6 §4.2: on Accepted, `interval` is the heartbeat interval to use.
            self.config.set_heartbeat_interval(response.interval)
        self._booted = True
        self._boot_time = self._now()
        for connector_id in self._connectors:  # connector 0 first, then 1..N
            await self._send_status(charge_point, connector_id)
        self._restart_heartbeat()

    def _restart_heartbeat(self) -> None:
        """(Re)start the heartbeat loop with the current HeartbeatInterval; 0 means none.

        The only place that adds a task to a running session. Called from session tasks
        (the boot sequence, the ChangeConfiguration handler).
        """
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        if not self._booted or self._session_tasks is None or self._charge_point is None:
            return
        if is_cancelling():
            # The session is ending: when its TaskGroup shuts down it cancels every session
            # task, this caller included, and refuses new ones (RuntimeError). Checked before
            # the coroutine is created, so none is left un-awaited.
            return
        if self.config.heartbeat_interval > 0:
            self._heartbeat_task = self._session_tasks.create_task(
                self._heartbeat_loop(self._charge_point)
            )

    async def _heartbeat_loop(self, charge_point: ChargerChargePoint) -> None:
        while True:
            await self._sleep(self.config.heartbeat_interval)
            await charge_point.send(call.Heartbeat())
            self._last_heartbeat = self._now()

    async def _send_status(self, charge_point: ChargerChargePoint, connector_id: int) -> None:
        state = self._connectors[connector_id]
        await charge_point.send(
            call.StatusNotification(
                connector_id=connector_id,
                error_code=state.error_code,
                status=state.status,
                info=state.info,
                timestamp=self._now().isoformat(),
            )
        )

    def _new_message_id(self) -> str:
        """A uuid4-shaped id drawn from the seeded RNG, so the same seed gives the same ids."""
        return str(uuid.UUID(int=self._rng.getrandbits(128), version=4))

    # --- Called by the protocol handlers ---

    def change_configuration(self, key: str, value: str) -> ConfigurationStatus:
        status = self.config.change(key, value)
        if key == "HeartbeatInterval" and status == ConfigurationStatus.accepted:
            self._restart_heartbeat()  # takes effect now, not after the next heartbeat
        return status

    async def reboot(self, reset_type: ResetType) -> None:
        """Simulate the reboot after an accepted Reset (Soft and Hard alike for now).

        Closing the connection ends the session; run() then waits `reboot_delay` and connects
        and boots again. Config is kept (non-volatile on real chargers), and so are connector
        statuses: what a reboot clears is decided per fault in week 2.
        """
        logger.info("%s: %s reset, rebooting", self.id, reset_type.value)
        self._reset_requested = True
        if self._websocket is not None:
            await self._websocket.close()
