"""The fleet file: the default one is valid, and bad files give clear errors."""

from pathlib import Path

import pytest

from ocpp_ops.fleet.config import FleetFileError, load_fleet

DEFAULT_FLEET = Path(__file__).parent.parent / "fleet.yaml"

VALID = """
seed: 1
heartbeat_interval: 30
startup_jitter_seconds: 5
templates:
  - {id_prefix: A-, vendor: V, model: M, connectors: 2, count: 2}
  - {id_prefix: B-, vendor: W, model: N, connectors: 1, count: 1}
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "fleet.yaml"
    path.write_text(text)
    return path


def test_default_fleet_has_50_chargers_across_templates() -> None:
    fleet = load_fleet(DEFAULT_FLEET)
    chargers = fleet.chargers()
    assert len(chargers) == 50
    assert len(fleet.templates) == 3
    assert len({c.id for c in chargers}) == 50


def test_templates_expand_to_numbered_chargers(tmp_path: Path) -> None:
    fleet = load_fleet(_write(tmp_path, VALID))
    assert [(c.id, c.vendor, c.model, c.connectors) for c in fleet.chargers()] == [
        ("A-001", "V", "M", 2),
        ("A-002", "V", "M", 2),
        ("B-001", "W", "N", 1),
    ]
    assert (fleet.seed, fleet.heartbeat_interval, fleet.startup_jitter_seconds) == (1, 30, 5)


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        # A typo in a key is an error, not silently ignored.
        (("connectors: 2", "conectors: 2"), "templates.0.conectors: Extra inputs"),
        (("connectors: 2, ", ""), "templates.0.connectors: Field required"),
        (("count: 1}", "count: 0}"), "templates.1.count: Input should be greater than or equal"),
        (("heartbeat_interval: 30", "heartbeat_interval: 0"), "heartbeat_interval: Input"),
        (("startup_jitter_seconds: 5", "startup_jitter_seconds: -1"), "startup_jitter_seconds"),
        (("seed: 1", "seed: abc"), "seed: Input should be a valid integer"),
        # Two templates with the same prefix.
        (("id_prefix: B-", "id_prefix: A-"), "duplicate charger ids: A-001"),
    ],
)
def test_invalid_field_names_the_field(
    tmp_path: Path, change: tuple[str, str], expected: str
) -> None:
    path = _write(tmp_path, VALID.replace(*change))
    with pytest.raises(FleetFileError) as error:
        load_fleet(path)
    assert str(error.value).startswith(f"{path}: ")
    assert expected in str(error.value)


def test_different_prefixes_can_still_collide(tmp_path: Path) -> None:
    # "A-" + "1001" == "A-1" + "001"
    text = VALID.replace("count: 2}", "count: 1001}").replace("id_prefix: B-", "id_prefix: A-1")
    with pytest.raises(FleetFileError, match="duplicate charger ids: A-1001"):
        load_fleet(_write(tmp_path, text))


def test_no_templates(tmp_path: Path) -> None:
    text = VALID.split("templates:")[0] + "templates: []\n"
    with pytest.raises(FleetFileError, match="templates: List should have at least 1 item"):
        load_fleet(_write(tmp_path, text))


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FleetFileError, match="cannot read file"):
        load_fleet(tmp_path / "nope.yaml")


def test_invalid_yaml(tmp_path: Path) -> None:
    with pytest.raises(FleetFileError, match="invalid YAML"):
        load_fleet(_write(tmp_path, "seed: [1, 2\n"))


def test_top_level_not_a_mapping(tmp_path: Path) -> None:
    with pytest.raises(FleetFileError, match="expected a mapping at the top level"):
        load_fleet(_write(tmp_path, "- just\n- a list\n"))
