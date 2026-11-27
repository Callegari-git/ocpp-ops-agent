"""Cancellation checks, for a cancellation lost inside the ocpp library on Python 3.11."""

import asyncio


def is_cancelling() -> bool:
    """True if the current task has been asked to cancel (Task.cancelling(), Python 3.11+)."""
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


# Workaround for CPython gh-86296: on Python 3.11, asyncio.wait_for() returns the result
# instead of raising CancelledError when the result and the cancellation arrive in the same
# loop iteration (fixed in 3.12). ocpp's ChargePoint.call() waits for every answer with
# wait_for (ocpp 2.1.0, ocpp/charge_point.py:455, in _get_specific_response). Remove once
# Python 3.11 is dropped or ocpp stops using wait_for.
def raise_if_cancelling() -> None:
    """Re-raise a cancellation that was swallowed. Call right after awaiting ocpp's call().

    3.11's wait_for drops the CancelledError but never resets the task's cancellation
    count, so the request is still visible in Task.cancelling(). On 3.12+ the CancelledError
    is raised by call() itself and this never triggers.
    """
    if is_cancelling():
        raise asyncio.CancelledError
