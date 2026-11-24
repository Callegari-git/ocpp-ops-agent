"""WebSocket server that accepts OCPP 1.6J charger connections."""

from functools import partial
from http import HTTPStatus
from urllib.parse import unquote, urlsplit

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response
from websockets.typing import Subprotocol

from ocpp_ops.clock import Clock
from ocpp_ops.csms.handlers import CsmsChargePoint
from ocpp_ops.csms.registry import Registry
from ocpp_ops.message_log import LoggingConnection, MessageLog

# OCPP-J 1.6 §3.1.2: the charger must negotiate this WebSocket subprotocol.
OCPP_SUBPROTOCOL = Subprotocol("ocpp1.6")


def charger_id_from_path(path: str) -> str:
    """OCPP-J 1.6 §3.1.1: the charger's identity is the last segment of the URL path."""
    last_segment = urlsplit(path).path.rstrip("/").rsplit("/", 1)[-1]
    return unquote(last_segment)


async def start_server(
    host: str,
    port: int,
    registry: Registry,
    log: MessageLog,
    heartbeat_interval: int,
    now: Clock,
) -> Server:
    """Start listening. Pass port 0 to let the OS pick a free port (read it from the server)."""
    handler = partial(
        _handle_connection,
        registry=registry,
        log=log,
        heartbeat_interval=heartbeat_interval,
        now=now,
    )
    # With `subprotocols` set, websockets itself rejects (HTTP 400) any client that offers
    # no subprotocol or not this one, before our handler ever runs.
    return await serve(
        handler,
        host,
        port,
        subprotocols=[OCPP_SUBPROTOCOL],
        process_request=_reject_missing_charger_id,
    )


def _reject_missing_charger_id(connection: ServerConnection, request: Request) -> Response | None:
    if not charger_id_from_path(request.path):
        return connection.respond(HTTPStatus.BAD_REQUEST, "Missing charger id in URL path\n")
    return None  # continue the handshake


async def _handle_connection(
    connection: ServerConnection,
    *,
    registry: Registry,
    log: MessageLog,
    heartbeat_interval: int,
    now: Clock,
) -> None:
    """Serve one charger from connect to disconnect."""
    assert connection.request is not None  # set once the handshake has succeeded
    charger_id = charger_id_from_path(connection.request.path)
    token = registry.connect(charger_id)
    logging_connection = LoggingConnection(connection, charger_id, log, now)
    charge_point = CsmsChargePoint(
        charger_id, logging_connection, registry, heartbeat_interval, now
    )
    try:
        await charge_point.start()
    except ConnectionClosed:
        # Clean close, network drop without a close frame, or server shutdown: all end the
        # session the same way. Any other exception propagates and websockets logs it.
        pass
    finally:
        # Runs on every exit path, so the registry never shows a dead charger as connected.
        registry.disconnect(charger_id, token)
