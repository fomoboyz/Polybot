"""Fixed-duration trial runner.

Wraps a Polybot paper run, adds:
  - auto-exit after `duration_hours`
  - progress checkpoint every `checkpoint_interval_hours`, writing a
    markdown progress report + machine-readable JSON
  - persistent trial state so a restart resumes the same trial instead of
    starting fresh
  - final 7-day consolidated report on exit

Idempotent: if you crash and relaunch, it picks up where it left off and
counts down from the original start_ts.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

from .report import aggregate, render_markdown
from .risk import RiskManager

log = logging.getLogger(__name__)

TRIAL_DIR = Path("state/trial")
TRIAL_STATE_FILE = TRIAL_DIR / "state.json"


@dataclass
class TrialCheckpoint:
    ts: float
    pnl_today: float
    pnl_lifetime: float
    fills: int
    volume_usd: float
    resting_orders: int
    tuner_changes: dict = field(default_factory=dict)


@dataclass
class TrialState:
    start_ts: float
    duration_hours: float
    checkpoint_interval_hours: float
    starting_balance_usd: float = 100.0
    last_checkpoint_ts: float = 0.0
    checkpoints: List[dict] = field(default_factory=list)
    finalized: bool = False

    @property
    def end_ts(self) -> float:
        return self.start_ts + self.duration_hours * 3600.0

    def checkpoint_due(self) -> bool:
        return (time.time() - self.last_checkpoint_ts) >= (
            self.checkpoint_interval_hours * 3600.0
        )

    def expired(self) -> bool:
        return time.time() >= self.end_ts

    def hours_elapsed(self) -> float:
        return (time.time() - self.start_ts) / 3600.0

    def hours_remaining(self) -> float:
        return max(0.0, (self.end_ts - time.time()) / 3600.0)


class TrialRunner:
    def __init__(
        self,
        duration_hours: float = 168.0,
        checkpoint_interval_hours: float = 3.0,
        starting_balance_usd: float = 100.0,
        fresh: bool = False,
    ):
        TRIAL_DIR.mkdir(parents=True, exist_ok=True)
        self._duration = duration_hours
        self._interval = checkpoint_interval_hours
        self._balance = starting_balance_usd
        self.state: TrialState = self._load_or_init(fresh)

    def _load_or_init(self, fresh: bool) -> TrialState:
        if not fresh and TRIAL_STATE_FILE.exists():
            try:
                data = json.loads(TRIAL_STATE_FILE.read_text())
                state = TrialState(
                    start_ts=data["start_ts"],
                    duration_hours=data.get("duration_hours", self._duration),
                    checkpoint_interval_hours=data.get(
                        "checkpoint_interval_hours", self._interval,
                    ),
                    starting_balance_usd=data.get(
                        "starting_balance_usd", self._balance,
                    ),
                    last_checkpoint_ts=data.get("last_checkpoint_ts", 0.0),
                    checkpoints=data.get("checkpoints", []),
                    finalized=data.get("finalized", False),
                )
                log.info(
                    "trial resumed: %.1fh elapsed, %.1fh remaining",
                    state.hours_elapsed(), state.hours_remaining(),
                )
                return state
            except Exception as e:  # corrupt state — start fresh
                log.warning("trial state load failed (%s) — starting fresh", e)
        state = TrialState(
            start_ts=time.time(),
            duration_hours=self._duration,
            checkpoint_interval_hours=self._interval,
            starting_balance_usd=self._balance,
        )
        self._persist(state)
        log.info(
            "trial started: duration=%.1fh checkpoint=%.1fh balance=$%.2f",
            state.duration_hours, state.checkpoint_interval_hours,
            state.starting_balance_usd,
        )
        return state

    def _persist(self, state: Optional[TrialState] = None) -> None:
        s = state or self.state
        TRIAL_STATE_FILE.write_text(json.dumps(asdict(s), indent=2))

    # ---- public ----

    def should_exit(self) -> bool:
        return self.state.expired()

    def checkpoint_due(self) -> bool:
        return self.state.checkpoint_due()

    def record_checkpoint(self, risk: RiskManager, resting_orders: int) -> None:
        """Called every `checkpoint_interval_hours` from the bot loop."""
        now = time.time()
        agg = aggregate(hours=self.state.checkpoint_interval_hours)
        cp = TrialCheckpoint(
            ts=now,
            pnl_today=risk.state.realized_pnl_today,
            pnl_lifetime=risk.state.realized_pnl_lifetime,
            fills=agg.fills,
            volume_usd=agg.filled_volume_usd,
            resting_orders=resting_orders,
        )
        self.state.checkpoints.append(asdict(cp))
        self.state.last_checkpoint_ts = now
        self._persist()
        self._write_progress_report(risk)

    def finalize(self, risk: RiskManager) -> Path:
        """Called once on trial expiry. Writes the consolidated 7-day report."""
        out = self._write_final_report(risk)
        self.state.finalized = True
        self._persist()
        log.info("trial finalized → %s", out)
        return out

    # ---- writers ----

    def _write_progress_report(self, risk: RiskManager) -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
        out = TRIAL_DIR / f"progress-{stamp}.md"
        elapsed = self.state.hours_elapsed()
        remaining = self.state.hours_remaining()
        lifetime_pnl = risk.state.realized_pnl_lifetime
        pct = (lifetime_pnl / self.state.starting_balance_usd) * 100.0
        peak_dd = risk.state.peak_pnl_lifetime - risk.state.realized_pnl_lifetime
        content = [
            f"# Trial Progress — checkpoint {len(self.state.checkpoints)}",
            "",
            f"- **Elapsed:** {elapsed:.1f}h of {self.state.duration_hours:.0f}h"
            f" ({elapsed / self.state.duration_hours * 100:.1f}%)",
            f"- **Remaining:** {remaining:.1f}h",
            f"- **Starting balance:** ${self.state.starting_balance_usd:.2f} (paper)",
            f"- **Lifetime realized PnL:** ${lifetime_pnl:+.4f} ({pct:+.2f}%)",
            f"- **Peak → trough drawdown:** ${peak_dd:.4f}",
            f"- **Today's PnL:** ${risk.state.realized_pnl_today:+.4f}",
            f"- **Resting orders:** {risk.snapshot().get('resting_orders', 'n/a')}",
            "",
            "## Last window summary",
        ]
        if self.state.checkpoints:
            last = self.state.checkpoints[-1]
            content += [
                f"- Fills: {last.get('fills', 0)}",
                f"- Volume (USD): ${last.get('volume_usd', 0):,.2f}",
                f"- PnL today at checkpoint: ${last.get('pnl_today', 0):+.4f}",
            ]
        content += [
            "",
            "## Checkpoint history",
            "| # | Hours in | PnL lifetime | Fills | Volume |",
            "|---|---|---|---|---|",
        ]
        for i, cp in enumerate(self.state.checkpoints, 1):
            hours_in = (cp["ts"] - self.state.start_ts) / 3600.0
            content.append(
                f"| {i} | {hours_in:.1f}h | ${cp['pnl_lifetime']:+.4f} | "
                f"{cp['fills']} | ${cp['volume_usd']:,.2f} |"
            )
        out.write_text("\n".join(content))
        # Also overwrite the "latest" pointer.
        (TRIAL_DIR / "progress-latest.md").write_text("\n".join(content))
        return out

    def _write_final_report(self, risk: RiskManager) -> Path:
        out = TRIAL_DIR / "final-report.md"
        elapsed = self.state.hours_elapsed()
        total_pnl = risk.state.realized_pnl_lifetime
        pct = (total_pnl / self.state.starting_balance_usd) * 100.0
        peak_dd = risk.state.peak_pnl_lifetime - risk.state.realized_pnl_lifetime
        total_volume = sum(cp.get("volume_usd", 0) for cp in self.state.checkpoints)
        total_fills = sum(cp.get("fills", 0) for cp in self.state.checkpoints)

        # Success criteria from docs/SEVEN_DAY_PLAN.md.
        pass_pnl = total_pnl >= -5.0
        pass_volume = total_volume >= 500.0
        pass_dd = peak_dd <= 12.0
        strong = (
            total_pnl >= 3.0
            and total_volume >= 2000.0
            and peak_dd <= 6.0
        )

        lines = [
            "# Polybot 7-Day Trial — Final Report",
            "",
            f"_Generated: {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}_",
            "",
            "## Headline",
            "",
            f"- **Duration:** {elapsed:.1f}h (target {self.state.duration_hours:.0f}h)",
            f"- **Starting balance (paper):** ${self.state.starting_balance_usd:.2f}",
            f"- **Realized PnL:** ${total_pnl:+.4f} ({pct:+.2f}%)",
            f"- **Total volume:** ${total_volume:,.2f}",
            f"- **Total fills:** {total_fills}",
            f"- **Peak-to-trough drawdown:** ${peak_dd:.4f}",
            "",
            "## Success criteria",
            "",
            "| Check | Target | Result | Pass |",
            "|---|---|---|---|",
            f"| PnL | ≥ −$5 | ${total_pnl:+.4f} | {'✓' if pass_pnl else '✗'} |",
            f"| Volume | ≥ $500 | ${total_volume:,.2f} | {'✓' if pass_volume else '✗'} |",
            f"| Drawdown | ≤ $12 | ${peak_dd:.4f} | {'✓' if pass_dd else '✗'} |",
            "",
            f"**Verdict:** "
            + ("🌟 STRONG PASS" if strong
               else ("✓ PASS" if (pass_pnl and pass_volume and pass_dd)
                     else "✗ FAIL")),
            "",
            "## Checkpoint trajectory",
            "| # | Hours in | PnL lifetime | Fills | Volume |",
            "|---|---|---|---|---|",
        ]
        for i, cp in enumerate(self.state.checkpoints, 1):
            hours_in = (cp["ts"] - self.state.start_ts) / 3600.0
            lines.append(
                f"| {i} | {hours_in:.1f}h | ${cp['pnl_lifetime']:+.4f} | "
                f"{cp['fills']} | ${cp['volume_usd']:,.2f} |"
            )
        out.write_text("\n".join(lines))
        return out
