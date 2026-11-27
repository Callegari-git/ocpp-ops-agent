"""Build the virtual chargers of a fleet file and run them all in one asyncio process."""

import asyncio
import hashlib
import random
from dataclasses import dataclass

from ocpp_ops.charger.charger import VirtualCharger
from ocpp_ops.clock import Clock, Sleep
from ocpp_ops.fleet.config import Fleet
from ocpp_ops.message_log import MessageLog


@dataclass(frozen=True)
class FleetMember:
    charger: VirtualCharger
    # Seconds to wait before the first connection, so the fleet doesn't connect all at once.
    startup_delay: float


def charger_seed(fleet_seed: int, charger_id: str) -> int:
    """The seed of one charger's RNG, from the fleet seed and the charger's id.

    One RNG per charger, not one shared by the fleet: with a shared RNG, adding or removing
    a charger would shift the numbers every other charger draws. sha256 (not Python's
    hash(), which changes per process) makes it stable across runs and machines.
    """
    digest = hashlib.sha256(f"{fleet_seed}/{charger_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def build_fleet(
    fleet: Fleet,
    csms_url: str,
    *,
    now: Clock,
    sleep: Sleep,
    log: MessageLog | None = None,
) -> list[FleetMember]:
    members = []
    for spec in fleet.chargers():
        rng = random.Random(charger_seed(fleet.seed, spec.id))
        # The first draw of the charger's own RNG, so the delay depends only on the fleet
        # seed and the charger id.
        startup_delay = rng.uniform(0, fleet.startup_jitter_seconds)
        charger = VirtualCharger(
            spec.id,
            csms_url,
            spec.connectors,
            now=now,
            sleep=sleep,
            rng=rng,
            vendor=spec.vendor,
            model=spec.model,
            log=log,
        )
        members.append(FleetMember(charger, startup_delay))
    return members


async def run_fleet(members: list[FleetMember], sleep: Sleep) -> None:
    """Run every charger until cancelled.

    Network failures are handled inside each charger (it reconnects). Anything else that
    escapes a charger is a bug: the TaskGroup then stops the whole fleet, loudly.
    """
    async with asyncio.TaskGroup() as tasks:
        for member in members:
            tasks.create_task(_start(member, sleep), name=member.charger.id)


async def _start(member: FleetMember, sleep: Sleep) -> None:
    await sleep(member.startup_delay)
    await member.charger.run()
