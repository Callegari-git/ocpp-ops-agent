"""The Central System: serves charger connections and sends commands to chargers."""

from datetime import timedelta
from typing import Any

from ocpp.exceptions import OCPPError
from ocpp.v16 import call
from ocpp.v16.enums import ConfigurationStatus, ResetStatus, ResetType
from websockets.exceptions import ConnectionClosed

from ocpp_ops.cancellation import raise_if_cancelling
from ocpp_ops.clock import Clock
from ocpp_ops.csms.commands import (
    ChangeConfigurationResult,
    ChargerCallError,
    ChargerNotConnectedError,
    CommandTimeoutError,
    GetConfigurationResult,
    ResetResult,
)
from ocpp_ops.csms.handlers import CsmsChargePoint
from ocpp_ops.csms.registry import ConfigurationKey, Registry
from ocpp_ops.message_log import Connection, LoggingConnection, MessageLog, describe_close


class CentralSystem:
    """Serves charger connections and sends commands to connected chargers.

    Commands are mechanism only: safety checks (guardrails) belong to the agent layer, week 3.

    One command at a time per charger: ocpp's ChargePoint.call() holds a lock while it
    waits for the answer (OCPP-J 1.6 §4.1.1, one outstanding CALL per side). A charger that
    never answers therefore blocks every command *to that charger* until `call_timeout`
    expires. Commands to other chargers are not affected.
    """

    def __init__(
        self,
        registry: Registry,
        log: MessageLog,
        heartbeat_interval: int,
        now: Clock,
        call_timeout: float = 30.0,
    ) -> None:
        self.registry = registry
        self._log = log
        self._heartbeat_interval = heartbeat_interval
        self._now = now
        self._call_timeout = call_timeout
        # The live session for each charger id: where commands are sent.
        self._charge_points: dict[str, CsmsChargePoint] = {}

    async def handle_connection(self, connection: Connection, charger_id: str) -> None:
        """Serve one charger from connect to disconnect."""
        token = self.registry.connect(charger_id)
        # Twice the call timeout, so a late answer is still logged with its action.
        pending_ttl = timedelta(seconds=2 * self._call_timeout)
        logging_connection = LoggingConnection(
            connection, charger_id, self._log, self._now, pending_ttl
        )
        charge_point = CsmsChargePoint(
            charger_id,
            logging_connection,
            self.registry,
            self._heartbeat_interval,
            self._now,
            response_timeout=self._call_timeout,
        )
        # A newer connection for the same id replaces the old one as the command target.
        self._charge_points[charger_id] = charge_point
        logging_connection.record_event("CONNECTED", {})
        close_info: dict[str, Any] = {"reason": "error", "close_code": None, "detail": None}
        try:
            await charge_point.start()
        except ConnectionClosed as closed:
            close_info = describe_close(closed)
        except BaseException as error:
            # Includes cancellation. Re-raised so websockets logs it.
            close_info = {"reason": "error", "close_code": None, "detail": repr(error)}
            raise
        finally:
            # Runs on every exit path, so a dead charger is never shown as connected.
            if self._charge_points.get(charger_id) is charge_point:
                del self._charge_points[charger_id]
            self.registry.disconnect(charger_id, token)
            logging_connection.record_event("DISCONNECTED", close_info)

    async def get_configuration(
        self, charger_id: str, keys: list[str] | None = None
    ) -> GetConfigurationResult:
        """Read configuration keys (all of them if `keys` is None). Updates the registry."""
        response = await self._call(charger_id, call.GetConfiguration(key=keys))
        configuration = [
            ConfigurationKey.model_validate(item) for item in response.configuration_key or []
        ]
        unknown_keys = list(response.unknown_key or [])
        self.registry.record_configuration(charger_id, configuration, unknown_keys)
        return GetConfigurationResult(configuration=configuration, unknown_keys=unknown_keys)

    async def change_configuration(
        self, charger_id: str, key: str, value: str
    ) -> ChangeConfigurationResult:
        """Ask the charger to change one key. Records its answer in the registry."""
        response = await self._call(charger_id, call.ChangeConfiguration(key=key, value=value))
        status = ConfigurationStatus(response.status)
        self.registry.record_config_change(charger_id, key, value, status)
        return ChangeConfigurationResult(key=key, value=value, status=status)

    async def reset(self, charger_id: str, type: ResetType) -> ResetResult:
        """Ask the charger to reboot (Soft or Hard)."""
        response = await self._call(charger_id, call.Reset(type=type))
        return ResetResult(type=type, status=ResetStatus(response.status))

    async def _call(self, charger_id: str, request: Any) -> Any:
        """Send one CALL and return the charger's response payload, or raise a CommandError."""
        charge_point = self._charge_points.get(charger_id)
        if charge_point is None:
            raise ChargerNotConnectedError(charger_id)
        action = request.__class__.__name__
        try:
            # suppress=False: by default ocpp returns None on a CALLERROR; we want an error.
            response = await charge_point.call(request, suppress=False)
        except TimeoutError as error:
            raise CommandTimeoutError(charger_id, action, self._call_timeout) from error
        except ConnectionClosed as error:
            raise ChargerNotConnectedError(charger_id) from error
        except OCPPError as error:
            error_code = getattr(error, "code", error.__class__.__name__)
            raise ChargerCallError(charger_id, action, error_code, error.description) from error
        # A cancelled command must not return a result, even on Python 3.11 where call() can
        # swallow the cancellation (see raise_if_cancelling).
        raise_if_cancelling()
        return response
