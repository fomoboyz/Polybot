"""Paper-trading broker.

Simulates order placement and fills against real live market data. The bot
pulls real order books from Polymarket's public endpoints, and this module
decides which of our simulated resting orders would have been filled.

Fill model (simple but honest):
  - Our resting BUY at price P fills when the real best_ask drops to <= P
    (a taker crossed our bid).
  - Our resting SELL at price P fills when the real best_bid rises to >= P.
  - We apply a queue-position discount `fill_probability` (default 0.5)
    because in reality we wouldn't always be first in queue.
  - The 2% Polymarket maker fee is NOT applied (maker fees are 0); taker
    fees only apply to IOC/FOK market orders.
"""

from __future__ import annotations

import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .models import OrderBook, Quote

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


class PaperBroker:
    """In-memory simulated broker. Thread-unsafe; the bot loop is single-threaded."""

    def __init__(self, fill_probability: float = 0.5, seed: Optional[int] = None):
        self._orders: Dict[str, SimOrder] = {}
        self._fill_probability = fill_probability
        self._rng = random.Random(seed)

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

    def match_fills(self, books: Dict[str, OrderBook]) -> List[SimFill]:
        """Given fresh books, return fills for any resting orders that crossed."""
        fills: List[SimFill] = []
        now = time.time()

        for oid, order in list(self._orders.items()):
            book = books.get(order.quote.token_id)
            if book is None:
                continue

            filled = False
            if order.quote.side == "BUY" and book.best_ask and book.best_ask.price <= order.quote.price:
                filled = self._rng.random() < self._fill_probability
                fill_price = order.quote.price
            elif order.quote.side == "SELL" and book.best_bid and book.best_bid.price >= order.quote.price:
                filled = self._rng.random() < self._fill_probability
                fill_price = order.quote.price
            else:
                continue

            if not filled:
                continue

            shares_available = min(
                order.remaining,
                (book.best_ask.size if order.quote.side == "BUY" else book.best_bid.size),
            )
            if shares_available <= 0:
                continue

            fills.append(SimFill(
                order_id=oid,
                token_id=order.quote.token_id,
                side=order.quote.side,
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
