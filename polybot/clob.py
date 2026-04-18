"""Thin wrapper over py-clob-client with normalized models, retries, and a
deterministic dry-run mode."""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass
from typing import List, Optional

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .models import BookLevel, OrderBook, Quote

log = logging.getLogger(__name__)


@dataclass
class PlacedOrder:
    order_id: str
    quote: Quote
    created_at: float


class ClobClientWrapper:
    """Wraps py_clob_client.ClobClient. If `dry_run=True`, no network writes are
    made — orders are only logged, useful for strategy validation."""

    def __init__(
        self,
        host: str,
        private_key: str,
        funder: str,
        chain_id: int = 137,
        signature_type: int = 1,
        dry_run: bool = False,
    ):
        self.dry_run = dry_run
        self._host = host
        self._chain_id = chain_id
        self._private_key = private_key
        self._funder = funder
        self._signature_type = signature_type
        self._client = None  # lazy — so dry-run users don't need a real key
        if not dry_run:
            self._connect()

    def _connect(self) -> None:
        from py_clob_client.client import ClobClient  # lazy import

        self._client = ClobClient(
            self._host,
            key=self._private_key,
            chain_id=self._chain_id,
            signature_type=self._signature_type,
            funder=self._funder,
        )
        self._client.set_api_creds(self._client.create_or_derive_api_creds())
        log.info("CLOB client authenticated as funder=%s", self._funder)

    # ---- read ----

    @retry(
        reraise=True,
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        retry=retry_if_exception_type(Exception),
    )
    def get_order_book(self, token_id: str) -> OrderBook:
        if self.dry_run and self._client is None:
            # Without credentials we can still hit the public endpoint via
            # py_clob_client. Try once; if it fails, return empty book.
            try:
                from py_clob_client.client import ClobClient
                self._client = ClobClient(self._host, chain_id=self._chain_id)
            except Exception as e:
                log.debug("dry-run: could not connect unauth CLOB: %s", e)
                return OrderBook(token_id=token_id, timestamp_ms=int(time.time() * 1000))
        raw = self._client.get_order_book(token_id)
        return _normalize_book(token_id, raw)

    def get_midpoint(self, token_id: str) -> Optional[float]:
        try:
            r = self._client.get_midpoint(token_id)
            if isinstance(r, dict):
                return float(r.get("mid") or r.get("midpoint") or 0) or None
            return float(r)
        except Exception as e:  # pragma: no cover — network
            log.debug("get_midpoint(%s) failed: %s", token_id, e)
            return None

    # ---- write ----

    def place_limit(self, quote: Quote, gtc: bool = True) -> Optional[PlacedOrder]:
        """Create and post a GTC limit order. Returns `None` on failure."""
        if self.dry_run:
            log.info(
                "[DRY] limit %s %s %.4f @ %.4f size=%.2f",
                quote.side, quote.token_id[:10], quote.price, quote.price, quote.size,
            )
            return PlacedOrder(
                order_id=f"dry-{uuid.uuid4()}", quote=quote, created_at=time.time()
            )
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

    def place_market(self, token_id: str, side: str, usd_amount: float) -> bool:
        """Fire an IOC/FOK taker order. Returns True if accepted."""
        if self.dry_run:
            log.info("[DRY] market %s %s $%.2f", side, token_id[:10], usd_amount)
            return True
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
            log.debug("[DRY] cancel %s", order_id)
            return True
        try:
            self._client.cancel(order_id)
            return True
        except Exception as e:
            log.warning("cancel(%s) failed: %s", order_id, e)
            return False

    def cancel_all(self) -> None:
        if self.dry_run:
            return
        try:
            self._client.cancel_all()
        except Exception as e:
            log.warning("cancel_all failed: %s", e)

    def get_open_orders(self) -> list:
        if self.dry_run or self._client is None:
            return []
        try:
            from py_clob_client.clob_types import OpenOrderParams
            return self._client.get_orders(OpenOrderParams()) or []
        except Exception as e:
            log.debug("get_orders failed: %s", e)
            return []


def _normalize_book(token_id: str, raw) -> OrderBook:
    """py-clob-client returns an OrderBookSummary object. Handle both object
    and plain-dict shapes."""

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
