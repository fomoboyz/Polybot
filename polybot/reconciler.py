"""Position reconciler.

Periodically compares our in-memory position ledger with whatever the CLOB
reports (open orders + settled positions). Any drift is logged and — if
`auto_correct=True` — corrected by overwriting the internal ledger.

Drift can happen from:
  - a missed fill event during a disconnect
  - a race between our cancel and an external fill
  - manual trading on the same wallet

The reconciler never trades. It only adjusts the internal model so the risk
manager's future decisions use accurate inventory.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List

from .clob import ClobClientWrapper
from .config import ReconcilerCfg
from .models import Position
from .risk import RiskManager

log = logging.getLogger(__name__)


@dataclass
class ReconcileResult:
    checked_at: float
    corrections: List[str] = field(default_factory=list)
    discrepancies: Dict[str, tuple] = field(default_factory=dict)


class Reconciler:
    def __init__(self, cfg: ReconcilerCfg, clob: ClobClientWrapper, risk: RiskManager):
        self._cfg = cfg
        self._clob = clob
        self._risk = risk
        self._last_run: float = 0.0

    def due(self) -> bool:
        return (time.time() - self._last_run) >= self._cfg.interval_sec

    def run(self) -> ReconcileResult:
        self._last_run = time.time()
        result = ReconcileResult(checked_at=self._last_run)
        if not self._cfg.enabled:
            return result

        # In paper/dry-run mode we trust our own ledger.
        if self._clob.dry_run or self._clob.paper:
            return result

        remote = self._fetch_remote_positions()
        local = {
            tid: pos.net_shares
            for tid, pos in self._risk.state.positions.items()
            if pos.net_shares != 0
        }
        tokens = set(remote) | set(local)
        for tid in tokens:
            r = remote.get(tid, 0.0)
            l = local.get(tid, 0.0)
            if abs(r - l) < 1e-4:
                continue
            result.discrepancies[tid] = (l, r)
            msg = f"drift {tid[:10]}… local={l:.4f} remote={r:.4f}"
            log.warning("reconcile: %s", msg)
            if self._cfg.auto_correct:
                pos = self._risk.state.positions.setdefault(tid, Position(token_id=tid))
                pos.net_shares = r
                result.corrections.append(msg)
        return result

    def _fetch_remote_positions(self) -> Dict[str, float]:
        """Best-effort. py-clob-client doesn't standardize position reads —
        we try a few shapes and fall back to the open-orders endpoint for a
        minimum-viable reconciliation."""
        try:
            orders = self._clob.get_open_orders() or []
        except Exception as e:
            log.debug("reconcile: get_open_orders failed: %s", e)
            return {}
        # Orders don't equal settled position, but their aggregate gives us
        # an approximate resting-side picture for sanity checks. A full
        # reconciliation would call `positions` on the data-api which varies
        # by account type.
        out: Dict[str, float] = {}
        for o in orders:
            tid = str(o.get("asset_id") or o.get("token_id") or "")
            if not tid:
                continue
            side = str(o.get("side", "")).upper()
            size = float(o.get("size_matched") or o.get("filled") or 0)
            if size <= 0:
                continue
            out[tid] = out.get(tid, 0.0) + (size if side == "BUY" else -size)
        return out
