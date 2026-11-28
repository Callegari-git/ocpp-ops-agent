import asyncio
from datetime import UTC, datetime

from ocpp.v16 import call
from ocpp.v16.enums import Reason

from ocpp_ops.charger.transactions import (
    UNKNOWN_TRANSACTION_ID,
    ChargerTransaction,
    QueuedMessage,
    TransactionQueue,
)

START = datetime(2026, 11, 24, 12, 0, tzinfo=UTC)


def _transaction() -> ChargerTransaction:
    return ChargerTransaction(connector_id=1, id_tag="TAG", meter_start=10, started_at=START)


def _stop(transaction: ChargerTransaction) -> QueuedMessage:
    request = call.StopTransaction(
        meter_stop=20,
        timestamp=START.isoformat(),
        transaction_id=UNKNOWN_TRANSACTION_ID,
        reason=Reason.local,
    )
    return QueuedMessage(transaction, request)


def test_payload_takes_the_transaction_id_known_at_send_time() -> None:
    transaction = _transaction()
    message = _stop(transaction)  # queued before StartTransaction.conf

    transaction.transaction_id = 42  # StartTransaction.conf arrives

    payload = message.payload()
    assert isinstance(payload, call.StopTransaction)
    assert payload.transaction_id == 42
    assert payload.meter_stop == 20  # the rest is unchanged


def test_payload_without_a_transaction_id_uses_minus_one() -> None:
    # Errata 3.18: StartTransaction was given up on, no id will ever come.
    payload = _stop(_transaction()).payload()
    assert isinstance(payload, call.StopTransaction)
    assert payload.transaction_id == -1


def test_start_transaction_payload_is_unchanged() -> None:
    request = call.StartTransaction(
        connector_id=1, id_tag="TAG", meter_start=10, timestamp=START.isoformat()
    )
    assert QueuedMessage(_transaction(), request).payload() is request


async def test_queue_is_fifo_and_waits_when_empty() -> None:
    queue = TransactionQueue()
    first_task = asyncio.create_task(queue.first())
    await asyncio.sleep(0)
    assert not first_task.done()  # empty: waits

    transaction = _transaction()
    a, b = _stop(transaction), _stop(transaction)
    queue.put(a)
    queue.put(b)

    assert await first_task is a
    assert await queue.first() is a  # stays queued until dropped
    assert queue.drop_first() is a
    assert await queue.first() is b
    assert len(queue) == 1


def test_short_description_for_logs() -> None:
    message = _stop(_transaction())
    message.failures = 2
    assert str(message) == "StopTransaction (connector 1, 2 failures)"
