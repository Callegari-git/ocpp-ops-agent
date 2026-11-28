"""The CSMS's authorization list: which idTags may charge.

Loaded from a small YAML file of fictional tags. A tag that is not in the list is Invalid
("Identifier is unknown", OCPP 1.6 §7.2).
"""

from pathlib import Path
from typing import Literal

import yaml
from ocpp.v16.enums import AuthorizationStatus
from pydantic import BaseModel, ConfigDict, ValidationError

# The statuses a tag can have in the file. Invalid is implied by absence; ConcurrentTx is
# decided per StartTransaction, not stored (§7.2).
ListedStatus = Literal["Accepted", "Blocked", "Expired"]


class AuthorizationFileError(ValueError):
    """The authorization file is missing, not YAML, or not in the expected shape."""


class _AuthorizationFile(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a typo in a field name must fail loudly

    id_tags: dict[str, ListedStatus]


class AuthorizationList:
    """idTag -> status. Lookups ignore case: IdToken is a case-insensitive string (§7.28)."""

    def __init__(self, id_tags: dict[str, ListedStatus] | None = None) -> None:
        self._statuses = {
            tag.casefold(): AuthorizationStatus(status) for tag, status in (id_tags or {}).items()
        }

    def status(self, id_tag: str) -> AuthorizationStatus:
        return self._statuses.get(id_tag.casefold(), AuthorizationStatus.invalid)


def load_authorization_list(path: Path) -> AuthorizationList:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        parsed = _AuthorizationFile.model_validate(data)
    except (OSError, yaml.YAMLError, ValidationError) as error:
        raise AuthorizationFileError(f"{path}: {error}") from error
    folded = [tag.casefold() for tag in parsed.id_tags]
    duplicates = sorted({tag for tag in folded if folded.count(tag) > 1})
    if duplicates:
        raise AuthorizationFileError(f"{path}: tags differ only by case: {duplicates}")
    return AuthorizationList(parsed.id_tags)
