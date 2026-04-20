"""Paper-trading broker.

Simulates order placement and fills against real live market data. The bot
pulls real order books from Polymarket's public endpoints, and this module
decides which of our simulated resting orders would have been filled.

Fill model (two paths):
  1. "Cross" fill — the real best-ask drops to or below our resting bid
     (or best-bid rises to our ask). This is the unambiguous case: a
     real taker actually came in at our price. Applies a 50% queue
     discount because in reality we might not be first in line.

  2. "Inside-spread" fill — our resting bid is strictly higher than the
     real best-bid, so on the live exchange we'd be the best bid and
     incoming market-sell takers would hit us first. We can't observe
     those hits directly in paper mode, so we simulate a Poisson arrival
     process with rate proportional to the market's 24h volume. This is
     the common case for a maker posting inside the spread, and without
     it paper fills are vanishingly rare.

The 2% Polymarket maker fee is NOT applied (maker fees are 0); taker
fees only apply to IOC/FOK market orders.
"""

from __future__ import annotations

import logging
import math
import random
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from .models import OrderBook, Quote, TokenMarket

log = logging.getLogger(__name__)


@dataclass
class SimOrder:
    order_id: str
    quote: Quote
    placed_at: float
    remaining: float  # unfilled size


@dataclass
class SimFill:
    order_id: str
    token_id: str
    side: str
    price: float
    shares: float
    filled_at: float


FillCallback = Callable[[SimFill], None]

# Calibration: a market with $10k/24h volume should produce roughly one
# maker fill every 2 minutes on one side when we're sitting at the best
# price. That maps to a per-sec arrival rate of 1/120 at $10k volume.
_FILL_RATE_PER_USD_PER_SEC = 1.0 / (120.0 * 10_000.0)
# Floor so even low-volume markets produce occasional fills.
_MIN_FILL_RATE_PER_SEC = 1.0 / 1800.0  # ~once per 30 min


class PaperBroker:
    """In-memory simulated broker. Thread-unsafe; the bot loop is single-threaded."""

    def __init__(self, fill_probability: float = 0.5, seed: Optional[int] = None):
        self._orders: Dict[str, SimOrder] = {}
        self._fill_probability = fill_probability
        self._rng = random.Random(seed)
        self._last_match_ts: Optional[float] = None

    # ---- placement ----

    def place_limit(self, quote: Quote) -> Optional[str]:
        oid = f"paper-{uuid.uuid4()}"
        self._orders[oid] = SimOrder(
            order_id=oid,
            quote=quote,
            placed_at=time.time(),
            remaining=quote.size,
        )
        return oid

    def cancel(self, order_id: str) -> bool:
        return self._orders.pop(order_id, None) is not None

    def cancel_all(self) -> None:
        self._orders.clear()

    def cancel_all_for(self, token_id: str) -> None:
        for oid in list(self._orders.keys()):
            if self._orders[oid].quote.token_id == token_id:
                del self._orders[oid]

    # ---- market orders simulate as instant fills at top of book ----

    def place_market(self, token_id: str, side: str, usd_amount: float, book: OrderBook) -> Optional[SimFill]:
        now = time.time()
        if side == "BUY":
            if not book.best_ask:
                return None
            price = book.best_ask.price
        else:
            if not book.best_bid:
                return None
            price = book.best_bid.price
        shares = usd_amount / max(price, 1e-6)
        return SimFill(
            order_id=f"paper-mkt-{uuid.uuid4()}",
            token_id=token_id,
            side=side,
            price=price,
            shares=shares,
            filled_at=now,
        )

    # ---- fill matching ----

    def match_fills(
        self,
        books: Dict[str, OrderBook],
        markets: Optional[Dict[str, TokenMarket]] = None,
    ) -> List[SimFill]:
        """Return fills for resting orders. Pass `markets` to enable
        inside-spread fill simulation (needs 24h volume for rate scaling)."""
        fills: List[SimFill] = []
        now = time.time()
        dt = (now - self._last_match_ts) if self._last_match_ts else 0.0
        self._last_match_ts = now

        for oid, order in list(self._orders.items()):
            book = books.get(order.quote.token_id)
            if book is None or not book.best_bid or not book.best_ask:
                continue

            side = order.quote.side
            price = order.quote.price
            filled = False
            fill_price = price
            matched_level_size = 0.0

            # Path 1: book crossed our price (unambiguous — a real taker
            # printed at or inside our level).
            if side == "BUY" and book.best_ask.price <= price:
                if self._rng.random() < self._fill_probability:
                    filled = True
                    matched_level_size = book.best_ask.size
            elif side == "SELL" and book.best_bid.price >= price:
                if self._rng.random() < self._fill_probability:
                    filled = True
                    matched_level_size = book.best_bid.size

            # Path 2: inside-spread. We'd be best-of-book and taker flow
            # would hit us. Only runs if `markets` passed (to get volume).
            if not filled and markets is not None and dt > 0:
                mkt = markets.get(order.quote.token_id)
                inside_buy = side == "BUY" and price > book.best_bid.price
                inside_sell = side == "SELL" and price < book.best_ask.price
                if mkt is not None and (inside_buy or inside_sell):
                    vol = max(mkt.volume_24h_usd, 0.0)
                    rate = max(vol * _FILL_RATE_PER_USD_PER_SEC, _MIN_FILL_RATE_PER_SEC)
                    # Scale by how deep inside the spread we are — quoting
                    # right at the far side of the spread earns fastest.
                    spread = max(book.best_ask.price - book.best_bid.price, 1e-6)
                    if inside_buy:
                        depth = (price - book.best_bid.price) / spread
                    else:
                        depth = (book.best_ask.price - price) / spread
                    depth = max(0.0, min(1.0, depth))
                    p = 1.0 - math.exp(-rate * dt * (0.25 + 0.75 * depth))
                    if self._rng.random() < p:
                        filled = True
                        # Fill at our quoted price, capped to live mid-bounded
                        # size (assume whatever liquidity was on the other
                        # side is what a typical taker would clear).
                        matched_level_size = (
                            book.best_ask.size if side == "BUY" else book.best_bid.size
                        )

            if not filled:
                continue

            shares_available = min(order.remaining, matched_level_size)
            if shares_available <= 0:
                continue

            fills.append(SimFill(
                order_id=oid,
                token_id=order.quote.token_id,
                side=side,
                price=fill_price,
                shares=shares_available,
                filled_at=now,
            ))
            order.remaining -= shares_available
            if order.remaining <= 1e-9:
                del self._orders[oid]
        return fills

    def open_orders(self) -> List[SimOrder]:
        return list(self._orders.values())
