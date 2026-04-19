"""Laddered quote generation.

One bid/ask at the target spread is suboptimal:
  - Reward scoring is quadratic, so multiple small orders near the mid
    accumulate reward faster than one big order placed wider.
  - Distributing size across 3–5 levels reduces adverse-selection risk —
    if the market walks through our top level we still have depth behind.

`ladder_quotes(pair, cfg, ...)` expands a single `QuotePair` into a list
of `Quote`s. Each subsequent level steps `step_bps` wider and carries
`size_decay` × previous size.
"""

from __future__ import annotations

from typing import List

from .config import LadderCfg, MarketMakerCfg
from .models import Quote, TokenMarket
from .reward_optimizer import QuotePair


def ladder_quotes(
    market: TokenMarket,
    pair: QuotePair,
    mm_cfg: MarketMakerCfg,
    ladder_cfg: LadderCfg,
    base_size_shares: float,
) -> List[Quote]:
    """Expand a single bid/ask into a ladder."""
    tick = market.tick_size or mm_cfg.tick_size
    levels = max(1, int(ladder_cfg.levels))
    step = ladder_cfg.step_bps / 10000.0
    decay = max(0.0, min(1.5, ladder_cfg.size_decay))

    out: List[Quote] = []
    size = base_size_shares
    for i in range(levels):
        offset = i * step
        bid_price = _snap(pair.bid - offset, tick)
        ask_price = _snap(pair.ask + offset, tick)
        if bid_price <= tick or ask_price >= 1 - tick:
            break
        sz = max(market.min_order_size, size)
        out.append(Quote(
            token_id=market.token_id, side="BUY",
            price=bid_price, size=sz, slot=i,
        ))
        out.append(Quote(
            token_id=market.token_id, side="SELL",
            price=ask_price, size=sz, slot=i,
        ))
        size *= decay
    return out


def _snap(price: float, tick: float) -> float:
    return round(round(price / tick) * tick, 6)
