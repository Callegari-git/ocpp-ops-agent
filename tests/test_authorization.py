from pathlib import Path

import pytest
from ocpp.v16.enums import AuthorizationStatus

from fakes import DEFAULT_ID_TAGS
from ocpp_ops.csms.authorization import (
    AuthorizationFileError,
    AuthorizationList,
    load_authorization_list,
)


def test_status_of_listed_and_unknown_tags() -> None:
    authorization = AuthorizationList({"TAG-A": "Accepted", "TAG-B": "Blocked"})

    assert authorization.status("TAG-A") == AuthorizationStatus.accepted
    assert authorization.status("TAG-B") == AuthorizationStatus.blocked
    # OCPP 1.6 §7.2: Invalid means "Identifier is unknown".
    assert authorization.status("TAG-Z") == AuthorizationStatus.invalid


def test_lookup_ignores_case() -> None:
    # §7.28: IdToken is a case-insensitive string.
    assert AuthorizationList({"Tag-a": "Accepted"}).status("TAG-A") == AuthorizationStatus.accepted


def test_default_file_loads_with_every_status() -> None:
    authorization = load_authorization_list(DEFAULT_ID_TAGS)

    assert authorization.status("TAG-0001") == AuthorizationStatus.accepted
    assert authorization.status("TAG-BLOCKED-01") == AuthorizationStatus.blocked
    assert authorization.status("TAG-EXPIRED-01") == AuthorizationStatus.expired


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("id_tags: {TAG-A: Valid}", "Input should be 'Accepted', 'Blocked' or 'Expired'"),
        ("id_tags: {TAG-A: Invalid}", "Input should be 'Accepted', 'Blocked' or 'Expired'"),
        ("id_tag: {TAG-A: Accepted}", "Extra inputs are not permitted"),
        ("id_tags: [TAG-A]", "Input should be a valid dictionary"),
        ("id_tags: {TAG-A: Accepted, tag-a: Blocked}", "tags differ only by case"),
        ("id_tags: {TAG-A: [", "while parsing"),
    ],
)
def test_bad_file_fails_loudly(tmp_path: Path, content: str, message: str) -> None:
    path = tmp_path / "id_tags.yaml"
    path.write_text(content)

    with pytest.raises(AuthorizationFileError, match=message):
        load_authorization_list(path)


def test_missing_file_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(AuthorizationFileError, match="No such file"):
        load_authorization_list(tmp_path / "nope.yaml")
