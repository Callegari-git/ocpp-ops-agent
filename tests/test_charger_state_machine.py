import pytest
from ocpp.v16.enums import ChargePointErrorCode as E
from ocpp.v16.enums import ChargePointStatus as S

from ocpp_ops.charger.state_machine import (
    ALLOWED_TRANSITIONS,
    InvalidTransitionError,
    check_transition,
)


@pytest.mark.parametrize(
    ("current", "new"),
    [
        (S.available, S.preparing),
        (S.preparing, S.charging),
        (S.charging, S.suspended_ev),
        (S.suspended_ev, S.charging),
        (S.charging, S.suspended_evse),
        (S.charging, S.finishing),
        (S.finishing, S.available),
        (S.available, S.unavailable),
        (S.unavailable, S.available),
        (S.faulted, S.available),
    ],
)
def test_valid_transitions(current: S, new: S) -> None:
    check_transition(1, current, new, E.no_error)


@pytest.mark.parametrize(
    ("current", "new"),
    [
        (S.available, S.finishing),
        (S.charging, S.preparing),
        (S.finishing, S.charging),
        (S.preparing, S.unavailable),
        (S.unavailable, S.finishing),
        (S.available, S.reserved),  # reservations are out of scope
    ],
)
def test_invalid_transitions_raise(current: S, new: S) -> None:
    with pytest.raises(InvalidTransitionError, match="not allowed"):
        check_transition(1, current, new, E.no_error)


@pytest.mark.parametrize("current", [s for s in ALLOWED_TRANSITIONS if s != S.faulted])
def test_any_status_can_fault_with_an_error_code(current: S) -> None:
    check_transition(1, current, S.faulted, E.ground_failure)


def test_faulted_needs_an_error_code() -> None:
    with pytest.raises(InvalidTransitionError, match="error code"):
        check_transition(1, S.available, S.faulted, E.no_error)


def test_same_status_is_a_re_report() -> None:
    # e.g. a warning while staying Available
    check_transition(1, S.available, S.available, E.high_temperature)


@pytest.mark.parametrize(
    ("current", "new", "error_code"),
    [
        (S.available, S.unavailable, E.no_error),
        (S.unavailable, S.faulted, E.power_meter_failure),
        (S.faulted, S.available, E.no_error),
    ],
)
def test_connector_0_moves_between_its_three_statuses(current: S, new: S, error_code: E) -> None:
    check_transition(0, current, new, error_code)


@pytest.mark.parametrize("new", [S.preparing, S.charging, S.finishing, S.suspended_ev])
def test_connector_0_cannot_take_connector_statuses(new: S) -> None:
    with pytest.raises(InvalidTransitionError, match="Connector 0"):
        check_transition(0, S.available, new, E.no_error)


def test_table_covers_every_status_except_reserved() -> None:
    every_status_but_reserved = set(S) - {S.reserved}
    assert set(ALLOWED_TRANSITIONS) == every_status_but_reserved
    for current, targets in ALLOWED_TRANSITIONS.items():
        assert targets <= every_status_but_reserved
        assert current not in targets  # staying put is handled outside the table
