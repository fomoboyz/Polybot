"""Low-latency Polymarket WebSocket feed.

The CLOB exposes a public market channel at
`wss://ws-subscriptions-clob.polymarket.com/ws/market` that streams `book`,
`price_change`, `last_trade_price`, and `best_bid_ask` events.

Design:
  - Runs in a dedicated background thread with its own asyncio loop.
  - Maintains an in-memory map `token_id -> OrderBook` that the main loop
    reads lock-free (dict assignment is atomic in CPython).
  - Tolerates disconnects with exponential backoff.
  - Subscriptions can be updated; a change triggers a reconnect.
  - Emits `book.timestamp_ms` from the event when available, else wall clock.
  - PINGs every `ping_interval_sec` to satisfy Polymarket's 10s ping policy.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Dict, Iterable, Optional, Set

try:
    import websockets  # type: ignore
except ImportError:  # pragma: no cover — optional runtime dep
    websockets = None  # type: ignore

from .models import BookLevel, OrderBook

log = logging.getLogger(__name__)

DEFAULT_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


class WebSocketFeed:
    def __init__(
        self,
        url: str = DEFAULT_URL,
        ping_interval_sec: float = 10.0,
        backoff_max_sec: float = 30.0,
    ):
        self._url = url
        self._ping_interval = ping_interval_sec
        self._backoff_max = backoff_max_sec

        self._books: Dict[str, OrderBook] = {}
        self._tokens: Set[str] = set()
        self._tokens_version = 0
        self._lock = threading.Lock()

        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event: Optional[asyncio.Event] = None
        self._restart_event: Optional[asyncio.Event] = None
        self._stopped = threading.Event()

        self.connected = False
        self.last_message_ts: float = 0.0
        self.disconnect_count = 0

    # ---- public API ----

    def start(self) -> None:
        if websockets is None:
            log.warning("websockets package missing — WS feed disabled")
            return
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="polybot-ws", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        if self._loop is not None and self._stop_event is not None:
            self._loop.call_soon_threadsafe(self._stop_event.set)
        if self._thread is not None:
            self._thread.join(timeout=5)

    def set_tokens(self, tokens: Iterable[str]) -> None:
        new = {t for t in tokens if t}
        with self._lock:
            if new == self._tokens:
                return
            self._tokens = new
            self._tokens_version += 1
            # Drop stale book entries.
            for k in list(self._books.keys()):
                if k not in new:
                    self._books.pop(k, None)
        if self._loop is not None and self._restart_event is not None:
            self._loop.call_soon_threadsafe(self._restart_event.set)

    def get(self, token_id: str) -> Optional[OrderBook]:
        return self._books.get(token_id)

    def age_sec(self) -> Optional[float]:
        if self.last_message_ts == 0:
            return None
        return time.time() - self.last_message_ts

    # ---- background loop ----

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._stop_event = asyncio.Event()
        self._restart_event = asyncio.Event()
        try:
            self._loop.run_until_complete(self._outer())
        except Exception as e:  # pragma: no cover
            log.exception("ws thread died: %s", e)
        finally:
            self._loop.close()

    async def _outer(self) -> None:
        backoff = 1.0
        while not self._stop_event.is_set():
            with self._lock:
                tokens = list(self._tokens)
                version = self._tokens_version
            if not tokens:
                try:
                    await asyncio.wait_for(self._restart_event.wait(), timeout=5)
                except asyncio.TimeoutError:
                    pass
                self._restart_event.clear()
                continue

            try:
                await self._connect_and_stream(tokens, version)
                backoff = 1.0
            except Exception as e:
                self.connected = False
                self.disconnect_count += 1
                log.warning("ws disconnect (%s): %s — reconnecting in %.1fs", type(e).__name__, e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(self._backoff_max, backoff * 2)

    async def _connect_and_stream(self, tokens: list, version: int) -> None:
        assert websockets is not None
        async with websockets.connect(  # type: ignore[attr-defined]
            self._url,
            ping_interval=self._ping_interval,
            ping_timeout=self._ping_interval * 2,
            close_timeout=2,
            max_size=2**22,
        ) as ws:
            await ws.send(json.dumps({
                "assets_ids": tokens,
                "type": "market",
                "custom_feature_enabled": True,
            }))
            self.connected = True
            log.info("ws connected, subscribed %d tokens", len(tokens))

            async def _receiver():
                async for raw in ws:
                    self.last_message_ts = time.time()
                    try:
                        self._handle_raw(raw)
                    except Exception as e:
                        log.debug("ws message parse error: %s", e)

            recv_task = asyncio.create_task(_receiver())
            try:
                # Block until we either need to stop, need to restart, or recv
                # errors out.
                stop_task = asyncio.create_task(self._stop_event.wait())
                restart_task = asyncio.create_task(self._restart_event.wait())
                done, pending = await asyncio.wait(
                    [recv_task, stop_task, restart_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                if self._restart_event.is_set() and version != self._tokens_version:
                    self._restart_event.clear()
            finally:
                if not recv_task.done():
                    recv_task.cancel()

    def _handle_raw(self, raw) -> None:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="ignore")
        if not raw or raw == "PONG":
            return
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return
        if isinstance(payload, list):
            for evt in payload:
                self._apply_event(evt)
        elif isinstance(payload, dict):
            self._apply_event(payload)

    def _apply_event(self, evt: dict) -> None:
        etype = evt.get("event_type") or evt.get("type")
        if etype == "book":
            token = evt.get("asset_id") or evt.get("market")
            if not token:
                return
            bids = _parse_levels(evt.get("bids") or [], descending=True)
            asks = _parse_levels(evt.get("asks") or [], descending=False)
            ts = _parse_ts(evt.get("timestamp"))
            self._books[token] = OrderBook(
                token_id=str(token), bids=bids, asks=asks, timestamp_ms=ts,
            )
        elif etype == "price_change":
            # Partial update — merge into the existing book.
            token = evt.get("asset_id") or evt.get("market")
            if not token:
                return
            book = self._books.get(str(token))
            if book is None:
                return
            for change in evt.get("changes") or []:
                price = float(change.get("price", 0) or 0)
                size = float(change.get("size", 0) or 0)
                side = str(change.get("side", "")).lower()
                if price <= 0:
                    continue
                target = book.bids if side in ("buy", "bid") else book.asks
                _apply_change(target, price, size, descending=(target is book.bids))
            book.timestamp_ms = _parse_ts(evt.get("timestamp"))
        elif etype == "best_bid_ask":
            token = evt.get("asset_id") or evt.get("market")
            if not token:
                return
            book = self._books.get(str(token)) or OrderBook(
                token_id=str(token), bids=[], asks=[], timestamp_ms=0,
            )
            bb = evt.get("best_bid")
            ba = evt.get("best_ask")
            if bb:
                book.bids = [BookLevel(price=float(bb.get("price", 0)), size=float(bb.get("size", 0)))]
            if ba:
                book.asks = [BookLevel(price=float(ba.get("price", 0)), size=float(ba.get("size", 0)))]
            book.timestamp_ms = _parse_ts(evt.get("timestamp"))
            self._books[str(token)] = book
        # last_trade_price / market_resolved / new_market — no book impact for now


def _parse_levels(levels, descending: bool):
    out = []
    for lvl in levels or []:
        if isinstance(lvl, dict):
            p = lvl.get("price")
            s = lvl.get("size")
        else:
            continue
        try:
            out.append(BookLevel(price=float(p), size=float(s)))
        except (TypeError, ValueError):
            continue
    out.sort(key=lambda x: x.price, reverse=descending)
    return out


def _parse_ts(raw) -> int:
    if raw is None:
        return int(time.time() * 1000)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return int(time.time() * 1000)


def _apply_change(levels, price: float, size: float, descending: bool) -> None:
    for i, lvl in enumerate(levels):
        if abs(lvl.price - price) < 1e-9:
            if size == 0:
                levels.pop(i)
            else:
                lvl.size = size
            return
    if size > 0:
        levels.append(BookLevel(price=price, size=size))
        levels.sort(key=lambda x: x.price, reverse=descending)
