"""Mean-reversion scalper (opt-in).

For each tracked market we keep a short rolling midpoint series. When the
latest midpoint is `z > zscore_trigger` away from the rolling mean, we take
a small position expecting a reversion within `max_hold_sec`.

Disabled by default — inventory risk is real on thin markets.
"""

from __future__ import annotations

import logging
import math
import time
from collections import deque
from typing import Deque, Dict, Sequence

from ..clob import ClobClientWrapper
from ..config import MeanReversionCfg
from ..models import OrderBook, TokenMarket
from ..risk import RiskManager

log = logging.getLogger(__name__)


class MeanReversionStrategy:
    name = "mean_reversion"

    def __init__(
        self,
        cfg: MeanReversionCfg,
        clob: ClobClientWrapper,
        risk: RiskManager,
    ):
        self._cfg = cfg
        self._clob = clob
        self._risk = risk
        self._series: Dict[str, Deque[tuple[float, float]]] = {}  # token_id -> [(ts, mid)]
        self._open: Dict[str, tuple[str, float, float]] = {}  # token_id -> (side, entry_mid, ts)

    def on_tick(
        self,
        markets: Sequence[TokenMarket],
        books: dict[str, OrderBook],
    ) -> None:
        if not self._cfg.enabled:
            return

        now = time.time()
        for market in markets:
            book = books.get(market.token_id)
            if not book or book.midpoint is None:
                continue
            series = self._series.setdefault(market.token_id, deque(maxlen=512))
            series.append((now, book.midpoint))
            # Evict stale.
            while series and now - series[0][0] > self._cfg.window_sec:
                series.popleft()
            if len(series) < 20:
                continue
            mids = [m for _t, m in series]
            mean = sum(mids) / len(mids)
            var = sum((x - mean) ** 2 for x in mids) / len(mids)
            sd = math.sqrt(var)
            if sd < 1e-4:
                continue
            z = (book.midpoint - mean) / sd

            # Exit: mean reached OR held too long.
            if market.token_id in self._open:
                side, entry, entered_at = self._open[market.token_id]
                if now - entered_at > self._cfg.max_hold_sec or (
                    (side == "BUY" and book.midpoint >= mean)
                    or (side == "SELL" and book.midpoint <= mean)
                ):
                    exit_side = "SELL" if side == "BUY" else "BUY"
                    self._clob.place_market(
                        market.token_id, exit_side, self._cfg.position_size_usd
                    )
                    del self._open[market.token_id]
                continue

            if self._risk.kill_switch_active():
                return
            if abs(z) < self._cfg.zscore_trigger:
                continue
            side = "SELL" if z > 0 else "BUY"
            ok = self._clob.place_market(
                market.token_id, side, self._cfg.position_size_usd
            )
            if ok:
                self._open[market.token_id] = (side, book.midpoint, now)
                log.info(
                    "MR open %s %s z=%.2f mid=%.4f",
                    side, market.token_id[:10], z, book.midpoint,
                )
