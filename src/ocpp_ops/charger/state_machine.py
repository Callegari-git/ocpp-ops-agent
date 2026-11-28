"""OCPP 1.6 connector statuses and the transitions allowed between them.

Pure rules, no I/O: VirtualCharger.set_status() checks every change here before applying it.
"""

from dataclasses import dataclass

from ocpp.v16.enums import ChargePointErrorCode, ChargePointStatus

S = ChargePointStatus

# OCPP 1.6 §4.9 transition table, as {from: allowed targets}. Staying in the same status is
# handled separately (see check_transition). Reserved is left out on purpose: reservations
# are out of scope for v1, so no connector can enter or leave it.
ALLOWED_TRANSITIONS: dict[ChargePointStatus, frozenset[ChargePointStatus]] = {
    S.available: frozenset(
        {S.preparing, S.charging, S.suspended_ev, S.suspended_evse, S.unavailable, S.faulted}
    ),
    S.preparing: frozenset(
        {S.available, S.charging, S.suspended_ev, S.suspended_evse, S.finishing, S.faulted}
    ),
    S.charging: frozenset(
        {S.available, S.suspended_ev, S.suspended_evse, S.finishing, S.unavailable, S.faulted}
    ),
    S.suspended_ev: frozenset(
        {S.available, S.charging, S.suspended_evse, S.finishing, S.unavailable, S.faulted}
    ),
    S.suspended_evse: frozenset(
        {S.available, S.charging, S.suspended_ev, S.finishing, S.unavailable, S.faulted}
    ),
    S.finishing: frozenset({S.available, S.preparing, S.unavailable, S.faulted}),
    S.unavailable: frozenset(
        {S.available, S.preparing, S.charging, S.suspended_ev, S.suspended_evse, S.faulted}
    ),
    S.faulted: frozenset(
        {
            S.available,
            S.preparing,
            S.charging,
            S.suspended_ev,
            S.suspended_evse,
            S.finishing,
            S.unavailable,
        }
    ),
}

# A connector in use: an EV or a driver is there, from the first interaction until the cable
# is removed (§7.7: Preparing ... Finishing, all "Operative" but not available to a new user).
IN_USE = frozenset({S.preparing, S.charging, S.suspended_ev, S.suspended_evse, S.finishing})

# Connector 0 is the charge point as a whole: OCPP 1.6 only allows these three statuses for
# it, and any change between them.
CHARGE_POINT_STATUSES = frozenset({S.available, S.unavailable, S.faulted})


@dataclass(frozen=True)
class ConnectorState:
    """What a connector reports in its StatusNotification. Frozen: replaced, never edited."""

    status: ChargePointStatus = S.available
    error_code: ChargePointErrorCode = ChargePointErrorCode.no_error
    info: str | None = None


class InvalidTransitionError(ValueError):
    """A status change the OCPP 1.6 rules don't allow."""


def check_transition(
    connector_id: int,
    current: ChargePointStatus,
    new: ChargePointStatus,
    error_code: ChargePointErrorCode,
) -> None:
    """Raise InvalidTransitionError unless `current -> new` is allowed for this connector."""
    if new == S.faulted and error_code == ChargePointErrorCode.no_error:
        raise InvalidTransitionError("Faulted needs an error code other than NoError")
    if connector_id == 0:
        if new not in CHARGE_POINT_STATUSES:
            raise InvalidTransitionError(f"Connector 0 (charge point) cannot be {new.value}")
        return
    if new == current:
        # Same status again: a re-report, e.g. a new error code or info as a warning.
        return
    if new not in ALLOWED_TRANSITIONS.get(current, frozenset()):
        raise InvalidTransitionError(
            f"Connector {connector_id}: {current.value} -> {new.value} is not allowed"
        )
