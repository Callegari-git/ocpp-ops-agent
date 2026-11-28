"""Results and errors of the CSMS's outbound commands (CSMS -> charger)."""

from ocpp.v16.enums import (
    AvailabilityStatus,
    AvailabilityType,
    ConfigurationStatus,
    ResetStatus,
    ResetType,
)
from pydantic import BaseModel

from ocpp_ops.csms.registry import ConfigurationKey


class GetConfigurationResult(BaseModel):
    configuration: list[ConfigurationKey]
    unknown_keys: list[str]


class ChangeConfigurationResult(BaseModel):
    key: str
    value: str
    status: ConfigurationStatus


class ResetResult(BaseModel):
    type: ResetType
    status: ResetStatus


class ChangeAvailabilityResult(BaseModel):
    connector_id: int  # 0: the charge point and all its connectors
    type: AvailabilityType
    # Scheduled: the change waits for the end of a transaction in progress (§5.2).
    status: AvailabilityStatus


class CommandError(Exception):
    """An outbound command could not get an answer from the charger."""


class ChargerNotConnectedError(CommandError):
    def __init__(self, charger_id: str) -> None:
        super().__init__(f"Charger {charger_id!r} is not connected")
        self.charger_id = charger_id


class CommandTimeoutError(CommandError):
    def __init__(self, charger_id: str, action: str, timeout: float) -> None:
        super().__init__(f"Charger {charger_id!r} did not answer {action} within {timeout}s")
        self.charger_id = charger_id
        self.action = action
        self.timeout = timeout


class ChargerCallError(CommandError):
    """The charger answered with a CALLERROR, or with a response that failed validation."""

    def __init__(self, charger_id: str, action: str, error_code: str, description: str) -> None:
        super().__init__(
            f"Charger {charger_id!r} answered {action} with {error_code}: {description}"
        )
        self.charger_id = charger_id
        self.action = action
        self.error_code = error_code
        self.description = description
