"""WebSocket server that accepts OCPP 1.6J charger connections."""

from http import HTTPStatus
from urllib.parse import unquote, urlsplit

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.http11 import Request, Response
from websockets.typing import Subprotocol

from ocpp_ops.csms.central_system import CentralSystem

# OCPP-J 1.6 §3.1.2: the charger must negotiate this WebSocket subprotocol.
OCPP_SUBPROTOCOL = Subprotocol("ocpp1.6")


def charger_id_from_path(path: str) -> str:
    """OCPP-J 1.6 §3.1.1: the charger's identity is the last segment of the URL path."""
    last_segment = urlsplit(path).path.rstrip("/").rsplit("/", 1)[-1]
    return unquote(last_segment)


async def start_server(central_system: CentralSystem, host: str, port: int) -> Server:
    """Start listening. Pass port 0 to let the OS pick a free port (read it from the server)."""

    async def handler(connection: ServerConnection) -> None:
        assert connection.request is not None  # set once the handshake has succeeded
        charger_id = charger_id_from_path(connection.request.path)
        await central_system.handle_connection(connection, charger_id)

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
