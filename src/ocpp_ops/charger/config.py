"""Configuration store of a virtual charger: standard OCPP 1.6 keys (spec §9.1, Core profile).

Pure, no I/O. It lives on the VirtualCharger, not on a connection, so it survives reconnects
and resets, like the non-volatile config of a real charger.
"""

from dataclasses import dataclass
from typing import Literal

from ocpp.v16.enums import ConfigurationStatus, Measurand

# "measurands": a comma-separated list of the measurands this charger can sample.
ValueKind = Literal["int", "bool", "text", "measurands"]

# What our meter can report (MeterValuesSampledData may only list these, §3.16.4).
SUPPORTED_MEASURANDS = (Measurand.energy_active_import_register, Measurand.power_active_import)


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
        # The spec's default is the energy register alone (§9.1); ours adds the power so
        # MeterValues show both how much and how fast.
        "MeterValuesSampledData": rw(
            "Energy.Active.Import.Register,Power.Active.Import", "measurands"
        ),
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

    @property
    def meter_value_sample_interval(self) -> int:
        """Seconds between MeterValues during a transaction; 0 means none (§9.1)."""
        return int(self._keys["MeterValueSampleInterval"].value)

    @property
    def sampled_measurands(self) -> list[Measurand]:
        """What each periodic MeterValues contains, in the order listed."""
        return [Measurand(name) for name in self._keys["MeterValuesSampledData"].value.split(",")]

    @property
    def transaction_message_attempts(self) -> int:
        """Transmissions of a transaction-related message before giving up (§3.7.1)."""
        return int(self._keys["TransactionMessageAttempts"].value)

    @property
    def transaction_message_retry_interval(self) -> int:
        """Seconds, multiplied by the transmissions so far, before a retry (§3.7.1)."""
        return int(self._keys["TransactionMessageRetryInterval"].value)

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
    if kind == "measurands":
        # §3.16.4: a measurand the charger doesn't support makes the whole change Rejected.
        names = [name.strip() for name in value.split(",")]
        supported = {str(measurand) for measurand in SUPPORTED_MEASURANDS}
        if not all(name in supported for name in names):
            return None  # also rejects "" : a MeterValues needs at least one sampled value
        return ",".join(names)
    return value
