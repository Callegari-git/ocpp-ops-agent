"""Run one virtual charger: python -m ocpp_ops.charger --id CP_1 --url ws://127.0.0.1:9000"""

import argparse
import asyncio
import logging
import random
from contextlib import nullcontext
from pathlib import Path

from ocpp_ops.charger.charger import VirtualCharger
from ocpp_ops.clock import utc_now
from ocpp_ops.message_log import MessageLog

logger = logging.getLogger("ocpp_ops.charger")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m ocpp_ops.charger", description=__doc__)
    parser.add_argument("--id", required=True, help="charger identity, sent in the URL path")
    parser.add_argument("--url", default="ws://127.0.0.1:9000", help="CSMS WebSocket URL")
    parser.add_argument("--connectors", type=int, default=2)
    parser.add_argument("--seed", type=int, default=0, help="seed for jitter and message ids")
    parser.add_argument("--vendor", default="OcppOps")
    parser.add_argument("--model", default="VirtualCharger")
    parser.add_argument("--reboot-delay", type=float, default=5.0, help="seconds")
    parser.add_argument(
        "--log-file", type=Path, default=None, help="JSONL log of this charger's side (off)"
    )
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> None:
    """Run until cancelled (Ctrl+C), which closes the connection and the log file."""
    with MessageLog(args.log_file) if args.log_file else nullcontext() as log:
        charger = VirtualCharger(
            args.id,
            args.url,
            args.connectors,
            now=utc_now,
            sleep=asyncio.sleep,
            rng=random.Random(args.seed),
            vendor=args.vendor,
            model=args.model,
            log=log,
            reboot_delay=args.reboot_delay,
        )
        logger.info("Charger %s (seed %d) connecting to %s", args.id, args.seed, args.url)
        await charger.run()


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    # The ocpp library logs every frame at INFO; use --log-file to record them.
    logging.getLogger("ocpp").setLevel(logging.WARNING)
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        logger.info("Charger stopped")


if __name__ == "__main__":
    main()
