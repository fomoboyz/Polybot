"""Intra-market YES/NO arbitrage.

For a binary market, YES and NO sum to $1.00 at resolution. Whenever
`best_ask(YES) + best_ask(NO) + fees < 1.00 - buffer`, we buy both and lock
a risk-free profit (modulo platform / resolution risk).

We trade both legs as FOK (fill-or-kill) market orders so partial fills on
one leg leave us unhedged. Size is capped by `max_size_per_arb_usd` and by
the thinner of the two legs' top-of-book size.
"""

from __future__ import annotations

import logging
from typing import Sequence

from ..clob import ClobClientWrapper
from ..config import ArbitrageCfg
from ..gamma import group_by_condition
from ..models import OrderBook, TokenMarket
from ..risk import RiskManager

log = logging.getLogger(__name__)


class ArbitrageStrategy:
    name = "arbitrage"

    def __init__(
        self,
        cfg: ArbitrageCfg,
        clob: ClobClientWrapper,
        risk: RiskManager,
    ):
        self._cfg = cfg
        self._clob = clob
        self._risk = risk

    def on_tick(
        self,
        markets: Sequence[TokenMarket],
        books: dict[str, OrderBook],
    ) -> None:
        if not self._cfg.enabled:
            return

        for _cond, tokens in group_by_condition(markets).items():
            if len(tokens) != 2:
                continue
            a, b = tokens
            book_a = books.get(a.token_id)
            book_b = books.get(b.token_id)
            if not book_a or not book_b:
                continue
            ask_a = book_a.best_ask
            ask_b = book_b.best_ask
            if not ask_a or not ask_b:
                continue

            # Gross cost to synthesize $1.
            sum_ask = ask_a.price + ask_b.price
            raw_gap = 1.0 - sum_ask
            if raw_gap <= 0:
                continue

            fee_drag = (self._cfg.taker_fee_bps / 10000.0) * 1.0  # fee on $1 notional
            net_gap = raw_gap - fee_drag
            net_gap_bps = net_gap * 10000.0
            if net_gap_bps < self._cfg.min_profit_bps:
                continue

            max_shares_book = min(ask_a.size, ask_b.size)
            # Size both legs by $ cap and by available book depth.
            usd_cap = self._cfg.max_size_per_arb_usd
            usd_leg = min(
                usd_cap / 2.0,
                max_shares_book * ask_a.price,
                max_shares_book * ask_b.price,
            )
            if usd_leg < 1.0:
                continue

            log.info(
                "ARB %s: sum_ask=%.4f net=%.1fbps $%.2f/leg",
                a.question[:60], sum_ask, net_gap_bps, usd_leg,
            )

            # Risk gate: we're net-neutral by design but still cap notional.
            if self._risk.kill_switch_active():
                return
            ok_a = self._clob.place_market(a.token_id, "BUY", usd_leg)
            if not ok_a:
                continue
            ok_b = self._clob.place_market(b.token_id, "BUY", usd_leg)
            if not ok_b:
                log.warning(
                    "ARB leg-b failed after leg-a on %s — hedge may be required!",
                    a.condition_id,
                )
