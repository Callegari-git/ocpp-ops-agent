"""Run the CSMS: python -m ocpp_ops.csms --host 127.0.0.1 --port 9000"""

import argparse
import asyncio
import logging
from pathlib import Path

from ocpp_ops.clock import utc_now
from ocpp_ops.csms.central_system import CentralSystem
from ocpp_ops.csms.registry import Registry
from ocpp_ops.csms.server import start_server
from ocpp_ops.message_log import MessageLog

logger = logging.getLogger("ocpp_ops.csms")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m ocpp_ops.csms", description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9000, help="0 picks a free port")
    parser.add_argument(
        "--heartbeat-interval",
        type=int,
        default=60,
        help="seconds, sent to chargers in BootNotification.conf",
    )
    parser.add_argument(
        "--call-timeout",
        type=float,
        default=30.0,
        help="seconds to wait for a charger's answer to an outbound command",
    )
    parser.add_argument(
        "--log-file",
        type=Path,
        default=None,
        help="JSONL message log (default: logs/csms-<UTC start time>.jsonl)",
    )
    return parser.parse_args(argv)


def default_log_file() -> Path:
    return Path("logs") / f"csms-{utc_now():%Y%m%dT%H%M%SZ}.jsonl"


async def run(args: argparse.Namespace, log_file: Path) -> None:
    """Serve until cancelled.

    On Ctrl+C, asyncio.run() cancels this coroutine. Leaving `async with server` closes
    every connection and waits for their handlers; leaving `with MessageLog` then closes
    the log file, after the last frame has been written.
    """
    with MessageLog(log_file) as log:
        central_system = CentralSystem(
            Registry(now=utc_now),
            log,
            heartbeat_interval=args.heartbeat_interval,
            now=utc_now,
            call_timeout=args.call_timeout,
        )
        server = await start_server(central_system, args.host, args.port)
        async with server:
            port = server.sockets[0].getsockname()[1]
            logger.info("CSMS listening on ws://%s:%d (log: %s)", args.host, port, log.path)
            await server.serve_forever()


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # The ocpp library logs every frame at INFO; the JSONL log already records them.
    logging.getLogger("ocpp").setLevel(logging.WARNING)
    log_file = args.log_file or default_log_file()
    try:
        asyncio.run(run(args, log_file))
    except KeyboardInterrupt:
        # Cleanup already happened inside run(); see its docstring.
        logger.info("CSMS stopped")


if __name__ == "__main__":
    main()
