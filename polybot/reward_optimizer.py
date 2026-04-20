"""Reward-optimizing quote placement.

Layer 1 — maker reward maximization:
  The reward score is approximately
    score_per_order ≈ ((max_spread - distance_to_mid) / max_spread)^2 * size
  with a one-sided penalty factor c (≈3). Two-sided quotes near the midpoint
  dominate — quadratically — so we target the tightest spread allowed by our
  risk budget and step inside competitors when they're wider.

Layer 2 — Avellaneda-Stoikov inventory-aware pricing (optional):
  The reservation price shifts the mid by -γσ²q (q = net inventory in shares,
  σ = realized vol, γ = risk aversion). We quote symmetrically around that
  reservation price. When long, both bid and ask drop toward the market so we
  sell more and buy less; when short, the opposite.

Layer 3 — adaptive spread (optional):
  Half-spread scales linearly with σ up to a ceiling. Fast markets get wider
  quotes; quiet markets get tighter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from .config import MarketMakerCfg
from .models import OrderBook, TokenMarket


@dataclass
class QuotePair:
    bid: float
    ask: float
    spread: float
    expected_score: float
    reservation_price: float


def compute_quote_pair(
    market: TokenMarket,
    book: OrderBook,
    cfg: MarketMakerCfg,
    inventory_shares: float = 0.0,
    sigma: Optional[float] = None,
    size_usd: Optional[float] = None,
) -> Optional[QuotePair]:
    mid = book.midpoint
    if mid is None or mid <= 0 or mid >= 1:
        return None

    tick = market.tick_size or cfg.tick_size
    if size_usd is None:
        size_usd = cfg.quote_size_usd

    # Layer 2 — reservation price shifts the mid by inventory term.
    reservation = mid
    if cfg.use_inventory_skew and sigma is not None:
        # σ is per-tick stddev of mid deltas; inventory is in shares.
        # Clamp the shift so a wild σ doesn't blow up quotes.
        shift = -cfg.inventory_risk_aversion * inventory_shares * (sigma ** 2)
        shift = max(-cfg.max_skew_shift, min(cfg.max_skew_shift, shift))
        reservation = mid + shift
        # Keep reservation in-bounds.
        reservation = max(tick * 2, min(1.0 - tick * 2, reservation))

    # Layer 3 — adaptive half-spread.
    target_half = cfg.target_spread_bps / 10000.0
    floor_half = cfg.min_edge_over_mid_bps / 10000.0
    if cfg.use_adaptive_spread and sigma is not None:
        # Scale by sigma; cfg.sigma_to_spread_multiplier is how many half-spread
        # units we add per 1 unit of sigma (per 1.0 mid-move stddev).
        adaptive = sigma * cfg.sigma_to_spread_multiplier
        target_half = max(target_half, adaptive)
        # Cap at 2c (Polymarket's typical max reward-eligible spread).
        target_half = min(target_half, 0.02)

    half = max(target_half, floor_half)

    # If competitors are wider than target by the join threshold, step inside.
    join_trigger = cfg.join_if_wider_bps / 10000.0
    if book.best_bid and book.best_ask:
        competitor_half = (book.best_ask.price - book.best_bid.price) / 2.0
        if competitor_half > target_half + join_trigger:
            bid = _snap(book.best_bid.price + tick, tick)
            ask = _snap(book.best_ask.price - tick, tick)
            if (
                bid < reservation - floor_half
                and ask > reservation + floor_half
                and ask > bid
            ):
                return _package(bid, ask, reservation, mid, market, size_usd)

    bid = _snap(reservation - half, tick)
    ask = _snap(reservation + half, tick)
    bid = max(tick, bid)
    ask = min(1.0 - tick, ask)
    if ask <= bid:
        return None
    return _package(bid, ask, reservation, mid, market, size_usd)


def _package(
    bid: float,
    ask: float,
    reservation: float,
    mid: float,
    market: TokenMarket,
    size_usd: float,
) -> QuotePair:
    spread = ask - bid
    max_spread = 0.03
    per_side_dist = (ask - mid + mid - bid) / 2.0
    raw = max(0.0, (max_spread - per_side_dist)) / max_spread
    # Polymarket LP reward is quadratic in tightness AND linear in size.
    return QuotePair(
        bid=bid,
        ask=ask,
        spread=spread,
        expected_score=raw * raw * max(size_usd, 0.0),
        reservation_price=reservation,
    )


def _snap(price: float, tick: float) -> float:
    return round(round(price / tick) * tick, 6)


def rank_markets_by_reward(
    markets,
    books: Dict[str, OrderBook],
    cfg: MarketMakerCfg,
):
    """Rank markets by expected LP reward score × size × liquidity weight."""
    ranked: list[Tuple[TokenMarket, QuotePair]] = []
    for m in markets:
        book = books.get(m.token_id)
        if book is None:
            continue
        pair = compute_quote_pair(m, book, cfg, size_usd=cfg.quote_size_usd)
        if pair is None:
            continue
        weight = 1.0 + (m.liquidity_usd / 10000.0)
        ranked.append((m, QuotePair(
            bid=pair.bid,
            ask=pair.ask,
            spread=pair.spread,
            expected_score=pair.expected_score * weight,
            reservation_price=pair.reservation_price,
        )))
    ranked.sort(key=lambda x: x[1].expected_score, reverse=True)
    return ranked
