"""Entrypoint: `python -m polybot [run|report|research] ...`."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .backtest import run_cli_backtest
from .bot import Polybot
from .config import load_config, load_secrets
from .observability import configure_logging as _setup_logging
from .report import render_markdown, run_report
from .research import ResearchEngine
from .trial import TrialRunner


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


def _cmd_backtest(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    _setup_logging("INFO")
    overrides = {}
    if args.target_spread_bps is not None:
        overrides["target_spread_bps"] = args.target_spread_bps
    if args.quote_size_usd is not None:
        overrides["quote_size_usd"] = args.quote_size_usd
    if args.min_edge_over_mid_bps is not None:
        overrides["min_edge_over_mid_bps"] = args.min_edge_over_mid_bps
    result = run_cli_backtest(
        cfg,
        hours=args.hours,
        overrides=overrides,
        fill_probability=args.fill_probability,
    )
    print(result.summary())
    print(f"per-token PnL (top 10):")
    top = sorted(result.per_token_pnl.items(), key=lambda x: abs(x[1]), reverse=True)[:10]
    for tid, pnl in top:
        print(f"  {tid[:16]}… ${pnl:+.4f}")
    return 0


def _cmd_trial(args: argparse.Namespace) -> int:
    """Run a fixed-duration paper trial (default 7 days, 3h auto-improve)."""
    secrets = load_secrets(require_wallet=False)
    cfg = load_config(args.config)
    _setup_logging(secrets.log_level, cfg.observability.json_logs)
    trial = TrialRunner(
        duration_hours=args.days * 24.0,
        checkpoint_interval_hours=args.checkpoint_hours,
        starting_balance_usd=args.starting_balance,
        fresh=args.fresh,
    )
    Polybot(cfg=cfg, secrets=secrets, dry_run=False, paper=True, trial=trial).run()
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

    tr = sub.add_parser(
        "trial",
        help="Run a fixed-duration paper trial (real data, simulated fills)",
    )
    tr.add_argument("--config", default="config.trial.yaml")
    tr.add_argument("--days", type=float, default=7.0)
    tr.add_argument("--checkpoint-hours", type=float, default=3.0)
    tr.add_argument("--starting-balance", type=float, default=100.0)
    tr.add_argument(
        "--fresh", action="store_true",
        help="Ignore any existing trial state and start a new trial",
    )
    tr.set_defaults(func=_cmd_trial)

    bt = sub.add_parser("backtest", help="Replay recorded books against current config")
    bt.add_argument("--config", default=None)
    bt.add_argument("--hours", type=float, default=12.0)
    bt.add_argument("--fill-probability", type=float, default=0.5)
    bt.add_argument("--target-spread-bps", type=float, default=None)
    bt.add_argument("--quote-size-usd", type=float, default=None)
    bt.add_argument("--min-edge-over-mid-bps", type=float, default=None)
    bt.set_defaults(func=_cmd_backtest)

    args = parser.parse_args()
    if not getattr(args, "func", None):
        args.func = _cmd_run
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
