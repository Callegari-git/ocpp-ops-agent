"""In-memory registry of chargers as the CSMS sees them.

No OCPP here: handlers translate messages into calls on the registry, so this module can be
tested on its own. All methods are synchronous and run on the single asyncio thread, so no
lock is needed.
"""

import itertools
from datetime import datetime
from typing import Literal

from ocpp.v16.enums import (
    AuthorizationStatus,
    ChargePointErrorCode,
    ChargePointStatus,
    ConfigurationStatus,
    Reason,
)
from pydantic import BaseModel, ConfigDict, Field

from ocpp_ops.clock import Clock

# Per charger: enough for the agent to see recent sessions, bounded so memory stays flat.
MAX_FINISHED_TRANSACTIONS = 50
MAX_ANOMALIES = 100


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


ENERGY_REGISTER = "Energy.Active.Import.Register"
# OCPP 1.6 §7.31: the energy register is reported in Wh or kWh.
_WH_PER_UNIT = {"Wh": 1.0, "kWh": 1000.0}


def energy_register_wh(reading: MeterReading) -> float | None:
    """The energy register (overall, not per phase) of a reading in Wh, or None if absent.

    §7.43: a sampled value without measurand is the energy register, and without unit it is
    in Wh. Values we can't read as numbers (e.g. format SignedData) are skipped.
    """
    for sampled in reading.sampled_values:
        if (sampled.measurand or ENERGY_REGISTER) != ENERGY_REGISTER or sampled.phase:
            continue
        factor = _WH_PER_UNIT.get(sampled.unit or "Wh")
        if factor is None:
            continue
        try:
            return float(sampled.value) * factor
        except ValueError:
            continue
    return None


class Transaction(_Model):
    """A transaction in progress, as the CSMS sees it."""

    id: int
    connector_id: int
    id_tag: str
    # What we answered in StartTransaction.conf. The charger may charge on a non-Accepted
    # tag anyway (StopTransactionOnInvalidId=false, OCPP 1.6 §3.5.4), so we keep it.
    id_tag_status: AuthorizationStatus
    meter_start: int  # Wh, the connector's register at the start
    started_at: datetime  # by the charger's clock
    last_meter_value: MeterReading | None = None
    # Last energy register reading minus meter_start: energy delivered so far.
    energy_wh: float = 0.0


class FinishedTransaction(Transaction):
    meter_stop: int  # Wh
    stopped_at: datetime  # by the charger's clock
    stop_reason: Reason
    # From the charger's timestamps, not from when the messages reached us: a StopTransaction
    # queued while the charger was offline still gives the real duration.
    duration_s: float


AnomalyKind = Literal["unknown_transaction", "start_on_busy_connector"]


class Anomaly(_Model):
    """Something a charger sent that doesn't fit what we know. It was answered anyway:
    OCPP 1.6 §4.7/§4.10 say sanity checks must never stop the CSMS from responding."""

    at: datetime  # by our clock
    kind: AnomalyKind
    action: str
    transaction_id: int | None
    detail: str


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


class ConfigurationKey(_Model):
    """One configuration key as reported by the charger (OCPP 1.6 KeyValue type)."""

    key: str
    readonly: bool
    # Optional in OCPP 1.6: a key can exist without a value.
    value: str | None = None


class ConfigChange(_Model):
    """The charger's answer to our last ChangeConfiguration for one key."""

    value: str
    status: ConfigurationStatus
    at: datetime


class ChargerState(_Model):
    """Everything the CSMS knows about one charger."""

    id: str
    vendor: str | None = None
    model: str | None = None
    connected: bool = False
    boot_time: datetime | None = None
    last_heartbeat: datetime | None = None
    # What the charger itself reported via GetConfiguration, by key.
    known_config: dict[str, ConfigurationKey] = Field(default_factory=dict)
    # Last ChangeConfiguration answer per key. It does NOT update known_config, even when
    # Accepted: only a new GetConfiguration tells us what the charger really holds.
    config_changes: dict[str, ConfigChange] = Field(default_factory=dict)
    # OCPP 1.6 §4.9: connectorId 0 is the charge point as a whole; 1..N are the connectors.
    connectors: dict[int, ConnectorState] = Field(default_factory=dict)
    # By transaction id, not by connector: StopTransaction and MeterValues only carry the id,
    # and a second start on a busy connector must not overwrite the first transaction.
    active_transactions: dict[int, Transaction] = Field(default_factory=dict)
    finished_transactions: list[FinishedTransaction] = Field(default_factory=list)
    anomalies: list[Anomaly] = Field(default_factory=list)

    def transaction_on(self, connector_id: int) -> Transaction | None:
        """The most recent active transaction on a connector, if any."""
        on_connector = [
            t for t in self.active_transactions.values() if t.connector_id == connector_id
        ]
        return max(on_connector, key=lambda t: t.id, default=None)


class Registry:
    """Chargers by id. Callers read the returned models but only change them through here."""

    def __init__(self, now: Clock) -> None:
        self._now = now
        self._chargers: dict[str, ChargerState] = {}
        # Token of the connection that currently owns each charger id (see disconnect()).
        self._current_connection: dict[str, int] = {}
        self._tokens = itertools.count(1)
        # Unique positive ids for the whole run, as OCPP 1.6 errata 3.54 recommends.
        self._transaction_ids = itertools.count(1)

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

    def record_meter_value(
        self,
        charger_id: str,
        connector_id: int,
        reading: MeterReading,
        transaction_id: int | None = None,
    ) -> None:
        self._connector(charger_id, connector_id).last_meter_value = reading
        if transaction_id is None:
            return
        transaction = self._chargers[charger_id].active_transactions.get(transaction_id)
        if transaction is None:
            self._unknown_transaction(charger_id, "MeterValues", transaction_id)
            return
        transaction.last_meter_value = reading
        register = energy_register_wh(reading)
        if register is not None:
            transaction.energy_wh = register - transaction.meter_start

    def start_transaction(
        self,
        charger_id: str,
        connector_id: int,
        id_tag: str,
        id_tag_status: AuthorizationStatus,
        meter_start: int,
        started_at: datetime,
    ) -> int:
        """Record a new transaction and return its id (the CSMS's answer)."""
        charger = self._chargers[charger_id]
        busy = charger.transaction_on(connector_id)
        if busy is not None:
            self._add_anomaly(
                charger_id,
                "start_on_busy_connector",
                "StartTransaction",
                busy.id,
                f"connector {connector_id} already has active transaction {busy.id}",
            )
        transaction_id = next(self._transaction_ids)
        charger.active_transactions[transaction_id] = Transaction(
            id=transaction_id,
            connector_id=connector_id,
            id_tag=id_tag,
            id_tag_status=id_tag_status,
            meter_start=meter_start,
            started_at=started_at,
        )
        return transaction_id

    def stop_transaction(
        self,
        charger_id: str,
        transaction_id: int,
        meter_stop: int,
        stopped_at: datetime,
        reason: Reason,
    ) -> None:
        """Move a transaction to the history. An unknown id is recorded as an anomaly."""
        charger = self._chargers[charger_id]
        transaction = charger.active_transactions.pop(transaction_id, None)
        if transaction is None:
            self._unknown_transaction(charger_id, "StopTransaction", transaction_id)
            return
        finished = FinishedTransaction(
            **transaction.model_dump(exclude={"energy_wh"}),
            energy_wh=meter_stop - transaction.meter_start,
            meter_stop=meter_stop,
            stopped_at=stopped_at,
            stop_reason=reason,
            duration_s=(stopped_at - transaction.started_at).total_seconds(),
        )
        charger.finished_transactions.append(finished)
        del charger.finished_transactions[:-MAX_FINISHED_TRANSACTIONS]

    def record_configuration(
        self, charger_id: str, keys: list[ConfigurationKey], unknown_keys: list[str]
    ) -> None:
        """Merge a GetConfiguration answer: update reported keys, forget unknown ones."""
        known_config = self._chargers[charger_id].known_config
        for item in keys:
            known_config[item.key] = item
        for key in unknown_keys:
            known_config.pop(key, None)

    def record_config_change(
        self, charger_id: str, key: str, value: str, status: ConfigurationStatus
    ) -> None:
        change = ConfigChange(value=value, status=status, at=self._now())
        self._chargers[charger_id].config_changes[key] = change

    def get(self, charger_id: str) -> ChargerState | None:
        return self._chargers.get(charger_id)

    def all(self) -> list[ChargerState]:
        return [self._chargers[charger_id] for charger_id in sorted(self._chargers)]

    def _unknown_transaction(self, charger_id: str, action: str, transaction_id: int) -> None:
        finished = any(
            t.id == transaction_id for t in self._chargers[charger_id].finished_transactions
        )
        detail = "already finished" if finished else "not an active transaction of this charger"
        self._add_anomaly(charger_id, "unknown_transaction", action, transaction_id, detail)

    def _add_anomaly(
        self,
        charger_id: str,
        kind: AnomalyKind,
        action: str,
        transaction_id: int | None,
        detail: str,
    ) -> None:
        anomalies = self._chargers[charger_id].anomalies
        anomalies.append(
            Anomaly(
                at=self._now(),
                kind=kind,
                action=action,
                transaction_id=transaction_id,
                detail=detail,
            )
        )
        del anomalies[:-MAX_ANOMALIES]

    def _connector(self, charger_id: str, connector_id: int) -> ConnectorState:
        connectors = self._chargers[charger_id].connectors
        return connectors.setdefault(connector_id, ConnectorState())
