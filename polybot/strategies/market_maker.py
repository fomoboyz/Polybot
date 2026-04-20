"""Reward-optimizing market maker with laddered quotes and Kelly-lite sizing.

For each eligible market:
  1. Pull inventory + σ.
  2. Compute the optimal bid/ask pair with inventory-aware + adaptive spread.
  3. Expand into a ladder of N levels per side (ladder.py).
  4. Size each level via the allocator (size proportional to market edge,
     capped by per-market risk).
  5. Place/refresh each quote through the executor; risk manager gates.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, Optional, Sequence

from ..allocator import Allocator
from ..config import AllocatorCfg, LadderCfg, MarketMakerCfg, RiskCfg
from ..executor import OrderExecutor
from ..ladder import ladder_quotes
from ..models import OrderBook, Quote, TokenMarket
from ..reward_optimizer import compute_quote_pair, rank_markets_by_reward
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
        ladder_cfg: Optional[LadderCfg] = None,
        allocator_cfg: Optional[AllocatorCfg] = None,
        risk_cfg: Optional[RiskCfg] = None,
    ):
        self._cfg = cfg
        self._executor = executor
        self._risk = risk
        self._vol = volatility
        self._ladder_cfg = ladder_cfg
        self._allocator: Optional[Allocator] = (
            Allocator(allocator_cfg, risk_cfg)
            if allocator_cfg and risk_cfg else None
        )
        # token_id → wall-clock until which the market is on cooldown after a
        # fast adverse move.
        self._cooldowns: Dict[str, float] = {}

    def on_tick(
        self,
        markets: Sequence[TokenMarket],
        books: dict[str, OrderBook],
    ) -> None:
        if not self._cfg.enabled:
            return

        # Per-market allocation based on expected score × liquidity. When the
        # allocator is disabled we fall back to the flat `quote_size_usd`.
        ranked = rank_markets_by_reward(markets, books, self._cfg)
        if self._allocator is not None and self._allocator.enabled:
            total_budget = self._remaining_budget()
            size_map = self._allocator.allocate(ranked, total_budget)
        else:
            size_map = {}

        now = time.time()
        for market in markets:
            book = books.get(market.token_id)
            if book is None or book.midpoint is None:
                continue

            # Per-market fast-move guard: if the mid just moved sharply,
            # cancel any of our resting orders here and skip quoting until
            # the cooldown expires. Protects against adverse selection.
            if self._fast_move_active(market.token_id, now):
                self._executor.cancel_all_for(market.token_id)
                continue

            inventory = self._current_inventory(market.token_id)
            sigma = self._vol.sigma(market.token_id) if self._vol else None

            size_usd = None  # computed after allocator below; passed in for scoring
            pair = compute_quote_pair(
                market, book, self._cfg,
                inventory_shares=inventory, sigma=sigma,
                size_usd=size_usd,
            )
            if pair is None:
                continue

            mid = book.midpoint
            size_usd = size_map.get(market.token_id, self._cfg.quote_size_usd)
            base_size_shares = max(
                market.min_order_size,
                size_usd / max(mid, 0.01),
            )

            if self._ladder_cfg is not None and self._ladder_cfg.enabled:
                quotes = ladder_quotes(
                    market, pair, self._cfg, self._ladder_cfg, base_size_shares,
                )
            else:
                quotes = [
                    Quote(token_id=market.token_id, side="BUY",
                          price=pair.bid, size=base_size_shares),
                    Quote(token_id=market.token_id, side="SELL",
                          price=pair.ask, size=base_size_shares),
                ]

            for q in quotes:
                self._executor.place_or_refresh(
                    q,
                    current_midpoint=mid,
                    requote_drift_bps=self._cfg.requote_drift_bps,
                    requote_interval_sec=self._cfg.requote_interval_sec,
                )

    def _current_inventory(self, token_id: str) -> float:
        pos = self._risk.state.positions.get(token_id)
        return pos.net_shares if pos else 0.0

    def _remaining_budget(self) -> float:
        used = self._risk.state.resting_notional_usd + self._risk.state.positions_notional_usd
        return max(0.0, self._risk.cfg.max_notional_usd - used)

    def _fast_move_active(self, token_id: str, now: float) -> bool:
        """True if the market is in cooldown after a large recent mid move."""
        expiry = self._cooldowns.get(token_id, 0.0)
        if expiry > now:
            return True
        if self._vol is None:
            return False
        delta = self._vol.midpoint_delta_bps(
            token_id, lookback_sec=self._cfg.fast_move_lookback_sec,
        )
        if delta is not None and delta >= self._cfg.fast_move_bps:
            self._cooldowns[token_id] = now + self._cfg.fast_move_cooldown_sec
            log.info(
                "mm: fast-move cooldown on %s (Δ%.0fbps over %.0fs)",
                token_id[:10], delta, self._cfg.fast_move_lookback_sec,
            )
            return True
        return False
