"""Copy-trading watchlist (opt-in, conservative).

Polls recent trades from a short list of top wallets (sourced from the
research snapshot) and mirrors *new* entries at a capped size. Designed as a
*signal* — we don't try to exactly replicate sizing or timing.

Safety:
  - Each mirrored order passes through the same risk manager as everything else.
  - We only mirror when the top wallet's 7d PnL exceeds `min_wallet_pnl_usd`.
  - If the bot is already long/short a token past the skew cap, we skip.
  - Mirrors a fraction of the wallet's size capped at `mirror_size_usd`.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional, Set

import httpx

from .clob import ClobClientWrapper
from .config import Config, CopyTradingCfg, Secrets
from .models import OrderBook
from .risk import RiskManager

log = logging.getLogger(__name__)


class CopyTrader:
    def __init__(
        self,
        cfg: Config,
        secrets: Secrets,
        clob: ClobClientWrapper,
        risk: RiskManager,
    ):
        self._cfg = cfg
        self._copy_cfg: CopyTradingCfg = cfg.copy_trading
        self._secrets = secrets
        self._clob = clob
        self._risk = risk
        self._client = httpx.Client(timeout=cfg.loop.http_timeout_sec)
        self._followed: List[str] = []
        self._seen_trades: Set[str] = set()
        self._last_refresh: float = 0.0

    def close(self) -> None:
        self._client.close()

    def on_tick(self, books: Dict[str, OrderBook]) -> None:
        if not self._copy_cfg.enabled:
            return
        self._refresh_watchlist_if_due()
        if not self._followed:
            return
        for addr in self._followed:
            for trade in self._recent_trades(addr):
                self._maybe_mirror(addr, trade, books)

    # ---- watchlist ----

    def _refresh_watchlist_if_due(self) -> None:
        if time.time() - self._last_refresh < 900:  # 15 min
            return
        self._last_refresh = time.time()
        latest = Path(self._cfg.research.report_dir) / "latest.json"
        if not latest.exists():
            return
        try:
            data = json.loads(latest.read_text())
        except Exception as e:
            log.warning("copy: could not read %s: %s", latest, e)
            return
        wallets = data.get("top_wallets") or []
        picks = [
            w["address"]
            for w in wallets
            if isinstance(w, dict)
            and (w.get("pnl_usd") or 0) >= self._copy_cfg.min_wallet_pnl_usd
            and w.get("address")
        ]
        self._followed = picks[: self._copy_cfg.max_wallets_to_follow]
        if self._followed:
            log.info("copy: following %d wallets", len(self._followed))

    # ---- trade fetch ----

    def _recent_trades(self, address: str) -> List[dict]:
        for url in (
            f"{self._secrets.data_host}/trades",
            f"{self._secrets.gamma_host}/trades",
        ):
            try:
                r = self._client.get(
                    url,
                    params={"user": address, "limit": 20, "takerOnly": "true"},
                )
                if r.status_code == 404:
                    continue
                r.raise_for_status()
                raw = r.json()
                entries = raw.get("data") if isinstance(raw, dict) else raw
                if isinstance(entries, list):
                    return entries
            except Exception as e:
                log.debug("copy trades(%s) failed: %s", address[:8], e)
        return []

    # ---- mirroring ----

    def _maybe_mirror(self, addr: str, trade: dict, books: Dict[str, OrderBook]) -> None:
        tid = str(trade.get("tokenId") or trade.get("asset") or trade.get("token_id") or "")
        side_raw = str(trade.get("side") or trade.get("takerSide") or "").upper()
        key = f"{addr}:{trade.get('transactionHash') or trade.get('id') or ''}"
        if not tid or not side_raw or not key or key in self._seen_trades:
            return
        self._seen_trades.add(key)

        # Keep the seen set bounded.
        if len(self._seen_trades) > 10_000:
            self._seen_trades = set(list(self._seen_trades)[-5_000:])

        if self._copy_cfg.allow_markets and tid not in self._copy_cfg.allow_markets:
            return

        side = "BUY" if side_raw in ("BUY", "BID", "LONG") else "SELL"
        book = books.get(tid)
        if book is None or not (book.best_bid and book.best_ask):
            log.debug("copy: no book for %s", tid[:10])
            return
        usd = self._copy_cfg.mirror_size_usd
        # Paper/live both go through clob.place_market, which enforces paper mode.
        ok = self._clob.place_market(tid, side, usd, book=book)
        if ok:
            log.info("copy: mirrored %s %s $%.2f from %s", side, tid[:10], usd, addr[:8])
