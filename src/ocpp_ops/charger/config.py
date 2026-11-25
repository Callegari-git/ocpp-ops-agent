"""Configuration store of a virtual charger: standard OCPP 1.6 keys (spec §9.1, Core profile).

Pure, no I/O. It lives on the VirtualCharger, not on a connection, so it survives reconnects
and resets, like the non-volatile config of a real charger.
"""

from dataclasses import dataclass
from typing import Literal

from ocpp.v16.enums import ConfigurationStatus

ValueKind = Literal["int", "bool", "text"]


@dataclass
class ConfigValue:
    value: str
    readonly: bool
    kind: ValueKind


def standard_keys(number_of_connectors: int) -> dict[str, ConfigValue]:
    """The keys every virtual charger starts with. OCPP 1.6 sends all values as strings."""

    def rw(value: str, kind: ValueKind) -> ConfigValue:
        return ConfigValue(value, readonly=False, kind=kind)

    def ro(value: str, kind: ValueKind) -> ConfigValue:
        return ConfigValue(value, readonly=True, kind=kind)

    return {
        # Read-only: facts about the hardware and firmware.
        "NumberOfConnectors": ro(str(number_of_connectors), "int"),
        "SupportedFeatureProfiles": ro("Core", "text"),
        "GetConfigurationMaxKeys": ro("50", "int"),
        # Writable.
        "HeartbeatInterval": rw("60", "int"),
        "MeterValueSampleInterval": rw("60", "int"),
        "ConnectionTimeOut": rw("60", "int"),
        "ClockAlignedDataInterval": rw("0", "int"),
        "MeterValuesSampledData": rw("Energy.Active.Import.Register", "text"),
        "ResetRetries": rw("3", "int"),
        "WebSocketPingInterval": rw("0", "int"),
        "LocalAuthorizeOffline": rw("true", "bool"),
        "LocalPreAuthorize": rw("false", "bool"),
        "AuthorizeRemoteTxRequests": rw("true", "bool"),
        "StopTransactionOnEVSideDisconnect": rw("true", "bool"),
        "UnlockConnectorOnEVSideDisconnect": rw("true", "bool"),
        "TransactionMessageAttempts": rw("3", "int"),
        "TransactionMessageRetryInterval": rw("60", "int"),
    }


class ConfigStore:
    def __init__(self, number_of_connectors: int) -> None:
        self._keys = standard_keys(number_of_connectors)

    def get(self, keys: list[str] | None = None) -> tuple[dict[str, ConfigValue], list[str]]:
        """Return (known keys with their values, unknown keys).

        No list or an empty list means every key (OCPP 1.6 §5.8 GetConfiguration).
        """
        wanted = keys or list(self._keys)
        known = {key: self._keys[key] for key in wanted if key in self._keys}
        unknown = [key for key in wanted if key not in self._keys]
        return known, unknown

    def change(self, key: str, value: str) -> ConfigurationStatus:
        """Apply a ChangeConfiguration request and return the answer for the CSMS."""
        entry = self._keys.get(key)
        if entry is None:
            return ConfigurationStatus.not_supported
        if entry.readonly:
            return ConfigurationStatus.rejected
        normalized = _normalize(value, entry.kind)
        if normalized is None:
            # Badly formatted (e.g. HeartbeatInterval="abc"): refuse it rather than store a
            # value the charger itself could not use.
            return ConfigurationStatus.rejected
        entry.value = normalized
        return ConfigurationStatus.accepted

    @property
    def heartbeat_interval(self) -> int:
        """Seconds between heartbeats; 0 means no heartbeats."""
        return int(self._keys["HeartbeatInterval"].value)

    def set_heartbeat_interval(self, seconds: int) -> None:
        """Set by the charger itself, from BootNotification.conf (not a CSMS request)."""
        self._keys["HeartbeatInterval"].value = str(seconds)


def _normalize(value: str, kind: ValueKind) -> str | None:
    """The value as stored, or None if it's not valid for this kind of key."""
    if kind == "int":
        return value if value.isdigit() else None  # non-negative integers only
    if kind == "bool":
        lowered = value.lower()
        return lowered if lowered in ("true", "false") else None
    return value
