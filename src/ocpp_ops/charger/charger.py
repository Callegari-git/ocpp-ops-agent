"""A virtual OCPP 1.6J charge point: connects, boots, heartbeats, answers commands, reconnects."""

import asyncio
import logging
import random
import uuid
from collections.abc import Callable, Coroutine
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

from ocpp.exceptions import OCPPError
from ocpp.v16 import call
from ocpp.v16.datatypes import MeterValue, SampledValue
from ocpp.v16.enums import (
    AuthorizationStatus,
    AvailabilityStatus,
    AvailabilityType,
    ChargePointErrorCode,
    ChargePointStatus,
    ConfigurationStatus,
    Measurand,
    ReadingContext,
    Reason,
    RegistrationStatus,
    ResetType,
    UnitOfMeasure,
)
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake
from websockets.frames import CloseCode
from websockets.typing import Subprotocol

from ocpp_ops.cancellation import is_cancelling
from ocpp_ops.charger.config import ConfigStore
from ocpp_ops.charger.meter import EnergyMeter
from ocpp_ops.charger.protocol import ChargerChargePoint
from ocpp_ops.charger.state_machine import IN_USE, ConnectorState, check_transition
from ocpp_ops.charger.transactions import (
    UNKNOWN_TRANSACTION_ID,
    ChargerTransaction,
    QueuedMessage,
    TransactionQueue,
    TransactionRequest,
)
from ocpp_ops.clock import Clock, Sleep
from ocpp_ops.message_log import Connection, LoggingConnection, MessageLog, describe_close

logger = logging.getLogger(__name__)
S = ChargePointStatus

OCPP16 = Subprotocol("ocpp1.6")
# OCPP 1.6 §4.2: if a Pending/Rejected answer has interval 0, the charger picks its own wait.
BOOT_RETRY_FALLBACK_SECONDS = 60
BACKOFF_INITIAL_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 60.0
# Each connector of an AC charger on a 3-phase 16 A supply: 3 x 230 V x 16 A ~ 11 kW.
DEFAULT_MAX_POWER_W = 11_000
# What a connector with a cable still plugged in may report after a reboot. Vendors differ;
# Finishing matches §7.7 ("a Transaction has stopped [...] e.g. the cable has not been
# removed"). Available is a firmware that forgets the cable: a realistic fault.
PLUGGED_STATUSES_AFTER_REBOOT = frozenset({S.finishing, S.preparing, S.available})


def charger_endpoint(csms_url: str, charger_id: str) -> str:
    """The URL a charger connects to: the CSMS base URL plus its identity as the last segment.

    OCPP-J 1.6 §3.1.1. The base can have a path of its own (e.g. SteVe's
    `ws://host:8180/steve/websocket/CentralSystemService`). The id is fully percent-encoded,
    "/" included, so it always stays one path segment.
    """
    return f"{csms_url.rstrip('/')}/{quote(charger_id, safe='')}"


def reconnect_delay(failures: int, rng: random.Random) -> float:
    """Seconds to wait before reconnecting: exponential backoff with seeded jitter.

    1, 2, 4, ... seconds (capped at 60) after 0, 1, 2, ... failed attempts, scaled by a
    random 50-100%. The jitter keeps a fleet from reconnecting in lockstep after a CSMS
    restart; the seeded RNG keeps runs reproducible.
    """
    ceiling = min(BACKOFF_MAX_SECONDS, BACKOFF_INITIAL_SECONDS * 2 ** min(failures, 10))
    return ceiling * rng.uniform(0.5, 1.0)


class SessionError(RuntimeError):
    """A session action (plug in, authorize, start, stop, unplug) that doesn't fit the
    connector's current state."""


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
        max_power_w: int = DEFAULT_MAX_POWER_W,
        plugged_status_after_reboot: ChargePointStatus = S.finishing,
    ) -> None:
        if plugged_status_after_reboot not in PLUGGED_STATUSES_AFTER_REBOOT:
            raise ValueError(f"cannot report {plugged_status_after_reboot} after a reboot")
        self.id = charger_id
        self.config = ConfigStore(number_of_connectors)
        self._endpoint = charger_endpoint(csms_url, charger_id)
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
        # Availability asked by ChangeAvailability, per connector (0 included). It lives on
        # the charger: §4.9, Unavailable set this way "MUST be persistent across reboots".
        # It can differ from the status for a while: a change waits for the end of use.
        self._operative = dict.fromkeys(self._connectors, True)
        self._plugged_status_after_reboot = plugged_status_after_reboot
        # Charging sessions. Like the config, these live on the charger, not on a
        # connection: they carry on while offline. Every charging connector draws
        # max_power_w (no load sharing: smart charging is out of scope).
        self._max_power_w = max_power_w
        self._meters = {i: EnergyMeter(now) for i in range(1, number_of_connectors + 1)}
        self._authorized_tags: dict[int, str] = {}  # accepted idTag, until start()
        self._transactions: dict[int, ChargerTransaction] = {}  # by connector
        self._queue = TransactionQueue()
        # Set while run() runs: tasks that outlive a connection (meter sampling).
        self._lifetime_tasks: asyncio.TaskGroup | None = None
        self._sampling_tasks: dict[int, asyncio.Task[None]] = {}  # by connector
        # Session state: set while connected, cleared when the connection ends.
        self._websocket: ClientConnection | None = None
        self._charge_point: ChargerChargePoint | None = None
        self._session_tasks: asyncio.TaskGroup | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._sender_task: asyncio.Task[None] | None = None
        self._booted = False
        # Set by an accepted Reset just before the connection is closed; run() then reboots.
        self._reset_type: ResetType | None = None
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

    def transaction(self, connector_id: int) -> ChargerTransaction | None:
        return self._transactions.get(connector_id)

    def meter_wh(self, connector_id: int) -> int:
        """The connector's Energy.Active.Import.Register now, in Wh."""
        return self._meters[connector_id].register_wh()

    @property
    def max_power_w(self) -> int:
        """What each charging connector draws, in W (no load sharing)."""
        return self._max_power_w

    @property
    def active_transactions(self) -> int:
        return len(self._transactions)

    @property
    def power_w(self) -> int:
        """Power drawn by all connectors right now, in W."""
        return sum(meter.power_w for meter in self._meters.values())

    def operative(self, connector_id: int) -> bool:
        """The availability last asked for the connector (ChangeAvailability)."""
        return self._operative[connector_id]

    @property
    def queued_messages(self) -> int:
        """Transaction-related messages not yet delivered to the CSMS."""
        return len(self._queue)

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

        A status does not overtake its connector's transaction messages queued before it
        (e.g. stop() then unplug(): Finishing, StopTransaction, Available), but waits at most
        for their first delivery attempt, never through retry backoffs. Faulted never waits:
        faults are reported at once (§3.7 lets non-transaction messages go immediately).
        """
        if asyncio.current_task() is self._sender_task:
            # The sender would wait for its own deliveries: a deadlock. It never changes
            # statuses; this keeps it that way.
            raise RuntimeError("set_status() must not be called from the transaction sender")
        if connector_id not in self._connectors:
            raise ValueError(f"{self.id} has no connector {connector_id}")
        current = self._connectors[connector_id]
        check_transition(connector_id, current.status, status, error_code)
        # Stored before any await, so nothing can interleave between check and update.
        self._connectors[connector_id] = ConnectorState(status, error_code, info)
        charge_point = self._charge_point
        if not self._booted or charge_point is None:
            return
        if status != S.faulted:
            earlier = self._queue.last_for(connector_id)
            if earlier is not None:
                await self._queue.wait_for_first_attempt(earlier)
            if self._charge_point is not charge_point:
                return  # the session ended while waiting: reported after the next boot
        with suppress(ConnectionClosed):  # dropped now: reported after the next boot
            await self._send_status(charge_point, connector_id)

    # --- Lifecycle ---

    async def run(self) -> None:
        """Connect, boot and serve, reconnecting forever. Cancel the task to stop.

        Meter sampling must go on while the charger is offline, so it can't run in a
        session's TaskGroup: it runs in this one, which lasts as long as run().
        """
        async with asyncio.TaskGroup() as tasks:
            self._lifetime_tasks = tasks
            try:
                await self._connect_forever()
            finally:
                # Cleared before the TaskGroup shuts down (it cancels and awaits the sampling
                # tasks next): from here on, nothing tries to add a task to it.
                self._lifetime_tasks = None
                self._sampling_tasks.clear()

    async def _connect_forever(self) -> None:
        failures = 0
        while True:
            connected = await self._connect_and_serve()
            if self._reset_type is not None:
                reset_type, self._reset_type = self._reset_type, None
                await self._reboot(reset_type)
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
            self._sender_task = None
            self._queue.interrupt()  # a status waiting for the queue gives up (see set_status)
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
        # OCPP 1.6 §4.2 / §3.7: transaction messages queued while offline go out only after
        # an accepted boot, here after the statuses, then new ones as they come.
        self._sender_task = self._start_session_task(
            lambda: self._deliver_transaction_messages(charge_point)
        )

    def _start_session_task(
        self, make: Callable[[], Coroutine[Any, Any, None]]
    ) -> asyncio.Task[None] | None:
        """Add a task to the running session: the only place that does.

        Called from session tasks (the boot sequence, the ChangeConfiguration handler).
        Starts nothing if there is no session, or if the calling task is being cancelled:
        the session is then ending, and its TaskGroup, which cancels every session task (this
        caller included), refuses new ones (RuntimeError). `make` creates the coroutine only
        after that check, so none is left un-awaited.
        """
        if self._session_tasks is None or is_cancelling():
            return None
        return self._session_tasks.create_task(make())

    def _restart_heartbeat(self) -> None:
        """(Re)start the heartbeat loop with the current HeartbeatInterval; 0 means none."""
        if self._heartbeat_task is not None:
            self._heartbeat_task.cancel()
            self._heartbeat_task = None
        charge_point = self._charge_point
        if not self._booted or charge_point is None or self.config.heartbeat_interval == 0:
            return
        self._heartbeat_task = self._start_session_task(lambda: self._heartbeat_loop(charge_point))

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

    # --- Charging sessions: what a driver and an EV do at the charger ---
    #
    # Status changes go through set_status() like any other. Normal flow on a connector:
    # plug_in (Available -> Preparing), authorize, start (-> Charging), stop (-> Finishing),
    # unplug (-> Available).

    async def plug_in(self, connector_id: int) -> None:
        """An EV is plugged in: Available -> Preparing (OCPP 1.6 §4.9 A2)."""
        self._require_status(connector_id, {S.available})
        await self.set_status(connector_id, S.preparing)

    async def authorize(self, connector_id: int, id_tag: str) -> AuthorizationStatus:
        """The driver presents an idTag; the CSMS decides (Authorize, §4.1).

        Not Accepted: the use ends (back to Available, or Unavailable if that change was
        scheduled) and nothing starts. The spec has no rule for this case; we end the
        intended usage (§4.9 B1). Needs a connection: there is no local authorization list
        or cache.
        """
        self._require_status(connector_id, {S.preparing})
        charge_point = self._charge_point
        if not self._booted or charge_point is None:
            raise SessionError(f"{self.id}: offline, cannot authorize {id_tag}")
        response = await charge_point.send(call.Authorize(id_tag=id_tag))
        status = AuthorizationStatus(response.id_tag_info["status"])
        if status == AuthorizationStatus.accepted:
            self._authorized_tags[connector_id] = id_tag
        else:
            await self._end_use(connector_id)
        return status

    async def start(self, connector_id: int) -> ChargerTransaction:
        """Charging starts: Preparing -> Charging (§4.9 B3), StartTransaction queued."""
        self._require_status(connector_id, {S.preparing})
        id_tag = self._authorized_tags.get(connector_id)
        if id_tag is None:
            raise SessionError(f"{self.id}: connector {connector_id} has no accepted idTag")
        if self._lifetime_tasks is None:
            raise SessionError(f"{self.id}: not running")
        del self._authorized_tags[connector_id]
        meter = self._meters[connector_id]
        transaction = ChargerTransaction(connector_id, id_tag, meter.register_wh(), self._now())
        self._transactions[connector_id] = transaction
        meter.set_power(self._max_power_w)
        self._restart_sampling(connector_id)
        request = call.StartTransaction(
            connector_id=connector_id,
            id_tag=id_tag,
            meter_start=transaction.meter_start,
            timestamp=transaction.started_at.isoformat(),
        )
        try:
            await self.set_status(connector_id, S.charging)
        finally:
            # Queued after the status report, so the wire order is always StatusNotification
            # then StartTransaction (see _queue). In `finally`: the transaction has started
            # even if the status report failed.
            self._queue_message(transaction, request)
        return transaction

    async def stop(self, connector_id: int, reason: Reason = Reason.local) -> None:
        """The driver stops charging (Local: tag presented again, stop button):
        Charging -> Finishing (§4.9 C6), StopTransaction queued. The cable is still in."""
        if reason != Reason.local:
            raise ValueError(f"stop() only implements reason Local, not {reason}")
        await self._stop_transaction(connector_id, reason)

    async def unplug(self, connector_id: int) -> None:
        """The cable is removed. During a transaction, that is the EV side disconnecting:
        StopTransaction with reason EVDisconnected, Charging -> Finishing -> Available (or
        Unavailable, if that change was scheduled).

        §4.9 (StopTransactionOnEVSideDisconnect=true): a Finishing status "SHOULD be send",
        with info "EV side disconnected". The table also allows Charging -> Available (C1);
        we go through Finishing, as that paragraph describes.
        """
        self._require_status(connector_id, {S.preparing, S.charging, S.finishing})
        if connector_id in self._transactions:
            await self._stop_transaction(
                connector_id, Reason.ev_disconnected, info="EV side disconnected"
            )
        self._authorized_tags.pop(connector_id, None)
        await self._end_use(connector_id)

    async def _end_use(self, connector_id: int) -> None:
        """The connector is free again: Available, then any scheduled availability change.

        From Finishing, a scheduled Unavailable is applied directly (§4.9 F8), so the
        connector is never shown Available to a new driver in between. Preparing has no
        direct path to Unavailable (B8 is empty): Available first, then Unavailable.
        """
        status = self._connectors[connector_id].status
        direct = status == S.finishing and not self._operative[connector_id]
        await self.set_status(connector_id, S.unavailable if direct else S.available)
        await self._apply_availability()

    async def _stop_transaction(
        self, connector_id: int, reason: Reason, info: str | None = None
    ) -> None:
        transaction = self._transactions.get(connector_id)
        if transaction is None:
            raise SessionError(f"{self.id}: no transaction on connector {connector_id}")
        self._require_status(connector_id, {S.charging})
        del self._transactions[connector_id]
        self._restart_sampling(connector_id)  # no transaction any more: just stops it
        meter = self._meters[connector_id]
        meter.set_power(0)
        request = call.StopTransaction(
            meter_stop=meter.register_wh(),
            timestamp=self._now().isoformat(),
            transaction_id=UNKNOWN_TRANSACTION_ID,  # placeholder, set when sent
            reason=reason,
            # "The identifier which requested to stop" (§6.49): the driver's tag when they
            # stop it themselves; nobody presented one when the EV side disconnected.
            id_tag=transaction.id_tag if reason == Reason.local else None,
        )
        try:
            await self.set_status(connector_id, S.finishing, info=info)
        finally:
            self._queue_message(transaction, request)

    def _require_status(self, connector_id: int, allowed: set[ChargePointStatus]) -> None:
        if connector_id not in self._meters:
            raise ValueError(f"{self.id} has no connector {connector_id}")
        status = self._connectors[connector_id].status
        if status not in allowed:
            raise SessionError(f"{self.id}: connector {connector_id} is {status.value}")

    # --- Transaction-related messages ---

    def _queue_message(self, transaction: ChargerTransaction, request: TransactionRequest) -> None:
        """Every transaction-related message goes through the queue, online or not.

        Callers queue *after* their own StatusNotification has been answered, so on the
        wire the status comes first (start: Charging then StartTransaction).
        """
        self._queue.put(QueuedMessage(transaction, request))

    async def _deliver_transaction_messages(self, charge_point: ChargerChargePoint) -> None:
        """Send queued messages one at a time, oldest first, for as long as the session lasts.

        OCPP 1.6 §3.7.1: when the CSMS fails to process a message, resend it after
        TransactionMessageRetryInterval x the transmissions so far, and give up after
        TransactionMessageAttempts transmissions.
        """
        while True:
            message = await self._queue.first()
            try:
                # send() re-raises a cancellation that ocpp swallowed (Python 3.11): the
                # message then stays queued instead of being marked delivered.
                response = await charge_point.send(message.payload())
            except OCPPError as error:
                # OCPP-J errata 3.2: a CALLERROR is how a CSMS reports "failure to process".
                self._queue.mark_attempted(message)
                logger.warning("%s: CSMS failed to process %s: %r", self.id, message, error)
                if not self._give_up_after_failure(message):
                    await self._back_off(message)
                continue
            except TimeoutError:
                # No answer counts as a failure too (§4.8, §4.10: the charger "will try the
                # same message again"). Like any timeout it ends the session; the message is
                # resent after the next boot.
                self._queue.mark_attempted(message)
                self._give_up_after_failure(message)
                raise
            self._queue.mark_attempted(message)
            self._queue.drop_first()
            self._on_delivered(message, response)

    async def _back_off(self, message: QueuedMessage) -> None:
        """Wait before resending: TransactionMessageRetryInterval x failures so far. Statuses
        waiting for the queue go out meanwhile (see set_status)."""
        self._queue.set_backing_off(True)
        try:
            await self._sleep(self.config.transaction_message_retry_interval * message.failures)
        finally:
            self._queue.set_backing_off(False)

    def _give_up_after_failure(self, message: QueuedMessage) -> bool:
        """Count a failed transmission of the first queued message. True if dropped."""
        message.failures += 1
        if message.failures < max(1, self.config.transaction_message_attempts):
            return False
        self._queue.drop_first()
        logger.warning("%s: giving up on %s", self.id, message)
        if isinstance(message.request, call.StartTransaction):
            # Errata 3.18: no transactionId will ever come; later messages use -1.
            message.transaction.transaction_id = UNKNOWN_TRANSACTION_ID
        return True

    def _on_delivered(self, message: QueuedMessage, response: Any) -> None:
        if not isinstance(message.request, call.StartTransaction):
            return
        message.transaction.transaction_id = response.transaction_id
        status = response.id_tag_info["status"]
        if status != AuthorizationStatus.accepted:
            # §3.5.4 StopTransactionOnInvalidId is not implemented yet: we keep charging.
            logger.warning(
                "%s: transaction %s started with idTag status %s; charging continues",
                self.id,
                response.transaction_id,
                status,
            )

    # --- Meter sampling ---

    def _restart_sampling(self, connector_id: int) -> None:
        """(Re)start periodic MeterValues for the connector's transaction, from now.

        Sampling tasks live in run()'s TaskGroup, not the session's: they go on offline. No
        is_cancelling() check here, unlike session tasks: a session task being cancelled
        (connection lost) must still be able to restart sampling. run() clears
        _lifetime_tasks before its TaskGroup shuts down, so a closed group is never used.
        """
        old = self._sampling_tasks.pop(connector_id, None)
        if old is not None:
            old.cancel()
        transaction = self._transactions.get(connector_id)
        if transaction is None or self._lifetime_tasks is None:
            return
        if self.config.meter_value_sample_interval == 0:
            return  # §9.1: 0 means "no sampled data should be transmitted"
        self._sampling_tasks[connector_id] = self._lifetime_tasks.create_task(
            self._sampling_loop(transaction, since=self._now())
        )

    async def _sampling_loop(self, transaction: ChargerTransaction, since: datetime) -> None:
        """A sample every MeterValueSampleInterval from `since` (§9.1: "from the start of
        the charging transaction"), or from the interval change.

        Each wait is computed from that schedule, not "interval after the last wake-up", so
        however late the task starts or wakes, samples stay on time and don't drift.
        """
        interval = timedelta(seconds=self.config.meter_value_sample_interval)
        next_sample = since + interval
        while True:
            await self._sleep(max(0.0, (next_sample - self._now()).total_seconds()))
            self._queue_message(transaction, self._meter_values(transaction.connector_id))
            next_sample += interval

    def _meter_values(self, connector_id: int) -> call.MeterValues:
        """A periodic sample of what MeterValuesSampledData lists."""
        meter = self._meters[connector_id]
        sampled = []
        for measurand in self.config.sampled_measurands:
            if measurand == Measurand.energy_active_import_register:
                value, unit = meter.register_wh(), UnitOfMeasure.wh
            else:  # Power.Active.Import, the only other one the config accepts
                value, unit = meter.power_w, UnitOfMeasure.w
            sampled.append(
                SampledValue(
                    value=str(value),
                    context=ReadingContext.sample_periodic,
                    measurand=measurand,
                    unit=unit,  # explicit: without it a reading means Wh (§7.43)
                )
            )
        return call.MeterValues(
            connector_id=connector_id,
            meter_value=[MeterValue(timestamp=self._now().isoformat(), sampled_value=sampled)],
            transaction_id=UNKNOWN_TRANSACTION_ID,  # placeholder, set when sent
        )

    # --- Called by the protocol handlers ---

    def change_configuration(self, key: str, value: str) -> ConfigurationStatus:
        status = self.config.change(key, value)
        if status != ConfigurationStatus.accepted:
            return status
        # Both take effect now, not after the current wait.
        if key == "HeartbeatInterval":
            self._restart_heartbeat()
        if key == "MeterValueSampleInterval":
            for connector_id in self._transactions:
                self._restart_sampling(connector_id)
        return status

    def change_availability(
        self, connector_id: int, availability: AvailabilityType
    ) -> AvailabilityStatus:
        """Record a ChangeAvailability request and return the answer (§5.2).

        Only records: the status changes are made by apply_availability_later(), called once
        the answer has been sent, so the CSMS gets the answer before the StatusNotification.

        Connector 0 means the charge point and all its connectors (§5.2). Accepted if every
        connector concerned can change now (or already is as asked), Scheduled if one is in
        use: §5.2 says so for a transaction in progress; we also schedule for Preparing and
        Finishing, which have no direct path to Unavailable in the §4.9 table. A Faulted
        connector stays Faulted (Accepted, recorded for when the fault clears).
        """
        if connector_id not in self._connectors:
            return AvailabilityStatus.rejected
        operative = availability == AvailabilityType.operative
        concerned = self.connector_ids if connector_id == 0 else [connector_id]
        for i in concerned:
            self._operative[i] = operative
        if any(self._waits_for_end_of_use(i) for i in concerned):
            return AvailabilityStatus.scheduled
        return AvailabilityStatus.accepted

    def apply_availability_later(self) -> None:
        """Apply recorded availability changes in a session task (from a protocol handler,
        which must not wait for its own StatusNotifications)."""
        self._start_session_task(self._apply_availability)

    async def _apply_availability(self) -> None:
        """Bring every connector's status in line with its requested availability, except
        connectors in use: they change at the end of use (see _end_use).

        Connector 0 changes last, once no connector 1..N still has a change waiting: with a
        session in progress, a Scheduled charge-point-wide change is not done yet.
        """
        for i in self.connector_ids[1:]:
            if self._needs_change(i) and not self._waits_for_end_of_use(i):
                await self.set_status(i, S.available if self._operative[i] else S.unavailable)
        waiting = any(self._needs_change(i) for i in self.connector_ids[1:])
        if self._needs_change(0) and not waiting:
            await self.set_status(0, S.available if self._operative[0] else S.unavailable)

    def _needs_change(self, connector_id: int) -> bool:
        """The status doesn't match the requested availability (Faulted is left alone)."""
        status = self._connectors[connector_id].status
        if self._operative[connector_id]:
            return status == S.unavailable
        return status not in (S.unavailable, S.faulted)

    def _waits_for_end_of_use(self, connector_id: int) -> bool:
        return self._needs_change(connector_id) and self._connectors[connector_id].status in IN_USE

    def reset_later(self, reset_type: ResetType) -> None:
        """Start an accepted Reset, once its answer has been sent (protocol @after handler).

        It runs as a session task, tracked and cancelled with the session, rather than in
        the untracked task ocpp creates for an async @after handler.
        """
        self._start_session_task(lambda: self._reset(reset_type))

    async def _reset(self, reset_type: ResetType) -> None:
        """Close the connection to reboot; run() then calls _reboot().

        §5.14 (as rewritten by errata 3.36): before a soft reset, stop ongoing transactions
        gracefully and send their StopTransaction; one that gets no answer stays queued. A
        hard reset restarts at once: the StopTransaction is sent after the reboot.
        """
        logger.info("%s: %s reset", self.id, reset_type.value)
        # Recorded first: if the session ends while the soft reset stops transactions (e.g.
        # a StopTransaction times out), run() still reboots instead of just reconnecting.
        self._reset_type = reset_type
        if reset_type == ResetType.soft:
            for connector_id in list(self._transactions):
                await self._stop_transaction(connector_id, Reason.soft_reset)
            for connector_id in self.connector_ids[1:]:
                stop = self._queue.last_for(connector_id)
                if stop is not None:
                    await self._queue.wait_for_first_attempt(stop)
        if self._websocket is not None:
            await self._websocket.close()

    async def _reboot(self, reset_type: ResetType) -> None:
        """The simulated reboot, offline, between the reset and the next connection.

        Hard: transactions end without a word to the CSMS. Each one's StopTransaction
        (reason HardReset, meter and time of the reset) waits in the queue, which is
        non-volatile, and goes out after the next accepted boot (§5.14). Then the restart:
        authorizations in progress are lost; config, queue, meters and requested
        availability are kept. A connector with a cable still plugged in reports
        `plugged_status_after_reboot`, and availability changes that were waiting are
        applied. All of it is reported by the boot's StatusNotifications.
        """
        for connector_id in list(self._transactions):
            # Soft: already stopped before the reset, unless one started in between.
            reason = Reason.hard_reset if reset_type == ResetType.hard else Reason.soft_reset
            await self._stop_transaction(connector_id, reason)
        self._authorized_tags.clear()
        await self._sleep(self._reboot_delay)
        after = self._plugged_status_after_reboot
        for connector_id in self.connector_ids[1:]:
            # Only Preparing and Finishing are left: every transaction was stopped above.
            status = self._connectors[connector_id].status
            if status in IN_USE and status != after:
                await self.set_status(connector_id, after)
        await self._apply_availability()
