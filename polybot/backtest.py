"""Backtester — replays recorded book snapshots and simulates strategy PnL.

Unlike `--paper` (which runs against live markets), the backtester is
deterministic: it walks the saved snapshots in order, uses the same fill
model as the paper broker, and emits the same metrics. Useful for comparing
parameter sets on identical data.

Usage:
    python -m polybot backtest --hours 12 \
        --target-spread-bps 40 --quote-size-usd 10
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .config import Config
from .history import HistoryRecorder
from .models import OrderBook, Position, Quote, TokenMarket
from .paper import PaperBroker
from .reward_optimizer import compute_quote_pair
from .volatility import VolatilityTracker

log = logging.getLogger(__name__)


@dataclass
class BacktestResult:
    n_snapshots: int = 0
    n_tokens: int = 0
    orders_placed: int = 0
    fills: int = 0
    filled_volume_usd: float = 0.0
    realized_pnl: float = 0.0
    per_token_pnl: Dict[str, float] = field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"snapshots={self.n_snapshots} tokens={self.n_tokens} "
            f"orders={self.orders_placed} fills={self.fills} "
            f"volume=${self.filled_volume_usd:,.2f} pnl=${self.realized_pnl:+.4f}"
        )


class Backtester:
    def __init__(self, cfg: Config, recorder: HistoryRecorder):
        self._cfg = cfg
        self._rec = recorder

    def run(
        self,
        since: Optional[float] = None,
        until: Optional[float] = None,
        fill_probability: float = 0.5,
    ) -> BacktestResult:
        mm = self._cfg.market_maker
        vol = VolatilityTracker(
            window_sec=self._cfg.volatility.window_sec,
            min_samples=self._cfg.volatility.min_samples,
        )
        broker = PaperBroker(fill_probability=fill_probability, seed=42)
        positions: Dict[str, Position] = defaultdict(lambda: Position(token_id=""))
        result = BacktestResult()

        # Buffer previous snapshot per token for requote cadence tracking.
        last_quote_mid: Dict[str, float] = {}
        last_quote_ts: Dict[str, float] = {}

        tokens_seen = set()
        for ts, token_id, book in self._rec.iter_snapshots(since=since, until=until):
            result.n_snapshots += 1
            tokens_seen.add(token_id)
            if book.midpoint is None:
                continue
            vol.update(token_id, ts, book.midpoint)

            # Check fills BEFORE requoting — a fill could leave an empty slot.
            for fill in broker.match_fills({token_id: book}):
                pos = positions.setdefault(
                    fill.token_id, Position(token_id=fill.token_id),
                )
                if not pos.token_id:
                    pos.token_id = fill.token_id
                pre = pos.realized_pnl
                pos.apply_fill(fill.side, fill.shares, fill.price)  # type: ignore[arg-type]
                result.fills += 1
                result.filled_volume_usd += fill.price * fill.shares
                delta = pos.realized_pnl - pre
                result.realized_pnl += delta
                result.per_token_pnl[fill.token_id] = (
                    result.per_token_pnl.get(fill.token_id, 0.0) + delta
                )

            # Requote cadence: drift OR interval.
            mid = book.midpoint
            prev_mid = last_quote_mid.get(token_id)
            drift_bps = (
                abs(mid - prev_mid) / max(prev_mid, 1e-6) * 10000.0
                if prev_mid is not None else 1e9
            )
            elapsed = ts - last_quote_ts.get(token_id, 0.0)
            if drift_bps < mm.requote_drift_bps and elapsed < mm.requote_interval_sec:
                continue

            market = _synth_market(token_id)
            pair = compute_quote_pair(
                market, book, mm,
                inventory_shares=positions[token_id].net_shares if token_id in positions else 0,
                sigma=vol.sigma(token_id),
            )
            if pair is None:
                continue

            size_shares = max(market.min_order_size, mm.quote_size_usd / max(mid, 0.01))
            # Cancel previous resting ladder to avoid stacking.
            broker.cancel_all_for(token_id)
            broker.place_limit(Quote(token_id=token_id, side="BUY",  price=pair.bid, size=size_shares))
            broker.place_limit(Quote(token_id=token_id, side="SELL", price=pair.ask, size=size_shares))
            result.orders_placed += 2
            last_quote_mid[token_id] = mid
            last_quote_ts[token_id] = ts

        result.n_tokens = len(tokens_seen)
        return result


def _synth_market(token_id: str) -> TokenMarket:
    return TokenMarket(
        token_id=token_id,
        outcome="",
        condition_id="",
        question="",
        end_date_iso=None,
        tick_size=0.001,
        min_order_size=5.0,
        sibling_token_id=None,
    )


def run_cli_backtest(
    cfg: Config,
    hours: float,
    overrides: dict,
    fill_probability: float = 0.5,
) -> BacktestResult:
    # Apply overrides in-place.
    for k, v in overrides.items():
        if hasattr(cfg.market_maker, k):
            setattr(cfg.market_maker, k, v)
    rec = HistoryRecorder(cfg.history)
    since = time.time() - hours * 3600.0
    return Backtester(cfg, rec).run(since=since, fill_probability=fill_probability)
