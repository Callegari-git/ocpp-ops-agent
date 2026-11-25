from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from fakes import CsmsHarness, FakeClock, running_csms
from manual_time import ManualTime


@pytest.fixture
async def csms(tmp_path: Path) -> AsyncIterator[CsmsHarness]:
    """A real CSMS on a free port, with a fake clock and its log in tmp_path."""
    async with running_csms(FakeClock(), tmp_path / "messages.jsonl") as harness:
        yield harness


@pytest.fixture
def time() -> ManualTime:
    """Virtual time shared by the CSMS and the virtual chargers of a test."""
    return ManualTime()


@pytest.fixture
async def sim_csms(time: ManualTime, tmp_path: Path) -> AsyncIterator[CsmsHarness]:
    """A real CSMS whose clock is the test's virtual time."""
    async with running_csms(time, tmp_path / "csms.jsonl") as harness:
        yield harness
