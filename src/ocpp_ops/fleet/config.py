"""The fleet file (fleet.yaml): which chargers to simulate, validated with Pydantic."""

from pathlib import Path
from typing import Literal, Self

import yaml
from ocpp.v16.enums import ChargePointStatus
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

CurrentType = Literal["AC", "DC"]
# Per connector, when the template gives no max_power_kw. AC: 3-phase 16 A (3 x 230 V x 16 A
# ~ 11 kW), the most common AC point. DC: 50 kW, the classic first-generation fast charger.
DEFAULT_MAX_POWER_KW: dict[CurrentType, float] = {"AC": 11.0, "DC": 50.0}
# Upper bounds, to catch typos (e.g. watts written as kW). AC: 3-phase 63 A, the most an AC
# connector can deliver (3 x 230 V x 63 A ~ 43.5 kW). DC: 400 kW, above today's fastest.
MAX_POWER_LIMIT_KW: dict[CurrentType, float] = {"AC": 43.5, "DC": 400.0}


class FleetFileError(Exception):
    """The fleet file is missing, is not valid YAML, or does not match the expected format."""


class _Strict(BaseModel):
    # A typo in a key ("conectors") must be an error, not silently ignored.
    model_config = ConfigDict(extra="forbid", frozen=True)


class ChargerTemplate(_Strict):
    """`count` identical chargers, with ids `{id_prefix}001`, `{id_prefix}002`, ..."""

    id_prefix: str = Field(min_length=1)
    vendor: str = Field(min_length=1)
    model: str = Field(min_length=1)
    connectors: int = Field(ge=1)
    count: int = Field(ge=1)
    current: CurrentType
    # Per connector: every charging connector draws this much at once. No load sharing (smart
    # charging is out of scope), although real DC chargers usually share their power across
    # connectors. Default: DEFAULT_MAX_POWER_KW.
    max_power_kw: float | None = Field(default=None, gt=0)
    # What a connector with a cable still plugged in reports after a reboot: vendors differ.
    plugged_status_after_reboot: Literal["Finishing", "Preparing", "Available"] = "Finishing"

    @model_validator(mode="after")
    def _power_is_plausible(self) -> Self:
        limit = MAX_POWER_LIMIT_KW[self.current]
        if self.max_power_kw is not None and self.max_power_kw > limit:
            raise ValueError(f"max_power_kw: {self.current} connectors deliver at most {limit} kW")
        return self

    @property
    def max_power_w(self) -> int:
        kw = (
            self.max_power_kw
            if self.max_power_kw is not None
            else DEFAULT_MAX_POWER_KW[self.current]
        )
        return round(kw * 1000)

    def charger_ids(self) -> list[str]:
        return [f"{self.id_prefix}{n:03d}" for n in range(1, self.count + 1)]


class ChargerSpec(_Strict):
    """One charger of the fleet, after expanding its template."""

    id: str
    vendor: str
    model: str
    connectors: int
    max_power_w: int  # per connector
    plugged_status_after_reboot: ChargePointStatus = ChargePointStatus.finishing


class MinMax(_Strict):
    """A range to draw from uniformly."""

    min: float = Field(ge=0)
    max: float = Field(ge=0)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.min > self.max:
            raise ValueError("min must not be greater than max")
        return self


class Usage(_Strict):
    """Simulated drivers (see fleet/usage.py). Each connector gets its own seeded stream of
    visits. Durations in minutes."""

    # Mean time between a driver leaving and the next one arriving (exponential).
    mean_idle_minutes: float = Field(gt=0)
    charge_minutes: MinMax
    # Drivers who stop the session themselves (Local), then unplug after
    # stay_after_stop_minutes. The others just unplug: EVDisconnected.
    driver_stop_share: float = Field(ge=0, le=1)
    stay_after_stop_minutes: MinMax
    # Arrivals presenting a Blocked or Expired tag (from the authorization list).
    refused_tag_share: float = Field(ge=0, le=1)


class Fleet(_Strict):
    seed: int
    # Sent by the embedded CSMS in BootNotification.conf (OCPP 1.6 §4.2): the CSMS, not the
    # charger template, decides the heartbeat interval.
    heartbeat_interval: int = Field(gt=0)
    # Each charger waits uniform(0, window) seconds before its first connection.
    startup_jitter_seconds: float = Field(ge=0)
    templates: list[ChargerTemplate] = Field(min_length=1)
    # None: nobody uses the chargers (tests drive sessions themselves).
    usage: Usage | None = None

    @model_validator(mode="after")
    def _ids_are_unique(self) -> Self:
        # Checked on the generated ids, not the prefixes: different prefixes can still
        # collide, e.g. "CP-" with count >= 1001 gives "CP-1001", which is also "CP-1" + "001".
        seen: set[str] = set()
        duplicates: list[str] = []
        for template in self.templates:
            for charger_id in template.charger_ids():
                if charger_id in seen:
                    duplicates.append(charger_id)
                seen.add(charger_id)
        if duplicates:
            raise ValueError(f"duplicate charger ids: {', '.join(duplicates[:5])}")
        return self

    def chargers(self) -> list[ChargerSpec]:
        return [
            ChargerSpec(
                id=charger_id,
                vendor=t.vendor,
                model=t.model,
                connectors=t.connectors,
                max_power_w=t.max_power_w,
                plugged_status_after_reboot=ChargePointStatus(t.plugged_status_after_reboot),
            )
            for t in self.templates
            for charger_id in t.charger_ids()
        ]


def load_fleet(path: Path) -> Fleet:
    """Read and validate a fleet file. Every problem is raised as a FleetFileError."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise FleetFileError(f"{path}: cannot read file: {error.strerror}") from error
    except yaml.YAMLError as error:
        raise FleetFileError(f"{path}: invalid YAML: {error}") from error
    if not isinstance(data, dict):
        raise FleetFileError(f"{path}: expected a mapping at the top level")
    try:
        return Fleet.model_validate(data)
    except ValidationError as error:
        raise FleetFileError(_describe(path, error)) from error


def _describe(path: Path, error: ValidationError) -> str:
    """One line per problem, e.g. `fleet.yaml: templates.1.connectors: Input should be ...`."""
    lines = []
    for problem in error.errors():
        location = ".".join(str(part) for part in problem["loc"]) or "(top level)"
        lines.append(f"{path}: {location}: {problem['msg']}")
    return "\n".join(lines)
