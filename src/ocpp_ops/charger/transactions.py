"""Transactions on the charger side, and the queue of transaction-related messages.

OCPP 1.6 §3.7: StartTransaction, StopTransaction and periodic MeterValues are
"transaction-related". The charger must deliver them in chronological order, queue them while
offline, and send new ones only once the queue is empty. This module holds that queue; the
VirtualCharger's sender task drains it while a session is up.
"""

import asyncio
import dataclasses
from collections import deque
from dataclasses import dataclass
from datetime import datetime

from ocpp.v16 import call

# OCPP 1.6 errata 3.18: when a charger never gets a transactionId (its StartTransaction was
# discarded), it sends the later messages of that transaction with transactionId -1.
UNKNOWN_TRANSACTION_ID = -1

TransactionRequest = call.StartTransaction | call.MeterValues | call.StopTransaction


@dataclass
class ChargerTransaction:
    """A transaction as the charger knows it."""

    connector_id: int
    id_tag: str
    meter_start: int  # Wh
    started_at: datetime
    # Given by the CSMS in StartTransaction.conf. None until that answer arrives, which can
    # be long after the start if the charger was offline.
    transaction_id: int | None = None


@dataclass
class QueuedMessage:
    """One transaction-related message waiting to be delivered."""

    transaction: ChargerTransaction
    # For MeterValues and StopTransaction, the transaction_id in here is only a placeholder.
    request: TransactionRequest
    # Times the CSMS failed to process it (CALLERROR or no answer), for the retry rules.
    failures: int = 0

    def __str__(self) -> str:
        """Short form for log lines, e.g. "StartTransaction (connector 1, 2 failures)"."""
        action = type(self.request).__name__
        return f"{action} (connector {self.transaction.connector_id}, {self.failures} failures)"

    def payload(self) -> TransactionRequest:
        """The request to send now, with the transaction id known at this point.

        Built at send time, not when queued: a MeterValues queued while offline, before the
        StartTransaction was answered, still goes out with the right id. FIFO order
        guarantees that StartTransaction has been answered (or discarded) by then.
        """
        if isinstance(self.request, call.StartTransaction):
            return self.request
        transaction_id = self.transaction.transaction_id
        if transaction_id is None:
            transaction_id = UNKNOWN_TRANSACTION_ID
        return dataclasses.replace(self.request, transaction_id=transaction_id)


class TransactionQueue:
    """FIFO of transaction-related messages. Lives on the charger: survives reconnects."""

    def __init__(self) -> None:
        self._messages: deque[QueuedMessage] = deque()
        self._not_empty = asyncio.Event()

    def __len__(self) -> int:
        return len(self._messages)

    def put(self, message: QueuedMessage) -> None:
        self._messages.append(message)
        self._not_empty.set()

    async def first(self) -> QueuedMessage:
        """The oldest message, waiting for one if the queue is empty. It stays queued until
        drop_first(): a message is removed only once delivered (or given up on)."""
        while not self._messages:
            self._not_empty.clear()
            await self._not_empty.wait()
        return self._messages[0]

    def drop_first(self) -> QueuedMessage:
        return self._messages.popleft()
