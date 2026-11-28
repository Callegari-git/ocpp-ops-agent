"""CSMS-side OCPP 1.6 handlers for one charger connection.

The ocpp library validates each incoming payload against the 1.6 JSON schema and turns its
camelCase keys into snake_case keyword arguments before calling these handlers. `**_` absorbs
the optional fields we don't use yet.
"""

from datetime import datetime
from typing import Any

from ocpp.routing import on
from ocpp.v16 import ChargePoint, call_result
from ocpp.v16.datatypes import IdTagInfo
from ocpp.v16.enums import (
    Action,
    ChargePointErrorCode,
    ChargePointStatus,
    Reason,
    RegistrationStatus,
)

from ocpp_ops.clock import Clock
from ocpp_ops.csms.authorization import AuthorizationList
from ocpp_ops.csms.registry import MeterReading, Registry
from ocpp_ops.message_log import Connection


class CsmsChargePoint(ChargePoint):
    """Answers one charger's requests and keeps the registry up to date.

    Every handler updates the registry *before* returning its response, so by the time the
    charger gets the answer, the registry already reflects the request.
    """

    def __init__(
        self,
        charger_id: str,
        connection: Connection,
        registry: Registry,
        authorization: AuthorizationList,
        heartbeat_interval: int,
        now: Clock,
        response_timeout: float,
    ) -> None:
        super().__init__(charger_id, connection, response_timeout=response_timeout)
        self._registry = registry
        self._authorization = authorization
        self._heartbeat_interval = heartbeat_interval
        self._now = now

    @on(Action.boot_notification)
    def on_boot_notification(
        self, charge_point_vendor: str, charge_point_model: str, **_: Any
    ) -> call_result.BootNotification:
        self._registry.record_boot(self.id, vendor=charge_point_vendor, model=charge_point_model)
        return call_result.BootNotification(
            current_time=self._now().isoformat(),
            interval=self._heartbeat_interval,
            status=RegistrationStatus.accepted,
        )

    @on(Action.heartbeat)
    def on_heartbeat(self) -> call_result.Heartbeat:
        now = self._registry.record_heartbeat(self.id)
        return call_result.Heartbeat(current_time=now.isoformat())

    @on(Action.status_notification)
    def on_status_notification(
        self,
        connector_id: int,
        error_code: str,
        status: str,
        info: str | None = None,
        timestamp: str | None = None,
        **_: Any,
    ) -> call_result.StatusNotification:
        self._registry.record_status(
            self.id,
            connector_id,
            status=ChargePointStatus(status),
            error_code=ChargePointErrorCode(error_code),
            info=info,
            timestamp=datetime.fromisoformat(timestamp) if timestamp else None,
        )
        return call_result.StatusNotification()

    @on(Action.meter_values)
    def on_meter_values(
        self,
        connector_id: int,
        meter_value: list[dict[str, Any]],
        transaction_id: int | None = None,
        **_: Any,
    ) -> call_result.MeterValues:
        readings = [
            MeterReading(timestamp=mv["timestamp"], sampled_values=mv["sampled_value"])
            for mv in meter_value
        ]
        if readings:
            # One message can carry several readings; keep the most recent one.
            latest = max(readings, key=lambda reading: reading.timestamp)
            self._registry.record_meter_value(self.id, connector_id, latest, transaction_id)
        return call_result.MeterValues()

    @on(Action.authorize)
    def on_authorize(self, id_tag: str) -> call_result.Authorize:
        return call_result.Authorize(id_tag_info=self._id_tag_info(id_tag))

    @on(Action.start_transaction)
    def on_start_transaction(
        self, connector_id: int, id_tag: str, meter_start: int, timestamp: str, **_: Any
    ) -> call_result.StartTransaction:
        # OCPP 1.6 §4.8: the CSMS must check the idTag again, and always answer with a
        # transaction id, even for a tag it refuses: the charger has already started.
        id_tag_info = self._id_tag_info(id_tag)
        transaction_id = self._registry.start_transaction(
            self.id,
            connector_id,
            id_tag=id_tag,
            id_tag_status=id_tag_info.status,
            meter_start=meter_start,
            started_at=datetime.fromisoformat(timestamp),
        )
        return call_result.StartTransaction(transaction_id=transaction_id, id_tag_info=id_tag_info)

    @on(Action.stop_transaction)
    def on_stop_transaction(
        self,
        meter_stop: int,
        timestamp: str,
        transaction_id: int,
        id_tag: str | None = None,
        reason: str | None = None,
        **_: Any,  # transactionData: not used yet
    ) -> call_result.StopTransaction:
        self._registry.stop_transaction(
            self.id,
            transaction_id,
            meter_stop=meter_stop,
            stopped_at=datetime.fromisoformat(timestamp),
            # §4.10: an absent reason means Local.
            reason=Reason(reason) if reason else Reason.local,
        )
        # §6.50: idTagInfo is optional; we send it when the charger told us who stopped.
        id_tag_info = self._id_tag_info(id_tag) if id_tag else None
        return call_result.StopTransaction(id_tag_info=id_tag_info)

    def _id_tag_info(self, id_tag: str) -> IdTagInfo:
        return IdTagInfo(status=self._authorization.status(id_tag))
