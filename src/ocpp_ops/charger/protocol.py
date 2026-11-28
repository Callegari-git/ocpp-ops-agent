"""OCPP 1.6 protocol side of a virtual charger: answers CSMS commands, sends requests.

The handlers only translate OCPP to and from VirtualCharger calls; the behavior lives there.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from ocpp.routing import after, on
from ocpp.v16 import ChargePoint, call_result
from ocpp.v16.enums import Action, AvailabilityType, ResetStatus, ResetType

from ocpp_ops.cancellation import raise_if_cancelling
from ocpp_ops.message_log import Connection

if TYPE_CHECKING:  # only for type hints; avoids a circular import at runtime
    from ocpp_ops.charger.charger import VirtualCharger


class ChargerChargePoint(ChargePoint):
    def __init__(
        self,
        charger: VirtualCharger,
        connection: Connection,
        new_message_id: Callable[[], str],
        response_timeout: float,
    ) -> None:
        super().__init__(charger.id, connection, response_timeout=response_timeout)
        self._charger = charger
        self._new_message_id = new_message_id
        # See send(). asyncio.Lock serves waiters first come, first served.
        self._send_lock = asyncio.Lock()

    async def send(self, request: Any) -> Any:
        """Send a CALL to the CSMS and return its response.

        CALLs go out in the order send() was called. ocpp's own lock is not enough: call()
        validates the payload in an executor thread *before* taking it, so a CALL whose
        validation finishes first can overtake an earlier one. Our lock is taken before
        call() even starts.

        Message ids come from the charger's seeded RNG instead of ocpp's default uuid4(),
        so the same seed gives the same ids. suppress=False: a CALLERROR raises.
        """
        async with self._send_lock:
            response = await self.call(request, unique_id=self._new_message_id(), suppress=False)
            # On Python 3.11 call() can swallow a cancellation (see raise_if_cancelling):
            # without this, a stopped session would carry on booting or heartbeating. The
            # CancelledError leaves the `async with`, which releases the lock.
            raise_if_cancelling()
        return response

    @on(Action.get_configuration)
    def on_get_configuration(self, key: list[str] | None = None) -> call_result.GetConfiguration:
        known, unknown = self._charger.config.get(key)
        configuration = [
            {"key": name, "readonly": entry.readonly, "value": entry.value}
            for name, entry in known.items()
        ]
        return call_result.GetConfiguration(
            configuration_key=configuration or None, unknown_key=unknown or None
        )

    @on(Action.change_configuration)
    def on_change_configuration(self, key: str, value: str) -> call_result.ChangeConfiguration:
        return call_result.ChangeConfiguration(
            status=self._charger.change_configuration(key, value)
        )

    @on(Action.reset)
    def on_reset(self, type: str) -> call_result.Reset:
        return call_result.Reset(status=ResetStatus.accepted)

    @after(Action.reset)
    def after_reset(self, type: str) -> None:
        # Runs once the Accepted answer has been sent, so the CSMS gets it before we go down.
        # Synchronous on purpose: ocpp runs an async @after handler in an untracked task.
        self._charger.reset_later(ResetType(type))

    @on(Action.change_availability)
    def on_change_availability(
        self, connector_id: int, type: str
    ) -> call_result.ChangeAvailability:
        status = self._charger.change_availability(connector_id, AvailabilityType(type))
        return call_result.ChangeAvailability(status=status)

    @after(Action.change_availability)
    def after_change_availability(self, connector_id: int, type: str) -> None:
        # §5.2: the StatusNotification reports the change once it has happened, so after
        # the answer. Not from the handler itself: ocpp waits for a handler before reading
        # the next message, so a handler waiting for the CSMS's answer would block it.
        self._charger.apply_availability_later()
