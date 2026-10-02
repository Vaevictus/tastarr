"""Command-line entry point: ``tastarr [--once] [--dry-run] [--healthcheck]``."""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time

from . import __version__
from .arr import Arr, ArrError
from .config import Config
from .state import State
from .taster import Taster
from .transmission import Transmission, TransmissionError

log = logging.getLogger("tastarr")


def setup_logging(data_dir: str) -> None:
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
    to_file = logging.handlers.RotatingFileHandler(
        os.path.join(data_dir, "tastarr.log"), maxBytes=5_000_000, backupCount=3
    )
    to_stdout = logging.StreamHandler(sys.stdout)
    for handler in (to_file, to_stdout):
        handler.setFormatter(fmt)
        log.addHandler(handler)
    log.setLevel(logging.INFO)


def healthcheck(cfg: Config) -> int:
    """Exit 0 if a cycle completed recently (used by the container HEALTHCHECK)."""
    try:
        age = time.time() - os.path.getmtime(os.path.join(cfg.data_dir, "heartbeat"))
    except OSError:
        return 1
    return 0 if age < 3 * cfg.interval + 60 else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="tastarr",
        description="Reject fake and malicious Sonarr/Radarr downloads in Transmission. "
        "Configured through TASTARR_* environment variables.",
    )
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit")
    parser.add_argument("--dry-run", action="store_true", help="log decisions, change nothing")
    parser.add_argument(
        "--healthcheck", action="store_true", help="exit 0 if the running instance completed a cycle recently"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    cfg = Config.from_env()
    if args.healthcheck:
        return healthcheck(cfg)
    if args.dry_run:
        cfg.mode = "dry-run"
    os.makedirs(cfg.data_dir, exist_ok=True)
    setup_logging(cfg.data_dir)

    taster = Taster(
        cfg,
        Transmission(cfg.transmission_url, cfg.transmission_user, cfg.transmission_password),
        [Arr(a.name, a.url, a.api_key) for a in cfg.arrs],
        State(os.path.join(cfg.data_dir, "state.json")).load(),
    )
    log.info(
        "tastarr %s: mode=%s labels=%s arrs=%s interval=%ss",
        __version__,
        cfg.mode,
        ",".join(cfg.labels),
        ",".join(a.name for a in cfg.arrs) or "none",
        cfg.interval,
    )
    if not cfg.arrs:
        log.warning("no *arr configured: rejected torrents will only be stopped and labelled")

    stopping = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopping.set())
    while not stopping.is_set():
        try:
            taster.cycle()
        except (TransmissionError, ArrError) as exc:
            log.error("cycle failed: %s", exc)
        except Exception:  # keep tasting; one bad cycle shouldn't kill the daemon
            log.exception("cycle failed")
        if args.once:
            break
        stopping.wait(cfg.interval)
    log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
