"""Strategy protocol."""

from __future__ import annotations

from typing import Protocol, Sequence

from ..models import OrderBook, TokenMarket


class Strategy(Protocol):
    name: str

    def on_tick(
        self,
        markets: Sequence[TokenMarket],
        books: dict[str, OrderBook],
    ) -> None:
        ...
