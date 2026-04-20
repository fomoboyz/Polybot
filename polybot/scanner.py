"""Market discovery & ranking."""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional

from .clob import ClobClientWrapper
from .config import MarketMakerCfg, ScannerCfg
from .gamma import GammaClient
from .models import OrderBook, TokenMarket
from .reward_optimizer import rank_markets_by_reward
from .volatility import VolatilityTracker

log = logging.getLogger(__name__)


class MarketScanner:
    def __init__(
        self,
        gamma: GammaClient,
        clob: ClobClientWrapper,
        scanner_cfg: ScannerCfg,
        mm_cfg: MarketMakerCfg,
        volatility: Optional[VolatilityTracker] = None,
    ):
        self._gamma = gamma
        self._clob = clob
        self._scanner_cfg = scanner_cfg
        self._mm_cfg = mm_cfg
        self._vol = volatility
        self._last_refresh: float = 0.0
        self._cached: List[TokenMarket] = []

    def active_markets(self) -> List[TokenMarket]:
        now = time.time()
        if now - self._last_refresh > self._scanner_cfg.refresh_interval_sec:
            self._refresh()
            self._last_refresh = now
        return self._cached

    def _refresh(self) -> None:
        try:
            candidates = self._gamma.list_markets(
                min_liquidity=self._scanner_cfg.min_liquidity_usd,
                min_volume_24h=self._scanner_cfg.min_volume_24h_usd,
                rewards_only=self._scanner_cfg.rewards_only,
                exclude_closing_within_hours=self._scanner_cfg.exclude_closing_within_hours,
            )
        except Exception as e:
            log.warning("gamma list_markets failed: %s", e)
            return

        if not candidates:
            log.info("scanner: 0 candidates from gamma")
            self._cached = []
            return

        # Fetch books for ranking — capped so we don't blast the API.
        cap = max(self._scanner_cfg.max_concurrent_markets * 3, 30)
        subset = candidates[:cap]
        books: Dict[str, OrderBook] = {}
        for m in subset:
            try:
                books[m.token_id] = self._clob.get_order_book(m.token_id)
            except Exception as e:
                log.debug("book fetch failed %s: %s", m.token_id[:10], e)

        # Drop near-certainty markets (mid pinned near 0 or 1). They have
        # stale liquidity but no active flow, so passive quotes never fill.
        lo = self._scanner_cfg.min_mid_price
        hi = self._scanner_cfg.max_mid_price
        max_move = self._scanner_cfg.max_recent_move_bps
        lookback = self._scanner_cfg.recent_move_lookback_sec
        dropped_extreme = 0
        dropped_jumpy = 0
        filtered: list[TokenMarket] = []
        for m in subset:
            book = books.get(m.token_id)
            if book is None or book.midpoint is None:
                filtered.append(m)  # keep — unknown, let optimizer decide
                continue
            if book.midpoint < lo or book.midpoint > hi:
                dropped_extreme += 1
                continue
            # Volatility gate: drop markets that just moved too fast.
            if self._vol is not None:
                move = self._vol.midpoint_delta_bps(
                    m.token_id, lookback_sec=lookback,
                )
                if move is not None and move >= max_move:
                    dropped_jumpy += 1
                    continue
            filtered.append(m)

        ranked = rank_markets_by_reward(filtered, books, self._mm_cfg)
        self._cached = [m for m, _ in ranked[: self._scanner_cfg.max_concurrent_markets]]
        log.info(
            "scanner: %d candidates → %d after filters → %d active "
            "(dropped %d near-certainty, %d jumpy; first: %s)",
            len(candidates),
            len(filtered),
            len(self._cached),
            dropped_extreme,
            dropped_jumpy,
            self._cached[0].question[:60] if self._cached else "-",
        )
