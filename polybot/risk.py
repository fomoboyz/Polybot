"""Risk manager. Hard ceilings + kill-switch + inventory skew control.

Every order the strategies want to place goes through `RiskManager.validate`.
If any limit is breached the order is rejected and the reason is logged.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

from .config import RiskCfg
from .models import Position, Quote

log = logging.getLogger(__name__)

KILL_FILE = Path("state/KILL")


@dataclass
class RiskState:
    realized_pnl_today: float = 0.0
    unrealized_pnl: float = 0.0
    resting_notional_usd: float = 0.0
    positions_notional_usd: float = 0.0
    per_market_notional: Dict[str, float] = field(default_factory=dict)
    positions: Dict[str, Position] = field(default_factory=dict)
    day_started_at: float = field(default_factory=time.time)


class RiskManager:
    def __init__(self, cfg: RiskCfg):
        self.cfg = cfg
        self.state = RiskState()
        KILL_FILE.parent.mkdir(parents=True, exist_ok=True)

    # ---- invariants ----

    def kill_switch_active(self) -> bool:
        return KILL_FILE.exists()

    def _trip_kill_switch(self, reason: str) -> None:
        log.error("KILL SWITCH: %s", reason)
        try:
            KILL_FILE.write_text(f"{time.time()}: {reason}\n")
        except OSError as e:
            log.error("could not write kill file: %s", e)

    def reset_daily_if_needed(self) -> None:
        # Roll at 00:00 UTC.
        now = time.gmtime()
        day_started = time.gmtime(self.state.day_started_at)
        if now.tm_yday != day_started.tm_yday or now.tm_year != day_started.tm_year:
            log.info("daily reset: PnL=%.4f", self.state.realized_pnl_today)
            self.state.realized_pnl_today = 0.0
            self.state.day_started_at = time.time()

    # ---- validation ----

    def validate(self, quote: Quote) -> Optional[str]:
        """Return None if the order is OK to place, else a reason string."""
        self.reset_daily_if_needed()
        if self.kill_switch_active():
            return "kill-switch-active"
        if self.state.realized_pnl_today <= -self.cfg.max_daily_loss_usd:
            self._trip_kill_switch(
                f"daily loss {self.state.realized_pnl_today:.2f} <= "
                f"-{self.cfg.max_daily_loss_usd}"
            )
            return "daily-loss-breached"
        notional = quote.price * quote.size
        if notional < self.cfg.min_order_usd:
            return f"below-min-order-{notional:.2f}<{self.cfg.min_order_usd}"
        projected_total = (
            self.state.resting_notional_usd
            + self.state.positions_notional_usd
            + notional
        )
        if projected_total > self.cfg.max_notional_usd:
            return f"total-notional-cap-{projected_total:.2f}>{self.cfg.max_notional_usd}"
        per = self.state.per_market_notional.get(quote.token_id, 0.0) + notional
        if per > self.cfg.max_per_market_usd:
            return f"per-market-cap-{per:.2f}>{self.cfg.max_per_market_usd}"
        if self._would_worsen_skew(quote):
            return "inventory-skew-cap"
        return None

    def _would_worsen_skew(self, quote: Quote) -> bool:
        """Prevent the market maker from adding to the heavier side of a
        market once inventory skew is breached."""
        pos = self.state.positions.get(quote.token_id)
        if pos is None:
            return False
        skew = abs(pos.net_shares)
        if skew < self.cfg.max_inventory_skew:
            return False
        # We are already skewed. Block any order that adds to the heavy side.
        adding_long = quote.side == "BUY" and pos.net_shares > 0
        adding_short = quote.side == "SELL" and pos.net_shares < 0
        return adding_long or adding_short

    # ---- accounting ----

    def on_rest(self, quote: Quote) -> None:
        notional = quote.price * quote.size
        self.state.resting_notional_usd += notional
        self.state.per_market_notional[quote.token_id] = (
            self.state.per_market_notional.get(quote.token_id, 0.0) + notional
        )

    def on_unrest(self, quote: Quote) -> None:
        notional = quote.price * quote.size
        self.state.resting_notional_usd = max(
            0.0, self.state.resting_notional_usd - notional
        )
        cur = self.state.per_market_notional.get(quote.token_id, 0.0) - notional
        self.state.per_market_notional[quote.token_id] = max(0.0, cur)

    def on_fill(self, token_id: str, side: str, shares: float, price: float) -> None:
        pos = self.state.positions.setdefault(token_id, Position(token_id=token_id))
        pre_pnl = pos.realized_pnl
        pos.apply_fill(side, shares, price)  # type: ignore[arg-type]
        delta = pos.realized_pnl - pre_pnl
        self.state.realized_pnl_today += delta
        # Re-value positions — approximate marked at last fill price.
        self._revalue_positions()

    def _revalue_positions(self) -> None:
        self.state.positions_notional_usd = sum(
            abs(p.net_shares) * p.avg_cost for p in self.state.positions.values()
        )

    # ---- introspection ----

    def snapshot(self) -> dict:
        return {
            "pnl_today": round(self.state.realized_pnl_today, 4),
            "resting_usd": round(self.state.resting_notional_usd, 2),
            "positions_usd": round(self.state.positions_notional_usd, 2),
            "kill": self.kill_switch_active(),
            "n_positions": sum(
                1 for p in self.state.positions.values() if p.net_shares != 0
            ),
        }
