"""Orchestrator — the main 24/7 loop."""

from __future__ import annotations

import logging
import signal
import time
from typing import List

from .clob import ClobClientWrapper
from .config import Config, Secrets
from .executor import OrderExecutor
from .gamma import GammaClient
from .metrics import Metrics
from .models import OrderBook
from .risk import RiskManager
from .scanner import MarketScanner
from .state import Store
from .strategies import (
    ArbitrageStrategy,
    MarketMakerStrategy,
    MeanReversionStrategy,
    Strategy,
)

log = logging.getLogger(__name__)


class Polybot:
    def __init__(self, cfg: Config, secrets: Secrets, dry_run: bool = False):
        self._cfg = cfg
        self._secrets = secrets
        self._dry_run = dry_run

        self._clob = ClobClientWrapper(
            host=secrets.clob_host,
            private_key=secrets.private_key,
            funder=secrets.funder,
            signature_type=secrets.signature_type,
            dry_run=dry_run,
        )
        self._gamma = GammaClient(
            host=secrets.gamma_host,
            timeout_sec=cfg.loop.http_timeout_sec,
        )
        self._risk = RiskManager(cfg.risk)
        self._executor = OrderExecutor(self._clob, self._risk)
        self._scanner = MarketScanner(
            gamma=self._gamma,
            clob=self._clob,
            scanner_cfg=cfg.scanner,
            mm_cfg=cfg.market_maker,
        )
        self._store = Store()
        self._metrics = Metrics(self._store)

        self._strategies: List[Strategy] = [
            MarketMakerStrategy(cfg.market_maker, self._executor, self._risk),
            ArbitrageStrategy(cfg.arbitrage, self._clob, self._risk),
            MeanReversionStrategy(cfg.mean_reversion, self._clob, self._risk),
        ]
        self._shutdown = False
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)

    def _on_signal(self, signum, _frame):
        log.warning("signal %s — shutting down", signum)
        self._shutdown = True

    def run(self) -> None:
        log.info(
            "Polybot starting (dry_run=%s, strategies=%s)",
            self._dry_run,
            [s.name for s in self._strategies],
        )
        try:
            while not self._shutdown:
                try:
                    self._tick()
                except Exception as e:
                    log.exception("tick error: %s", e)
                time.sleep(self._cfg.loop.tick_interval_sec)
        finally:
            log.info("cancelling all open orders before exit…")
            self._executor.cancel_everything()
            self._gamma.close()

    def _tick(self) -> None:
        if self._risk.kill_switch_active():
            log.warning("kill switch active — cancelling all orders and idling")
            self._executor.cancel_everything()
            time.sleep(30)
            return

        markets = self._scanner.active_markets()
        if not markets:
            log.debug("no active markets this tick")
            return

        books: dict[str, OrderBook] = {}
        for market in markets:
            book = self._clob.get_order_book(market.token_id)
            if book and book.midpoint is not None:
                books[market.token_id] = book

        for strat in self._strategies:
            try:
                strat.on_tick(markets, books)
            except Exception as e:
                log.exception("strategy %s error: %s", strat.name, e)

        self._metrics.tick(
            self._risk,
            resting_orders=self._executor.resting_count(),
            n_markets=len(books),
        )
