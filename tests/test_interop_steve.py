"""Interoperability with SteVe, a third-party CSMS. Optional: skipped unless STEVE_URL is set.

    STEVE_URL=ws://localhost:8180/steve/websocket/CentralSystemService pytest -m interop -s

Prerequisite: every charger id of fleet.yaml registered in SteVe, registration status
Accepted. See docs/interop/steve.md. CI never sets STEVE_URL, so it never depends on SteVe.
"""

import asyncio
import os
import time
from collections import defaultdict
from pathlib import Path

import pytest

from ocpp_ops.clock import utc_now
from ocpp_ops.fleet.config import ChargerSpec, Fleet, load_fleet
from ocpp_ops.fleet.launcher import FleetMember, build_fleet, run_fleet
from ocpp_ops.message_log import LogRecord, MessageLog

STEVE_URL = os.environ.get("STEVE_URL", "")
DEFAULT_FLEET = Path(__file__).parent.parent / "fleet.yaml"
BOOT_BOUND_SECONDS = 60.0

pytestmark = [
    pytest.mark.interop,
    pytest.mark.skipif(not STEVE_URL, reason="set STEVE_URL to SteVe's OCPP-J base URL"),
]


def _read_log(path: Path) -> list[LogRecord]:
    if not path.exists():
        return []
    return [LogRecord.model_validate_json(line) for line in path.read_text().splitlines()]


def _boot_progress(records: list[LogRecord]) -> tuple[dict[str, int], dict[str, set[int]]]:
    """From a charger-side log: the accepted boot interval per charger, and the connectors
    whose StatusNotification was answered. A CALLRESULT only carries the id of its CALL, so
    the connector id is taken from the CALL with the same message id."""
    calls = {r.message_id: r for r in records if r.message_type == "CALL" and r.direction == "out"}
    intervals: dict[str, int] = {}
    answered: dict[str, set[int]] = defaultdict(set)
    for r in records:
        if r.message_type != "CALLRESULT" or r.direction != "in":
            continue
        if r.action == "BootNotification" and r.payload["status"] == "Accepted":
            intervals[r.charger_id] = r.payload["interval"]
        elif r.action == "StatusNotification" and r.message_id in calls:
            answered[r.charger_id].add(calls[r.message_id].payload["connectorId"])
    return intervals, answered


def _not_booted(records: list[LogRecord], specs: list[ChargerSpec]) -> list[str]:
    """Chargers without an accepted boot and an answer for every connector, 0 included."""
    intervals, answered = _boot_progress(records)
    return [
        spec.id
        for spec in specs
        if spec.id not in intervals or answered[spec.id] != set(range(spec.connectors + 1))
    ]


async def _boot_against_steve(fleet: Fleet, log_path: Path) -> tuple[float, list[FleetMember]]:
    """Run the fleet against SteVe until every charger has booted. Returns the time taken."""
    specs = fleet.chargers()
    with MessageLog(log_path) as log:
        members = build_fleet(fleet, STEVE_URL, now=utc_now, sleep=asyncio.sleep, log=log)
        started = time.perf_counter()
        task = asyncio.create_task(run_fleet(members, asyncio.sleep))
        try:
            async with asyncio.timeout(BOOT_BOUND_SECONDS):
                while _not_booted(_read_log(log_path), specs):  # noqa: ASYNC110
                    await asyncio.sleep(0.02)
        except TimeoutError:
            missing = _not_booted(_read_log(log_path), specs)
            pytest.fail(
                f"{len(missing)} chargers not booted after {BOOT_BOUND_SECONDS} s "
                f"(registered in SteVe as Accepted?): {missing[:10]}"
            )
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        return time.perf_counter() - started, members


def _check_log(records: list[LogRecord], members: list[FleetMember]) -> None:
    errors = [r for r in records if r.message_type in ("CALLERROR", "INVALID")]
    assert errors == []
    intervals, _ = _boot_progress(records)
    for member in members:
        # The interval SteVe sent at boot became the charger's HeartbeatInterval.
        assert intervals[member.charger.id] > 0
        assert member.charger.config.heartbeat_interval == intervals[member.charger.id]


async def test_one_charger_per_template_boots(tmp_path: Path) -> None:
    fleet = load_fleet(DEFAULT_FLEET)
    templates = [template.model_copy(update={"count": 1}) for template in fleet.templates]
    fleet = fleet.model_copy(update={"templates": templates, "startup_jitter_seconds": 0})

    _, members = await _boot_against_steve(fleet, tmp_path / "fleet.jsonl")

    assert len(members) == len(templates)
    _check_log(_read_log(tmp_path / "fleet.jsonl"), members)


async def test_fifty_chargers_boot(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Jitter 0: all 50 at once, the same worst case as the embedded scale test.
    fleet = load_fleet(DEFAULT_FLEET).model_copy(update={"startup_jitter_seconds": 0})

    elapsed, members = await _boot_against_steve(fleet, tmp_path / "fleet.jsonl")

    assert len(members) == 50
    _check_log(_read_log(tmp_path / "fleet.jsonl"), members)
    with capsys.disabled():
        print(f"\n[interop] 50 chargers booted against SteVe in {elapsed:.3f} s")
