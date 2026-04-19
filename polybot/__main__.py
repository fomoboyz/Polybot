"""Entrypoint: `python -m polybot [run|report|research] ...`."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .bot import Polybot
from .config import load_config, load_secrets
from .report import render_markdown, run_report
from .research import ResearchEngine


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _cmd_run(args: argparse.Namespace) -> int:
    require_wallet = not (args.dry_run or args.paper)
    try:
        secrets = load_secrets(require_wallet=require_wallet)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    cfg = load_config(args.config)
    _setup_logging(secrets.log_level)
    Polybot(cfg=cfg, secrets=secrets, dry_run=args.dry_run, paper=args.paper).run()
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    _setup_logging("INFO")
    out = run_report(hours=args.hours, db=args.db)
    print(out.read_text())
    print(f"\n→ written to {out}")
    return 0


def _cmd_research(args: argparse.Namespace) -> int:
    secrets = load_secrets(require_wallet=False)
    cfg = load_config(args.config)
    _setup_logging(secrets.log_level)
    engine = ResearchEngine(cfg, secrets)
    try:
        snap = engine.run()
        print(json.dumps(
            {"wallets": len(snap.top_wallets), "markets": len(snap.top_markets_by_volume),
             "rewarded": len(snap.reward_eligible_markets), "notes": snap.notes},
            indent=2,
        ))
    finally:
        engine.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="polybot")
    sub = parser.add_subparsers(dest="command")

    # Default (no subcommand) behaves like `run`.
    run = sub.add_parser("run", help="Run the trading bot loop")
    for p in (parser, run):
        p.add_argument("--config", default=None, help="YAML config path")
        p.add_argument("--dry-run", action="store_true", help="Log decisions, post no orders")
        p.add_argument("--paper", action="store_true", help="Real data, simulated fills")
    run.set_defaults(func=_cmd_run)

    rep = sub.add_parser("report", help="Generate a performance report")
    rep.add_argument("--hours", type=float, default=24.0)
    rep.add_argument("--db", default="state/polybot.sqlite")
    rep.set_defaults(func=_cmd_report)

    res = sub.add_parser("research", help="Run one research pass and exit")
    res.add_argument("--config", default=None)
    res.set_defaults(func=_cmd_research)

    args = parser.parse_args()
    if not getattr(args, "func", None):
        args.func = _cmd_run
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
