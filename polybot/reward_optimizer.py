"""Reward-optimizing quote placement.

Polymarket's maker reward scoring is approximately:

    score_per_order ≈ ((max_spread - order_spread) / max_spread)^2 * size

where `order_spread` is the distance from the size-cutoff-adjusted midpoint,
and `max_spread` is a market-specific cap (typically 3c). One-sided liquidity
is penalized by a factor `c` (≈3). Two-sided quotes near the midpoint
dominate — quadratically.

This module chooses the price pair (bid, ask) that:
  1. Respects a hard floor `min_edge_over_mid_bps`.
  2. Targets `target_spread_bps` unless competitors allow us to tighten
     profitably, in which case we step inside by 1 tick.
  3. Snaps to the market's tick size.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

from .config import MarketMakerCfg
from .models import OrderBook, TokenMarket


@dataclass
class QuotePair:
    bid: float
    ask: float
    spread: float
    expected_score: float  # unitless, relative — useful only for ranking


def compute_quote_pair(
    market: TokenMarket,
    book: OrderBook,
    cfg: MarketMakerCfg,
) -> Optional[QuotePair]:
    mid = book.midpoint
    if mid is None or mid <= 0 or mid >= 1:
        return None

    tick = market.tick_size or cfg.tick_size
    target_half = cfg.target_spread_bps / 10000.0
    floor_half = cfg.min_edge_over_mid_bps / 10000.0

    # Start from the target half-spread.
    half = max(target_half, floor_half)

    # If the current book is wider than target + join_if_wider, tighten to
    # `best_bid + tick` / `best_ask - tick`.
    join_trigger = cfg.join_if_wider_bps / 10000.0
    if book.best_bid and book.best_ask:
        competitor_half = (book.best_ask.price - book.best_bid.price) / 2.0
        if competitor_half > target_half + join_trigger:
            # Step inside by 1 tick on each side, but never cross the mid.
            bid = _snap(book.best_bid.price + tick, tick)
            ask = _snap(book.best_ask.price - tick, tick)
            if bid < mid - floor_half and ask > mid + floor_half and ask > bid:
                return _package(bid, ask, mid, market)

    bid = _snap(mid - half, tick)
    ask = _snap(mid + half, tick)
    # Keep both sides strictly inside (0, 1).
    bid = max(tick, bid)
    ask = min(1.0 - tick, ask)
    if ask <= bid:
        return None
    return _package(bid, ask, mid, market)


def _package(bid: float, ask: float, mid: float, market: TokenMarket) -> QuotePair:
    spread = ask - bid
    max_spread = 0.03  # 3c — the typical reward program cap
    per_side_dist = (ask - mid + mid - bid) / 2.0
    # Quadratic score in distance-to-mid. Pool size is market-dependent; we
    # only need relative ranking.
    raw = max(0.0, (max_spread - per_side_dist)) / max_spread
    expected_score = raw * raw
    return QuotePair(bid=bid, ask=ask, spread=spread, expected_score=expected_score)


def _snap(price: float, tick: float) -> float:
    return round(round(price / tick) * tick, 6)


def rank_markets_by_reward(
    markets: list[TokenMarket],
    books: dict[str, OrderBook],
    cfg: MarketMakerCfg,
) -> list[Tuple[TokenMarket, QuotePair]]:
    """Sort markets by expected reward score, descending. Used by the scanner
    when max_concurrent_markets is less than the candidate count."""
    ranked: list[Tuple[TokenMarket, QuotePair]] = []
    for m in markets:
        book = books.get(m.token_id)
        if book is None:
            continue
        pair = compute_quote_pair(m, book, cfg)
        if pair is None:
            continue
        # Weight by market liquidity (a proxy for reward pool share).
        weight = 1.0 + (m.liquidity_usd / 10000.0)
        ranked.append((m, QuotePair(
            bid=pair.bid,
            ask=pair.ask,
            spread=pair.spread,
            expected_score=pair.expected_score * weight,
        )))
    ranked.sort(key=lambda pair: pair[1].expected_score, reverse=True)
    return ranked
