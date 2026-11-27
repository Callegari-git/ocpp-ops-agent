"""The fleet file (fleet.yaml): which chargers to simulate, validated with Pydantic."""

from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator


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

    def charger_ids(self) -> list[str]:
        return [f"{self.id_prefix}{n:03d}" for n in range(1, self.count + 1)]


class ChargerSpec(_Strict):
    """One charger of the fleet, after expanding its template."""

    id: str
    vendor: str
    model: str
    connectors: int


class Fleet(_Strict):
    seed: int
    # Sent by the embedded CSMS in BootNotification.conf (OCPP 1.6 §4.2): the CSMS, not the
    # charger template, decides the heartbeat interval.
    heartbeat_interval: int = Field(gt=0)
    # Each charger waits uniform(0, window) seconds before its first connection.
    startup_jitter_seconds: float = Field(ge=0)
    templates: list[ChargerTemplate] = Field(min_length=1)

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
            ChargerSpec(id=charger_id, vendor=t.vendor, model=t.model, connectors=t.connectors)
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
