"""Auto-tuner.

Every `tuner.evaluation_window_hours`, reads recent fills from SQLite and
nudges `market_maker.target_spread_bps` toward the desired fill rate:

  - Too-low fill rate → narrow the spread (more aggressive quoting).
  - Too-high fill rate → widen the spread (avoid adverse selection).
  - Large drawdown → widen spread AND raise `min_edge_over_mid_bps`.

All changes are logged to `state/tuner.log`. If `research.auto_apply` is true
and we're NOT in live mode, the new config is written to `config.yaml`; in
live mode the tuner only recommends — humans apply.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .config import Config, write_config
from .report import aggregate

log = logging.getLogger(__name__)

TUNER_LOG = Path("state/tuner.log")


@dataclass
class TunerResult:
    applied: bool
    changes: dict
    reason: str


class AutoTuner:
    def __init__(self, cfg: Config, live: bool):
        self._cfg = cfg
        self._live = live
        self._last_run: float = 0.0

    def due(self) -> bool:
        interval = self._cfg.tuner.evaluation_window_hours * 3600.0
        return (time.time() - self._last_run) >= interval

    def run(self) -> Optional[TunerResult]:
        if not self._cfg.tuner.enabled:
            return None
        self._last_run = time.time()
        try:
            agg = aggregate(hours=self._cfg.tuner.evaluation_window_hours)
        except Exception as e:
            log.warning("tuner: aggregate failed: %s", e)
            return None

        if agg.orders_placed < 20:
            log.info("tuner: skip (orders_placed=%d too low)", agg.orders_placed)
            return None

        changes: dict = {}
        reason_parts = []

        current_spread = self._cfg.market_maker.target_spread_bps
        target = self._cfg.tuner.target_fill_rate
        lr = self._cfg.tuner.learning_rate

        # Fill-rate feedback.
        if agg.fill_rate < target * 0.5:
            new_spread = max(5.0, current_spread * (1 - lr))
            changes["target_spread_bps"] = new_spread
            reason_parts.append(f"fill_rate={agg.fill_rate:.2%}<target/2 → narrow")
        elif agg.fill_rate > target * 1.5:
            new_spread = min(150.0, current_spread * (1 + lr))
            changes["target_spread_bps"] = new_spread
            reason_parts.append(f"fill_rate={agg.fill_rate:.2%}>1.5×target → widen")

        # Drawdown response.
        if agg.realized_pnl < -10.0:
            changes["min_edge_over_mid_bps"] = min(
                50.0, self._cfg.market_maker.min_edge_over_mid_bps + 5
            )
            changes["target_spread_bps"] = max(
                changes.get("target_spread_bps", current_spread) * 1.1,
                current_spread + 10,
            )
            reason_parts.append(f"pnl=${agg.realized_pnl:.2f} → defensive widen")

        if not changes:
            return TunerResult(applied=False, changes={}, reason="no-change")

        reason = "; ".join(reason_parts)
        TUNER_LOG.parent.mkdir(parents=True, exist_ok=True)
        with TUNER_LOG.open("a") as f:
            f.write(f"{int(time.time())} {changes} {reason}\n")

        auto_apply = self._cfg.research.auto_apply and not self._live
        if auto_apply:
            for k, v in changes.items():
                setattr(self._cfg.market_maker, k, v)
            write_config(self._cfg)
            log.info("tuner: applied %s (%s)", changes, reason)
            return TunerResult(applied=True, changes=changes, reason=reason)

        log.info("tuner: recommend %s (%s) — not auto-applied", changes, reason)
        return TunerResult(applied=False, changes=changes, reason=reason)
