"""Order lifecycle management. Tracks resting orders per (token_id, side)
and re-quotes on drift or staleness."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from .clob import ClobClientWrapper, PlacedOrder
from .models import Quote
from .risk import RiskManager

log = logging.getLogger(__name__)


@dataclass
class RestingOrder:
    placed: PlacedOrder
    midpoint_at_place: float


class OrderExecutor:
    """Resting orders are keyed by (token_id, side)."""

    def __init__(self, clob: ClobClientWrapper, risk: RiskManager):
        self._clob = clob
        self._risk = risk
        self._resting: Dict[Tuple[str, str], RestingOrder] = {}

    def place_or_refresh(
        self,
        quote: Quote,
        current_midpoint: float,
        requote_drift_bps: float,
        requote_interval_sec: int,
    ) -> None:
        key = (quote.token_id, quote.side)
        existing = self._resting.get(key)

        if existing is not None:
            drift = abs(current_midpoint - existing.midpoint_at_place) * 10000.0
            stale = time.time() - existing.placed.created_at > requote_interval_sec
            same_price = abs(existing.placed.quote.price - quote.price) < 1e-9
            if same_price and not stale and drift <= requote_drift_bps:
                return  # still good
            self._cancel(key)

        reason = self._risk.validate(quote)
        if reason:
            log.debug("risk reject %s %s: %s", quote.side, quote.token_id[:10], reason)
            return

        placed = self._clob.place_limit(quote)
        if placed is None:
            return
        self._risk.on_rest(quote)
        self._resting[key] = RestingOrder(placed=placed, midpoint_at_place=current_midpoint)

    def cancel_side(self, token_id: str, side: str) -> None:
        self._cancel((token_id, side))

    def cancel_all_for(self, token_id: str) -> None:
        for side in ("BUY", "SELL"):
            self._cancel((token_id, side))

    def _cancel(self, key: Tuple[str, str]) -> None:
        existing = self._resting.pop(key, None)
        if existing is None:
            return
        self._clob.cancel(existing.placed.order_id)
        self._risk.on_unrest(existing.placed.quote)

    def cancel_everything(self) -> None:
        for key in list(self._resting.keys()):
            self._cancel(key)

    def resting_count(self) -> int:
        return len(self._resting)

    def resting_for(self, token_id: str) -> Dict[str, RestingOrder]:
        return {
            side: ord_
            for (tid, side), ord_ in self._resting.items()
            if tid == token_id
        }
