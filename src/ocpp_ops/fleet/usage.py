"""Simulated drivers: seeded arrivals and charging sessions at each connector.

One Driver per connector, with its own RNG. Each visit: arrive after an idle time, plug in,
present a tag, charge, then either stop and unplug a bit later, or just unplug. Everything
goes through the charger's session API, like a real driver at a real charger.
"""

import hashlib
import random
from dataclasses import dataclass

from ocpp.exceptions import OCPPError
from ocpp.v16.enums import AuthorizationStatus
from ocpp.v16.enums import ChargePointStatus as S
from websockets.exceptions import ConnectionClosed

from ocpp_ops.charger.charger import SessionError, VirtualCharger
from ocpp_ops.charger.state_machine import IN_USE
from ocpp_ops.clock import Sleep
from ocpp_ops.csms.authorization import AuthorizationList
from ocpp_ops.fleet.config import MinMax, Usage

# Seconds between plugging in and presenting the tag.
AUTHORIZE_DELAY_SECONDS = MinMax(min=5, max=30)
# What can interrupt a visit: the connector changed under the driver (a reset, an
# availability change), or the connection failed during a call. The driver then leaves.
_VISIT_ERRORS = (SessionError, ConnectionClosed, TimeoutError, OCPPError)


@dataclass(frozen=True)
class TagPool:
    """The idTags drivers present: the authorization list's, by status."""

    accepted: tuple[str, ...]
    refused: tuple[str, ...]  # Blocked or Expired

    @classmethod
    def from_authorization(cls, authorization: AuthorizationList) -> "TagPool":
        accepted = authorization.tags_with({AuthorizationStatus.accepted})
        if not accepted:
            raise ValueError("the authorization list has no Accepted tag for drivers to use")
        refused = authorization.tags_with(
            {AuthorizationStatus.blocked, AuthorizationStatus.expired}
        )
        return cls(tuple(accepted), tuple(refused))


@dataclass(frozen=True)
class Visit:
    """One driver's visit, drawn in full before it starts (durations in seconds)."""

    idle_s: float  # before arriving
    id_tag: str
    authorize_delay_s: float
    charge_s: float
    driver_stops: bool  # stop (Local), then unplug; otherwise just unplug (EVDisconnected)
    stay_after_stop_s: float


def usage_seed(fleet_seed: int, charger_id: str, connector_id: int) -> int:
    """The seed of one connector's driver RNG, like charger_seed() (fleet/launcher.py).

    Its own stream, not the charger's RNG: that one also draws message ids and reconnect
    delays, whose number depends on network timing, so sharing it would make the sessions
    depend on the network. Per connector, so connectors don't shift each other either.
    """
    digest = hashlib.sha256(f"{fleet_seed}/{charger_id}/usage/{connector_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big")


def plan_visit(usage: Usage, tags: TagPool, rng: random.Random) -> Visit:
    """Draw the next visit. Always the same draws in the same order, whatever happened to
    the previous visits (refused, interrupted, skipped): one visit's outcome never shifts
    the numbers of the next ones."""
    idle_s = rng.expovariate(1 / (usage.mean_idle_minutes * 60))
    refused = rng.random() < usage.refused_tag_share and bool(tags.refused)
    pool = tags.refused if refused else tags.accepted
    id_tag = pool[rng.randrange(len(pool))]
    return Visit(
        idle_s=idle_s,
        id_tag=id_tag,
        authorize_delay_s=_uniform(rng, AUTHORIZE_DELAY_SECONDS),
        charge_s=_uniform(rng, usage.charge_minutes) * 60,
        driver_stops=rng.random() < usage.driver_stop_share,
        stay_after_stop_s=_uniform(rng, usage.stay_after_stop_minutes) * 60,
    )


def _uniform(rng: random.Random, bounds: MinMax) -> float:
    return rng.uniform(bounds.min, bounds.max)


@dataclass
class Driver:
    """The drivers of one connector, one visit after another."""

    charger: VirtualCharger
    connector_id: int
    usage: Usage
    tags: TagPool
    rng: random.Random

    async def run(self, sleep: Sleep) -> None:
        """Visit after visit, until cancelled."""
        while True:
            visit = plan_visit(self.usage, self.tags, self.rng)
            await sleep(visit.idle_s)
            if not self.charger.booted or self._status() != S.available:
                continue  # offline, in use or out of order: this driver goes elsewhere
            await self._visit(visit, sleep)

    async def _visit(self, visit: Visit, sleep: Sleep) -> None:
        charger, connector_id = self.charger, self.connector_id
        try:
            await charger.plug_in(connector_id)
            await sleep(visit.authorize_delay_s)
            status = await charger.authorize(connector_id, visit.id_tag)
            if status != AuthorizationStatus.accepted:
                return  # refused: the charger has already ended the use
            await charger.start(connector_id)
            await sleep(visit.charge_s)
            if visit.driver_stops:
                await charger.stop(connector_id)
                await sleep(visit.stay_after_stop_s)
            await charger.unplug(connector_id)
        except _VISIT_ERRORS:
            await self._leave()

    async def _leave(self) -> None:
        """The visit was interrupted: the driver takes the cable and goes, if it's in."""
        if self._status() not in IN_USE:
            return  # e.g. a firmware that reports Available after a reboot, cable or not
        try:
            await self.charger.unplug(self.connector_id)
        except _VISIT_ERRORS:
            pass  # the charger's state is still consistent; the next visit starts afresh

    def _status(self) -> S:
        return self.charger.connector(self.connector_id).status
