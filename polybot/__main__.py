"""Entrypoint: `python -m polybot`."""

from __future__ import annotations

import argparse
import logging
import sys

from .bot import Polybot
from .config import load_config, load_secrets


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    # Silence noisy libraries.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def main() -> int:
    parser = argparse.ArgumentParser(prog="polybot")
    parser.add_argument("--config", default=None, help="YAML config path")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log decisions without sending any orders",
    )
    args = parser.parse_args()

    try:
        secrets = load_secrets() if not args.dry_run else _dry_secrets()
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    cfg = load_config(args.config)
    _setup_logging(secrets.log_level)

    Polybot(cfg=cfg, secrets=secrets, dry_run=args.dry_run).run()
    return 0


def _dry_secrets():
    """Allow --dry-run without real credentials."""
    from .config import Secrets
    import os

    return Secrets(
        private_key=os.getenv("PK_PRIVATE_KEY", "0x" + "0" * 64),
        funder=os.getenv("PK_FUNDER", "0x" + "0" * 40),
        signature_type=int(os.getenv("PK_SIGNATURE_TYPE", "1")),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
    )


if __name__ == "__main__":
    raise SystemExit(main())
