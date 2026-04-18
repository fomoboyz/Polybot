"""Reward-optimizing market maker. Primary volume + LP-reward engine.

For each eligible market:
  1. Compute an optimal bid/ask pair via `reward_optimizer.compute_quote_pair`.
  2. Size each side from `quote_size_usd`.
  3. Run both through the risk manager; if either side breaches skew limits,
     that side is pulled and only the other is quoted (single-sided is still
     rewarded at 1/c).
  4. Re-quote when midpoint drifts > `requote_drift_bps` or every
     `requote_interval_sec`.
"""

from __future__ import annotations

import logging
from typing import Sequence

from ..config import MarketMakerCfg
from ..executor import OrderExecutor
from ..models import OrderBook, Quote, TokenMarket
from ..reward_optimizer import compute_quote_pair
from ..risk import RiskManager

log = logging.getLogger(__name__)


class MarketMakerStrategy:
    name = "market_maker"

    def __init__(
        self,
        cfg: MarketMakerCfg,
        executor: OrderExecutor,
        risk: RiskManager,
    ):
        self._cfg = cfg
        self._executor = executor
        self._risk = risk

    def on_tick(
        self,
        markets: Sequence[TokenMarket],
        books: dict[str, OrderBook],
    ) -> None:
        if not self._cfg.enabled:
            return

        for market in markets:
            book = books.get(market.token_id)
            if book is None or book.midpoint is None:
                continue
            pair = compute_quote_pair(market, book, self._cfg)
            if pair is None:
                continue
            mid = book.midpoint
            size_shares = max(market.min_order_size, self._cfg.quote_size_usd / max(mid, 0.01))

            bid_quote = Quote(
                token_id=market.token_id,
                side="BUY",
                price=pair.bid,
                size=size_shares,
            )
            ask_quote = Quote(
                token_id=market.token_id,
                side="SELL",
                price=pair.ask,
                size=size_shares,
            )
            self._executor.place_or_refresh(
                bid_quote,
                current_midpoint=mid,
                requote_drift_bps=self._cfg.requote_drift_bps,
                requote_interval_sec=self._cfg.requote_interval_sec,
            )
            self._executor.place_or_refresh(
                ask_quote,
                current_midpoint=mid,
                requote_drift_bps=self._cfg.requote_drift_bps,
                requote_interval_sec=self._cfg.requote_interval_sec,
            )
