"""Auto-tuner — the self-improvement loop.

Fires every `tuner.evaluation_window_hours`. Two feedback paths:

 1. **Local feedback** (reads SQLite fills over the last
    `tuner.lookback_hours`):
      - Too-low fill rate → narrow `market_maker.target_spread_bps`.
      - Too-high fill rate → widen it.
      - Drawdown → widen AND raise `market_maker.min_edge_over_mid_bps`.

 2. **Research-driven** (reads `state/research/latest.json`):
      - Every recommendation above `research_confidence_threshold` is applied
        IF its lever is on the allowlist AND the new value is within the
        lever's safe bounds. Everything else is logged and skipped.

All changes (applied AND recommended-only) are appended to `state/tuner.log`
with a reason tag so the dashboard can show the audit trail.

If `research.auto_apply` is true AND we're not in live mode, the new config
is serialized to disk (via write_config). In live mode, the tuner only
recommends — humans apply.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .config import Config, write_config
from .report import aggregate

log = logging.getLogger(__name__)

TUNER_LOG = Path("state/tuner.log")
RESEARCH_LATEST = Path("state/research/latest.json")

# Allowlist: only these levers can be touched by the tuner, even from
# research. Each entry maps the dotted path to (min, max) bounds. Anything
# outside is clipped; unknown levers are rejected outright.
LEVER_BOUNDS: Dict[str, Tuple[float, float]] = {
    "market_maker.target_spread_bps": (5.0, 150.0),
    "market_maker.min_edge_over_mid_bps": (2.0, 80.0),
    "market_maker.quote_size_usd": (1.0, 50.0),
    "market_maker.requote_drift_bps": (5.0, 200.0),
    "scanner.max_concurrent_markets": (1.0, 50.0),
    "ladder.levels": (1.0, 5.0),
    "ladder.step_bps": (5.0, 100.0),
}

# Boolean levers — no numeric bounds, but still gated by the allowlist.
BOOL_LEVERS = {
    "arbitrage.enabled",
    "mean_reversion.enabled",
    "ladder.enabled",
    "allocator.enabled",
    "market_maker.use_inventory_skew",
    "market_maker.use_adaptive_spread",
}


@dataclass
class TunerResult:
    applied: bool
    changes: Dict[str, Any] = field(default_factory=dict)
    research_changes: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""


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

        lookback = (
            self._cfg.tuner.lookback_hours
            if self._cfg.tuner.lookback_hours is not None
            else self._cfg.tuner.evaluation_window_hours
        )

        all_changes: Dict[str, Any] = {}
        reasons: List[str] = []

        # ---- Local fill-rate / drawdown feedback ----
        try:
            agg = aggregate(hours=lookback)
        except Exception as e:
            log.warning("tuner: aggregate failed: %s", e)
            agg = None

        if agg is not None and agg.orders_placed >= self._cfg.tuner.min_orders_for_feedback:
            local_changes, local_reason = self._local_feedback(agg)
            for k, v in local_changes.items():
                all_changes[k] = v
            if local_reason:
                reasons.append(local_reason)
        elif agg is not None:
            log.info(
                "tuner: skip local feedback (orders=%d < %d) — research still applies",
                agg.orders_placed, self._cfg.tuner.min_orders_for_feedback,
            )

        # ---- Research-driven recommendations ----
        research_changes, research_reason = self._apply_research_recommendations()
        for k, v in research_changes.items():
            # Research never overrides a local defensive widening on drawdown.
            if k in all_changes and "defensive" in " ".join(reasons):
                continue
            all_changes[k] = v
        if research_reason:
            reasons.append(research_reason)

        if not all_changes:
            return TunerResult(applied=False, reason="no-change")

        reason = "; ".join(reasons) if reasons else "(no reason)"
        TUNER_LOG.parent.mkdir(parents=True, exist_ok=True)
        with TUNER_LOG.open("a") as f:
            f.write(f"{int(time.time())} {all_changes} {reason}\n")

        auto_apply = self._cfg.research.auto_apply and not self._live
        if auto_apply:
            self._apply_to_cfg(all_changes)
            write_config(self._cfg)
            log.info("tuner: applied %s (%s)", all_changes, reason)
            return TunerResult(
                applied=True, changes=all_changes,
                research_changes=research_changes, reason=reason,
            )

        log.info("tuner: recommend %s (%s) — not auto-applied", all_changes, reason)
        return TunerResult(
            applied=False, changes=all_changes,
            research_changes=research_changes, reason=reason,
        )

    # ---- internals ----

    def _local_feedback(self, agg) -> Tuple[Dict[str, Any], str]:
        changes: Dict[str, Any] = {}
        reason_parts: List[str] = []
        mm = self._cfg.market_maker
        target = self._cfg.tuner.target_fill_rate
        lr = self._cfg.tuner.learning_rate

        if agg.fill_rate < target * 0.5:
            new = max(5.0, mm.target_spread_bps * (1 - lr))
            changes["market_maker.target_spread_bps"] = new
            reason_parts.append(f"fill_rate={agg.fill_rate:.2%}<target/2 → narrow")
        elif agg.fill_rate > target * 1.5:
            new = min(150.0, mm.target_spread_bps * (1 + lr))
            changes["market_maker.target_spread_bps"] = new
            reason_parts.append(f"fill_rate={agg.fill_rate:.2%}>1.5×target → widen")

        if agg.realized_pnl < -10.0:
            changes["market_maker.min_edge_over_mid_bps"] = min(
                50.0, mm.min_edge_over_mid_bps + 5
            )
            current_spread = changes.get(
                "market_maker.target_spread_bps", mm.target_spread_bps,
            )
            changes["market_maker.target_spread_bps"] = max(
                current_spread * 1.1, mm.target_spread_bps + 10,
            )
            reason_parts.append(f"pnl=${agg.realized_pnl:.2f} → defensive widen")

        return changes, "; ".join(reason_parts)

    def _apply_research_recommendations(self) -> Tuple[Dict[str, Any], str]:
        if not RESEARCH_LATEST.exists():
            return {}, ""
        try:
            snap = json.loads(RESEARCH_LATEST.read_text())
        except Exception as e:
            log.warning("tuner: cannot read research snapshot: %s", e)
            return {}, ""
        recs = snap.get("recommendations") or []
        threshold = self._cfg.tuner.research_confidence_threshold
        changes: Dict[str, Any] = {}
        parts: List[str] = []
        for rec in recs:
            lever = rec.get("lever", "")
            conf = float(rec.get("confidence", 0.0) or 0.0)
            recommended = rec.get("recommended")
            if conf < threshold:
                log.debug("tuner: skip low-confidence rec (%s conf=%.2f)", lever, conf)
                continue
            if lever in BOOL_LEVERS:
                if not isinstance(recommended, bool):
                    continue
                changes[lever] = recommended
                parts.append(f"research:{lever}={recommended}")
                continue
            if lever in LEVER_BOUNDS:
                try:
                    num = float(recommended)
                except (TypeError, ValueError):
                    continue
                lo, hi = LEVER_BOUNDS[lever]
                clipped = max(lo, min(hi, num))
                changes[lever] = clipped
                parts.append(
                    f"research:{lever}={clipped:g}"
                    + (" (clipped)" if clipped != num else "")
                )
                continue
            log.debug("tuner: lever %s not in allowlist — ignored", lever)
        return changes, "; ".join(parts)

    def _apply_to_cfg(self, changes: Dict[str, Any]) -> None:
        for path, value in changes.items():
            section, _, field_name = path.partition(".")
            if not section or not field_name:
                continue
            obj = getattr(self._cfg, section, None)
            if obj is None or not hasattr(obj, field_name):
                log.warning("tuner: unknown lever %s — skipping", path)
                continue
            setattr(obj, field_name, value)
