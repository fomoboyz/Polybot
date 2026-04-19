"""Order lifecycle management. Resting orders are keyed by
`(token_id, side, slot)` so laddered quotes can coexist on the same side."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Dict, Tuple

from .clob import ClobClientWrapper, PlacedOrder
from .models import Quote
from .risk import RiskManager

log = logging.getLogger(__name__)

Key = Tuple[str, str, int]  # (token_id, side, slot)


@dataclass
class RestingOrder:
    placed: PlacedOrder
    midpoint_at_place: float


class OrderExecutor:
    def __init__(self, clob: ClobClientWrapper, risk: RiskManager):
        self._clob = clob
        self._risk = risk
        self._resting: Dict[Key, RestingOrder] = {}

    def place_or_refresh(
        self,
        quote: Quote,
        current_midpoint: float,
        requote_drift_bps: float,
        requote_interval_sec: int,
    ) -> None:
        key: Key = (quote.token_id, quote.side, quote.slot)
        existing = self._resting.get(key)

        if existing is not None:
            drift = abs(current_midpoint - existing.midpoint_at_place) * 10000.0
            stale = time.time() - existing.placed.created_at > requote_interval_sec
            same_price = abs(existing.placed.quote.price - quote.price) < 1e-9
            if same_price and not stale and drift <= requote_drift_bps:
                return
            self._cancel(key)

        reason = self._risk.validate(quote)
        if reason:
            log.debug(
                "risk reject %s %s slot=%d: %s",
                quote.side, quote.token_id[:10], quote.slot, reason,
            )
            return

        placed = self._clob.place_limit(quote)
        if placed is None:
            return
        self._risk.on_rest(quote)
        self._resting[key] = RestingOrder(placed=placed, midpoint_at_place=current_midpoint)

    def cancel_side(self, token_id: str, side: str) -> None:
        for key in list(self._resting.keys()):
            if key[0] == token_id and key[1] == side:
                self._cancel(key)

    def cancel_all_for(self, token_id: str) -> None:
        for key in list(self._resting.keys()):
            if key[0] == token_id:
                self._cancel(key)

    def _cancel(self, key: Key) -> None:
        existing = self._resting.pop(key, None)
        if existing is None:
            return
        self._clob.cancel(existing.placed.order_id)
        self._risk.on_unrest(existing.placed.quote)

    def cancel_everything(self) -> None:
        for key in list(self._resting.keys()):
            self._cancel(key)

    def mark_filled(self, order_id: str, token_id: str, side: str) -> None:
        """Clear a resting slot after an external fill. Matches on order_id
        to survive multi-slot ladders."""
        for key in list(self._resting.keys()):
            if key[0] != token_id or key[1] != side:
                continue
            ro = self._resting[key]
            if ro.placed.order_id == order_id:
                self._risk.on_unrest(ro.placed.quote)
                del self._resting[key]
                return

    def resting_count(self) -> int:
        return len(self._resting)

    def resting_for(self, token_id: str) -> Dict[Tuple[str, int], RestingOrder]:
        return {
            (side, slot): ord_
            for (tid, side, slot), ord_ in self._resting.items()
            if tid == token_id
        }
