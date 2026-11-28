"""Run a fleet of virtual chargers: python -m ocpp_ops.fleet --fleet fleet.yaml

By default an embedded CSMS is started in the same process. With --csms-url the fleet
connects to an external CSMS instead.
"""

import argparse
import asyncio
import logging
import sys
from collections.abc import Callable
from pathlib import Path

from ocpp_ops.clock import utc_now
from ocpp_ops.csms.authorization import (
    AuthorizationFileError,
    AuthorizationList,
    load_authorization_list,
)
from ocpp_ops.csms.central_system import CentralSystem
from ocpp_ops.csms.registry import Registry
from ocpp_ops.csms.server import start_server
from ocpp_ops.fleet.config import Fleet, FleetFileError, load_fleet
from ocpp_ops.fleet.launcher import FleetMember, build_fleet, run_fleet
from ocpp_ops.fleet.status_grid import show_status
from ocpp_ops.fleet.status_rows import ChargerRow, rows_from_chargers, rows_from_registry
from ocpp_ops.message_log import MessageLog

logger = logging.getLogger("ocpp_ops.fleet")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m ocpp_ops.fleet", description=__doc__)
    parser.add_argument("--fleet", type=Path, default=Path("fleet.yaml"), help="fleet file")
    parser.add_argument(
        "--csms-url",
        default=None,
        help="connect to this external CSMS instead of starting the embedded one",
    )
    parser.add_argument(
        "--port", type=int, default=9000, help="embedded CSMS port (0 picks a free port)"
    )
    parser.add_argument(
        "--id-tags",
        type=Path,
        default=Path("id_tags.yaml"),
        help="authorization list of the embedded CSMS (not used with --csms-url)",
    )
    parser.add_argument("--log-dir", type=Path, default=Path("logs"), help="JSONL log directory")
    return parser.parse_args(argv)


def _log_file(log_dir: Path, name: str) -> Path:
    return log_dir / f"{name}-{utc_now():%Y%m%dT%H%M%SZ}.jsonl"


async def run_embedded(
    fleet: Fleet, port: int, log_dir: Path, authorization: AuthorizationList
) -> None:
    """Embedded CSMS + fleet. The CSMS-side log records every message.

    On Ctrl+C the fleet stops first (each charger closes with 1001 while the server is still
    up), then the server closes, then the log file.
    """
    with MessageLog(_log_file(log_dir, "csms")) as log:
        central_system = CentralSystem(
            Registry(now=utc_now),
            log,
            heartbeat_interval=fleet.heartbeat_interval,
            now=utc_now,
            authorization=authorization,
        )
        server = await start_server(central_system, "127.0.0.1", port)
        async with server:
            url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
            logger.info("Embedded CSMS listening on %s (log: %s)", url, log.path)
            members = build_fleet(fleet, url, now=utc_now, sleep=asyncio.sleep)
            specs = fleet.chargers()
            await _run_with_status(
                members,
                lambda: rows_from_registry(
                    specs, central_system.registry, fleet.heartbeat_interval
                ),
            )


async def run_external(fleet: Fleet, csms_url: str, log_dir: Path) -> None:
    """Fleet only, against an external CSMS. The charger-side log records every message."""
    with MessageLog(_log_file(log_dir, "fleet")) as log:
        logger.info("Connecting the fleet to %s (log: %s)", csms_url, log.path)
        # OCPP 1.6 §4.2: the CSMS sets the heartbeat interval in BootNotification.conf.
        logger.info("heartbeat_interval from the fleet file is not used: the CSMS sets it")
        members = build_fleet(fleet, csms_url, now=utc_now, sleep=asyncio.sleep, log=log)
        chargers = [member.charger for member in members]
        await _run_with_status(members, lambda: rows_from_chargers(chargers))


async def _run_with_status(
    members: list[FleetMember], rows: Callable[[], list[ChargerRow]]
) -> None:
    async with asyncio.TaskGroup() as tasks:
        tasks.create_task(run_fleet(members, asyncio.sleep))
        tasks.create_task(show_status(rows, utc_now, asyncio.sleep, sys.stdout))


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    # A redrawn grid and log lines on the same terminal would garble each other: with the
    # grid on screen, only warnings are logged.
    level = logging.WARNING if sys.stdout.isatty() else logging.INFO
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # The ocpp library logs every frame at INFO; the JSONL log already records them.
    logging.getLogger("ocpp").setLevel(logging.WARNING)
    # websockets logs every connection open/close at INFO: 50 lines per fleet start.
    logging.getLogger("websockets").setLevel(logging.WARNING)
    try:
        fleet = load_fleet(args.fleet)
    except FleetFileError as error:
        sys.exit(f"Invalid fleet file:\n{error}")
    logger.info("Fleet: %d chargers, seed %d", len(fleet.chargers()), fleet.seed)
    try:
        if args.csms_url:
            asyncio.run(run_external(fleet, args.csms_url, args.log_dir))
        else:
            try:
                authorization = load_authorization_list(args.id_tags)
            except AuthorizationFileError as error:
                sys.exit(f"Invalid authorization file:\n{error}")
            asyncio.run(run_embedded(fleet, args.port, args.log_dir, authorization))
    except KeyboardInterrupt:
        # Cleanup already happened inside run_*(); see run_embedded's docstring. Printed,
        # not logged: with the grid on, INFO logging is off.
        print("Fleet stopped", file=sys.stderr)


if __name__ == "__main__":
    main()
