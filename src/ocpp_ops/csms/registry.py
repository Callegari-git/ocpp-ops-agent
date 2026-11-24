"""In-memory registry of chargers as the CSMS sees them.

No OCPP here: handlers translate messages into calls on the registry, so this module can be
tested on its own. All methods are synchronous and run on the single asyncio thread, so no
lock is needed.
"""

import itertools
from datetime import datetime

from ocpp.v16.enums import ChargePointErrorCode, ChargePointStatus
from pydantic import BaseModel, ConfigDict, Field

from ocpp_ops.clock import Clock


class _Model(BaseModel):
    # Registry state is updated field by field; validate those assignments too.
    model_config = ConfigDict(validate_assignment=True)


class SampledValue(_Model):
    """One sampled value of a MeterValues message (OCPP 1.6 SampledValue type)."""

    # OCPP 1.6 sends meter readings as strings.
    value: str
    # When absent, the spec default is "Energy.Active.Import.Register". We store what was sent.
    measurand: str | None = None
    unit: str | None = None
    phase: str | None = None
    context: str | None = None
    location: str | None = None
    format: str | None = None


class MeterReading(_Model):
    """Sampled values taken at one point in time, by the charger's clock."""

    timestamp: datetime
    sampled_values: list[SampledValue]


class ConnectorState(_Model):
    """Last known state of one connector."""

    status: ChargePointStatus | None = None
    error_code: ChargePointErrorCode | None = None
    info: str | None = None
    # Time of the last StatusNotification by the charger's clock (optional in the message)...
    timestamp: datetime | None = None
    # ...and by ours. Comparing the two shows clock drift.
    received_at: datetime | None = None
    last_meter_value: MeterReading | None = None


class ChargerState(_Model):
    """Everything the CSMS knows about one charger."""

    id: str
    vendor: str | None = None
    model: str | None = None
    connected: bool = False
    boot_time: datetime | None = None
    last_heartbeat: datetime | None = None
    known_config: dict[str, str] = Field(default_factory=dict)
    # OCPP 1.6 §4.9: connectorId 0 is the charge point as a whole; 1..N are the connectors.
    connectors: dict[int, ConnectorState] = Field(default_factory=dict)


class Registry:
    """Chargers by id. Callers read the returned models but only change them through here."""

    def __init__(self, now: Clock) -> None:
        self._now = now
        self._chargers: dict[str, ChargerState] = {}
        # Token of the connection that currently owns each charger id (see disconnect()).
        self._current_connection: dict[str, int] = {}
        self._tokens = itertools.count(1)

    def connect(self, charger_id: str) -> int:
        """Mark a charger connected. Returns a token identifying this connection."""
        token = next(self._tokens)
        self._current_connection[charger_id] = token
        charger = self._chargers.setdefault(charger_id, ChargerState(id=charger_id))
        charger.connected = True
        return token

    def disconnect(self, charger_id: str, token: int) -> None:
        """Mark a charger disconnected, unless a newer connection has taken over its id.

        A charger can reconnect before its old connection is detected as dead. When that
        old connection finally closes, it must not mark the live one as offline.
        """
        if self._current_connection.get(charger_id) != token:
            return
        del self._current_connection[charger_id]
        self._chargers[charger_id].connected = False

    def record_boot(self, charger_id: str, vendor: str, model: str) -> None:
        charger = self._chargers[charger_id]
        charger.vendor = vendor
        charger.model = model
        charger.boot_time = self._now()

    def record_heartbeat(self, charger_id: str) -> datetime:
        """Store the heartbeat time and return it (it is also the CSMS's answer)."""
        now = self._now()
        self._chargers[charger_id].last_heartbeat = now
        return now

    def record_status(
        self,
        charger_id: str,
        connector_id: int,
        status: ChargePointStatus,
        error_code: ChargePointErrorCode,
        info: str | None = None,
        timestamp: datetime | None = None,
    ) -> None:
        connector = self._connector(charger_id, connector_id)
        connector.status = status
        connector.error_code = error_code
        connector.info = info
        connector.timestamp = timestamp
        connector.received_at = self._now()

    def record_meter_value(self, charger_id: str, connector_id: int, reading: MeterReading) -> None:
        self._connector(charger_id, connector_id).last_meter_value = reading

    def get(self, charger_id: str) -> ChargerState | None:
        return self._chargers.get(charger_id)

    def all(self) -> list[ChargerState]:
        return [self._chargers[charger_id] for charger_id in sorted(self._chargers)]

    def _connector(self, charger_id: str, connector_id: int) -> ConnectorState:
        connectors = self._chargers[charger_id].connectors
        return connectors.setdefault(connector_id, ConnectorState())
