"""Shared domain models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Literal, Optional

Side = Literal["BUY", "SELL"]


@dataclass
class TokenMarket:
    """One outcome token within a Polymarket binary market."""

    token_id: str
    outcome: str  # "Yes" or "No"
    condition_id: str
    question: str
    end_date_iso: Optional[str]
    min_order_size: float = 5.0
    tick_size: float = 0.001
    liquidity_usd: float = 0.0
    volume_24h_usd: float = 0.0
    rewards_enabled: bool = False
    # The sibling token (the opposite outcome's token_id) — populated after
    # discovery so arbitrage can be done with O(1) lookup.
    sibling_token_id: Optional[str] = None


@dataclass
class BookLevel:
    price: float
    size: float


@dataclass
class OrderBook:
    token_id: str
    bids: List[BookLevel] = field(default_factory=list)  # descending price
    asks: List[BookLevel] = field(default_factory=list)  # ascending price
    timestamp_ms: int = 0

    @property
    def best_bid(self) -> Optional[BookLevel]:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> Optional[BookLevel]:
        return self.asks[0] if self.asks else None

    @property
    def midpoint(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return (self.best_bid.price + self.best_ask.price) / 2.0
        return None

    @property
    def spread(self) -> Optional[float]:
        if self.best_bid and self.best_ask:
            return self.best_ask.price - self.best_bid.price
        return None


@dataclass
class Quote:
    token_id: str
    side: Side
    price: float
    size: float  # in shares (size * price = USD)
    slot: int = 0  # ladder level — 0 = tightest, higher = wider


@dataclass
class Position:
    token_id: str
    net_shares: float = 0.0  # positive = long YES-token
    avg_cost: float = 0.0
    realized_pnl: float = 0.0

    def apply_fill(self, side: Side, shares: float, price: float) -> None:
        signed = shares if side == "BUY" else -shares
        new_net = self.net_shares + signed
        if self.net_shares == 0 or (self.net_shares > 0) == (signed > 0):
            # Opening or adding to a position — update avg cost.
            if new_net != 0:
                self.avg_cost = (
                    self.avg_cost * self.net_shares + price * signed
                ) / new_net
        else:
            # Closing/flipping — realize PnL on the closed portion.
            closed = min(abs(self.net_shares), abs(signed))
            if self.net_shares > 0:
                self.realized_pnl += closed * (price - self.avg_cost)
            else:
                self.realized_pnl += closed * (self.avg_cost - price)
            if abs(signed) > abs(self.net_shares):
                # Flipped — reset avg cost to the remainder.
                self.avg_cost = price
        self.net_shares = new_net
