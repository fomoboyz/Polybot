"""Rolling book-history recorder.

Snapshots the top-of-book for each tracked market at a configurable cadence
and writes them to SQLite (separate table from the main ledger). The
backtester replays these snapshots to simulate strategy performance
deterministically.

Storage is aggressively pruned — by default we keep `retention_hours` of
history to avoid unbounded disk growth.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Dict, Iterable, Optional

from sqlalchemy import Column, Float, Integer, String, create_engine, delete, select
from sqlalchemy.orm import DeclarativeBase, Session

from .config import HistoryCfg
from .models import BookLevel, OrderBook

log = logging.getLogger(__name__)


class _HistBase(DeclarativeBase):
    pass


class BookSnap(_HistBase):
    __tablename__ = "book_snapshots"
    id = Column(Integer, primary_key=True, autoincrement=True)
    ts = Column(Float, nullable=False, index=True)
    token_id = Column(String, nullable=False, index=True)
    best_bid = Column(Float)
    best_bid_size = Column(Float)
    best_ask = Column(Float)
    best_ask_size = Column(Float)
    levels_json = Column(String)  # top-5 each side for more detailed replays


class HistoryRecorder:
    def __init__(self, cfg: HistoryCfg):
        self._cfg = cfg
        self._engine = create_engine(f"sqlite:///{cfg.db_path}")
        _HistBase.metadata.create_all(self._engine)
        self._last_snap: Dict[str, float] = {}
        self._last_prune: float = 0.0

    def record(self, books: Dict[str, OrderBook]) -> None:
        if not self._cfg.enabled:
            return
        now = time.time()
        batch = []
        for token_id, book in books.items():
            last = self._last_snap.get(token_id, 0.0)
            if now - last < self._cfg.snapshot_interval_sec:
                continue
            if book.best_bid is None or book.best_ask is None:
                continue
            levels = {
                "bids": [(l.price, l.size) for l in book.bids[:5]],
                "asks": [(l.price, l.size) for l in book.asks[:5]],
            }
            batch.append(BookSnap(
                ts=now,
                token_id=token_id,
                best_bid=book.best_bid.price,
                best_bid_size=book.best_bid.size,
                best_ask=book.best_ask.price,
                best_ask_size=book.best_ask.size,
                levels_json=json.dumps(levels),
            ))
            self._last_snap[token_id] = now
        if not batch:
            return
        with Session(self._engine) as s:
            s.add_all(batch)
            s.commit()

        if now - self._last_prune > 3600:
            self._prune(now)
            self._last_prune = now

    def _prune(self, now: float) -> None:
        cutoff = now - self._cfg.retention_hours * 3600
        with Session(self._engine) as s:
            s.execute(delete(BookSnap).where(BookSnap.ts < cutoff))
            s.commit()

    # ---- replay interface ----

    def iter_snapshots(
        self,
        token_ids: Optional[Iterable[str]] = None,
        since: Optional[float] = None,
        until: Optional[float] = None,
    ):
        with Session(self._engine) as s:
            q = select(BookSnap)
            if token_ids is not None:
                q = q.where(BookSnap.token_id.in_(list(token_ids)))
            if since is not None:
                q = q.where(BookSnap.ts >= since)
            if until is not None:
                q = q.where(BookSnap.ts <= until)
            q = q.order_by(BookSnap.ts)
            for row in s.scalars(q):
                levels = json.loads(row.levels_json or "{}")
                yield row.ts, row.token_id, _to_book(row.token_id, row.ts, levels)


def _to_book(token_id: str, ts: float, levels: dict) -> OrderBook:
    return OrderBook(
        token_id=token_id,
        bids=[BookLevel(price=p, size=s) for p, s in (levels.get("bids") or [])],
        asks=[BookLevel(price=p, size=s) for p, s in (levels.get("asks") or [])],
        timestamp_ms=int(ts * 1000),
    )
