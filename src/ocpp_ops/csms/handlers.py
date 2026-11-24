"""CSMS-side OCPP 1.6 handlers for one charger connection.

The ocpp library validates each incoming payload against the 1.6 JSON schema and turns its
camelCase keys into snake_case keyword arguments before calling these handlers. `**_` absorbs
the optional fields we don't use yet.
"""

from datetime import datetime
from typing import Any

from ocpp.routing import on
from ocpp.v16 import ChargePoint, call_result
from ocpp.v16.enums import Action, ChargePointErrorCode, ChargePointStatus, RegistrationStatus

from ocpp_ops.clock import Clock
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
        heartbeat_interval: int,
        now: Clock,
    ) -> None:
        super().__init__(charger_id, connection)
        self._registry = registry
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
        self, connector_id: int, meter_value: list[dict[str, Any]], **_: Any
    ) -> call_result.MeterValues:
        readings = [
            MeterReading(timestamp=mv["timestamp"], sampled_values=mv["sampled_value"])
            for mv in meter_value
        ]
        if readings:
            # One message can carry several readings; keep the most recent one.
            latest = max(readings, key=lambda reading: reading.timestamp)
            self._registry.record_meter_value(self.id, connector_id, latest)
        return call_result.MeterValues()
