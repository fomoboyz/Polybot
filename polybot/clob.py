"""Thin wrapper over py-clob-client with normalized models, retries, and
three execution modes:

  - live:     real orders on real money (requires funded wallet + API creds)
  - paper:    real live market data, simulated fills via `PaperBroker`
  - dry_run:  decisions logged only; no orders tracked (useful for staring at
              strategy behavior without any accounting overhead)
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .models import BookLevel, OrderBook, Quote
from .paper import PaperBroker, SimFill

log = logging.getLogger(__name__)


@dataclass
class PlacedOrder:
    order_id: str
    quote: Quote
    created_at: float


class ClobClientWrapper:
    def __init__(
        self,
        host: str,
        private_key: str,
        funder: str,
        chain_id: int = 137,
        signature_type: int = 1,
        dry_run: bool = False,
        paper: bool = False,
    ):
        if dry_run and paper:
            raise ValueError("dry_run and paper are mutually exclusive")
        self.dry_run = dry_run
        self.paper = paper
        self._host = host
        self._chain_id = chain_id
        self._private_key = private_key
        self._funder = funder
        self._signature_type = signature_type
        self._client = None
        self._paper_broker: Optional[PaperBroker] = PaperBroker() if paper else None

        if not dry_run and not paper:
            self._connect_authed()
        else:
            # Even in paper/dry_run we want read access to real books.
            self._connect_public()

    @property
    def paper_broker(self) -> Optional[PaperBroker]:
        return self._paper_broker

    # ---- connection ----

    def _connect_authed(self) -> None:
        from py_clob_client.client import ClobClient  # lazy

        self._client = ClobClient(
            self._host,
            key=self._private_key,
            chain_id=self._chain_id,
            signature_type=self._signature_type,
            funder=self._funder,
        )
        self._client.set_api_creds(self._client.create_or_derive_api_creds())
        log.info("CLOB client authenticated as funder=%s", self._funder)

    def _connect_public(self) -> None:
        try:
            from py_clob_client.client import ClobClient
            self._client = ClobClient(self._host, chain_id=self._chain_id)
        except Exception as e:
            log.warning("public CLOB connect failed (read ops will fail): %s", e)
            self._client = None

    # ---- read ----

    @retry(
        reraise=True,
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        retry=retry_if_exception_type(Exception),
    )
    def get_order_book(self, token_id: str) -> OrderBook:
        if self._client is None:
            return OrderBook(token_id=token_id, timestamp_ms=int(time.time() * 1000))
        raw = self._client.get_order_book(token_id)
        return _normalize_book(token_id, raw)

    def get_midpoint(self, token_id: str) -> Optional[float]:
        if self._client is None:
            return None
        try:
            r = self._client.get_midpoint(token_id)
            if isinstance(r, dict):
                return float(r.get("mid") or r.get("midpoint") or 0) or None
            return float(r)
        except Exception as e:  # pragma: no cover
            log.debug("get_midpoint(%s) failed: %s", token_id, e)
            return None

    # ---- paper-mode fill drain ----

    def drain_paper_fills(self, books: Dict[str, OrderBook]) -> List[SimFill]:
        if self._paper_broker is None:
            return []
        return self._paper_broker.match_fills(books)

    # ---- write ----

    def place_limit(self, quote: Quote, gtc: bool = True) -> Optional[PlacedOrder]:
        if self.dry_run:
            log.info(
                "[DRY] limit %s %s %.4f size=%.2f",
                quote.side, quote.token_id[:10], quote.price, quote.size,
            )
            return PlacedOrder(
                order_id=f"dry-{uuid.uuid4()}", quote=quote, created_at=time.time()
            )
        if self.paper and self._paper_broker is not None:
            oid = self._paper_broker.place_limit(quote)
            if oid is None:
                return None
            return PlacedOrder(order_id=oid, quote=quote, created_at=time.time())

        # Live path.
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL

        side = BUY if quote.side == "BUY" else SELL
        args = OrderArgs(
            token_id=quote.token_id,
            price=round(quote.price, 4),
            size=round(quote.size, 4),
            side=side,
        )
        try:
            signed = self._client.create_order(args)
            resp = self._client.post_order(
                signed, OrderType.GTC if gtc else OrderType.GTD
            )
            oid = (resp or {}).get("orderID") or (resp or {}).get("orderId")
            if not oid:
                log.warning("post_order returned no id: %s", resp)
                return None
            return PlacedOrder(order_id=oid, quote=quote, created_at=time.time())
        except Exception as e:
            log.warning("place_limit failed (%s %s): %s", quote.side, quote.token_id[:10], e)
            return None

    def place_market(
        self,
        token_id: str,
        side: str,
        usd_amount: float,
        book: Optional[OrderBook] = None,
    ) -> bool:
        if self.dry_run:
            log.info("[DRY] market %s %s $%.2f", side, token_id[:10], usd_amount)
            return True
        if self.paper and self._paper_broker is not None:
            if book is None:
                return False
            fill = self._paper_broker.place_market(token_id, side, usd_amount, book)
            return fill is not None

        from py_clob_client.clob_types import MarketOrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL

        s = BUY if side == "BUY" else SELL
        args = MarketOrderArgs(
            token_id=token_id,
            amount=round(usd_amount, 2),
            side=s,
            order_type=OrderType.FOK,
        )
        try:
            signed = self._client.create_market_order(args)
            resp = self._client.post_order(signed, OrderType.FOK)
            return bool(resp and (resp.get("orderID") or resp.get("success")))
        except Exception as e:
            log.warning("place_market failed: %s", e)
            return False

    def cancel(self, order_id: str) -> bool:
        if self.dry_run:
            return True
        if self.paper and self._paper_broker is not None:
            return self._paper_broker.cancel(order_id)
        try:
            self._client.cancel(order_id)
            return True
        except Exception as e:
            log.warning("cancel(%s) failed: %s", order_id, e)
            return False

    def cancel_all(self) -> None:
        if self.dry_run:
            return
        if self.paper and self._paper_broker is not None:
            self._paper_broker.cancel_all()
            return
        try:
            self._client.cancel_all()
        except Exception as e:
            log.warning("cancel_all failed: %s", e)

    def get_open_orders(self) -> list:
        if self.dry_run or self._client is None:
            return []
        if self.paper and self._paper_broker is not None:
            return [
                {"id": o.order_id, "token_id": o.quote.token_id}
                for o in self._paper_broker.open_orders()
            ]
        try:
            from py_clob_client.clob_types import OpenOrderParams
            return self._client.get_orders(OpenOrderParams()) or []
        except Exception as e:
            log.debug("get_orders failed: %s", e)
            return []


def _normalize_book(token_id: str, raw) -> OrderBook:
    def _levels(src, descending: bool) -> List[BookLevel]:
        levels = []
        for lvl in src or []:
            price = float(getattr(lvl, "price", None) or lvl["price"])
            size = float(getattr(lvl, "size", None) or lvl["size"])
            levels.append(BookLevel(price=price, size=size))
        levels.sort(key=lambda x: x.price, reverse=descending)
        return levels

    bids = _levels(getattr(raw, "bids", None) or raw.get("bids"), descending=True)
    asks = _levels(getattr(raw, "asks", None) or raw.get("asks"), descending=False)
    ts = getattr(raw, "timestamp", None) or raw.get("timestamp") or int(time.time() * 1000)
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        ts = int(time.time() * 1000)
    return OrderBook(token_id=token_id, bids=bids, asks=asks, timestamp_ms=ts)
