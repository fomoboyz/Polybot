"""Reward-optimizing market maker. Primary volume + LP-reward engine.

For each eligible market:
  1. Pull current inventory (shares) from the risk manager.
  2. Pull realized volatility σ from the VolatilityTracker.
  3. Compute bid/ask via `reward_optimizer.compute_quote_pair` — inventory
     skew and adaptive spread layered on top of the reward-score optimum.
  4. Run each side through the risk manager; a side blocked by the skew or
     notional gate is silently dropped — the other side still quotes.
  5. Re-quote on drift > `requote_drift_bps` or on the `requote_interval_sec`
     heartbeat.
"""

from __future__ import annotations

import logging
from typing import Optional, Sequence

from ..config import MarketMakerCfg
from ..executor import OrderExecutor
from ..models import OrderBook, Quote, TokenMarket
from ..reward_optimizer import compute_quote_pair
from ..risk import RiskManager
from ..volatility import VolatilityTracker

log = logging.getLogger(__name__)


class MarketMakerStrategy:
    name = "market_maker"

    def __init__(
        self,
        cfg: MarketMakerCfg,
        executor: OrderExecutor,
        risk: RiskManager,
        volatility: Optional[VolatilityTracker] = None,
    ):
        self._cfg = cfg
        self._executor = executor
        self._risk = risk
        self._vol = volatility

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

            inventory_shares = self._current_inventory(market.token_id)
            sigma = self._vol.sigma(market.token_id) if self._vol else None

            pair = compute_quote_pair(
                market,
                book,
                self._cfg,
                inventory_shares=inventory_shares,
                sigma=sigma,
            )
            if pair is None:
                continue

            mid = book.midpoint
            size_shares = max(
                market.min_order_size,
                self._cfg.quote_size_usd / max(mid, 0.01),
            )

            bid_quote = Quote(
                token_id=market.token_id, side="BUY",
                price=pair.bid, size=size_shares,
            )
            ask_quote = Quote(
                token_id=market.token_id, side="SELL",
                price=pair.ask, size=size_shares,
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

    def _current_inventory(self, token_id: str) -> float:
        pos = self._risk.state.positions.get(token_id)
        return pos.net_shares if pos else 0.0
