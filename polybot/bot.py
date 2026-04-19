"""Orchestrator — the main 24/7 loop.

Tick sequence:
  1. Safety: check kill switch & circuit breaker.
  2. Scan the market universe (cached, refreshed on interval).
  3. Fetch live order books; update volatility tracker.
  4. Paper mode only: drain simulated fills and feed them into risk/state.
  5. Run each strategy.
  6. Run copy-trading signal.
  7. Periodic: research engine + auto-tuner.
  8. Metrics snapshot.
"""

from __future__ import annotations

import logging
import signal
import time
from typing import Dict, List, Optional

from .circuit_breaker import BreakerConfig, CircuitBreaker
from .clob import ClobClientWrapper
from .config import Config, Secrets
from .copy_trading import CopyTrader
from .executor import OrderExecutor
from .gamma import GammaClient
from .metrics import Metrics
from .models import OrderBook
from .paper import SimFill
from .research import ResearchEngine
from .risk import RiskManager
from .scanner import MarketScanner
from .state import Store
from .strategies import (
    ArbitrageStrategy,
    MarketMakerStrategy,
    MeanReversionStrategy,
    Strategy,
)
from .tuner import AutoTuner
from .volatility import VolatilityTracker

log = logging.getLogger(__name__)


class Polybot:
    def __init__(
        self,
        cfg: Config,
        secrets: Secrets,
        dry_run: bool = False,
        paper: bool = False,
    ):
        self._cfg = cfg
        self._secrets = secrets
        self._dry_run = dry_run
        self._paper = paper

        self._clob = ClobClientWrapper(
            host=secrets.clob_host,
            private_key=secrets.private_key,
            funder=secrets.funder,
            signature_type=secrets.signature_type,
            dry_run=dry_run,
            paper=paper,
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
        self._vol = VolatilityTracker(
            window_sec=cfg.volatility.window_sec,
            min_samples=cfg.volatility.min_samples,
        )
        self._breaker = CircuitBreaker(BreakerConfig(
            max_errors_per_minute=cfg.breaker.max_errors_per_minute,
            max_feed_age_sec=cfg.breaker.max_feed_age_sec,
            crash_bps_per_min=cfg.breaker.crash_bps_per_min,
            crash_market_share=cfg.breaker.crash_market_share,
            max_api_failure_rate=cfg.breaker.max_api_failure_rate,
            cooldown_sec=cfg.breaker.cooldown_sec,
        ))
        self._store = Store()
        self._metrics = Metrics(self._store)
        self._research: Optional[ResearchEngine] = (
            ResearchEngine(cfg, secrets) if cfg.research.enabled else None
        )
        self._copy: Optional[CopyTrader] = (
            CopyTrader(cfg, secrets, self._clob, self._risk)
            if cfg.copy_trading.enabled else None
        )
        self._tuner = AutoTuner(cfg, live=not (dry_run or paper))

        self._strategies: List[Strategy] = [
            MarketMakerStrategy(cfg.market_maker, self._executor, self._risk, self._vol),
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
        mode = "LIVE" if not (self._dry_run or self._paper) else (
            "PAPER" if self._paper else "DRY-RUN"
        )
        log.info(
            "Polybot starting mode=%s strategies=%s",
            mode,
            [s.name for s in self._strategies],
        )
        try:
            while not self._shutdown:
                try:
                    self._tick()
                except Exception as e:
                    log.exception("tick error: %s", e)
                    self._breaker.record_error()
                time.sleep(self._cfg.loop.tick_interval_sec)
        finally:
            log.info("cancelling all open orders before exit…")
            self._executor.cancel_everything()
            self._gamma.close()
            if self._research is not None:
                self._research.close()
            if self._copy is not None:
                self._copy.close()

    def _tick(self) -> None:
        if self._risk.kill_switch_active():
            log.warning("kill switch active — cancelling and idling")
            self._executor.cancel_everything()
            time.sleep(30)
            return

        if self._breaker.tripped():
            log.warning("breaker %s — idling", self._breaker.reason)
            self._executor.cancel_everything()
            time.sleep(5)
            return

        markets = self._scanner.active_markets()
        if not markets:
            return

        books: Dict[str, OrderBook] = {}
        now = time.time()
        mid_moves_bps: Dict[str, float] = {}

        for market in markets:
            try:
                book = self._clob.get_order_book(market.token_id)
                self._breaker.record_api(True)
            except Exception as e:
                log.debug("book fetch %s failed: %s", market.token_id[:10], e)
                self._breaker.record_api(False)
                continue
            if book and book.midpoint is not None:
                books[market.token_id] = book
                self._vol.update(market.token_id, now, book.midpoint)
                self._breaker.record_book(market.token_id, now)
                delta = self._vol.midpoint_delta_bps(market.token_id, lookback_sec=60.0)
                if delta is not None:
                    mid_moves_bps[market.token_id] = delta

        breaker_trip = self._breaker.check(
            active_tokens=[m.token_id for m in markets],
            mid_moves_bps=mid_moves_bps,
        )
        if breaker_trip:
            self._executor.cancel_everything()
            return

        # Drain paper fills BEFORE strategies decide — strategies read current
        # inventory from risk.
        if self._paper:
            for fill in self._clob.drain_paper_fills(books):
                self._on_paper_fill(fill)

        for strat in self._strategies:
            try:
                strat.on_tick(markets, books)
            except Exception as e:
                log.exception("strategy %s error: %s", strat.name, e)
                self._breaker.record_error()

        if self._copy is not None:
            try:
                self._copy.on_tick(books)
            except Exception as e:
                log.warning("copy-trader error: %s", e)
                self._breaker.record_error()

        if self._research is not None and self._research.due():
            try:
                self._research.run()
            except Exception as e:
                log.warning("research error: %s", e)

        if self._tuner.due():
            try:
                self._tuner.run()
            except Exception as e:
                log.warning("tuner error: %s", e)

        self._metrics.tick(
            self._risk,
            resting_orders=self._executor.resting_count(),
            n_markets=len(books),
        )

    def _on_paper_fill(self, fill: SimFill) -> None:
        self._risk.on_fill(fill.token_id, fill.side, fill.shares, fill.price)
        self._store.record_fill(
            order_id=fill.order_id,
            token_id=fill.token_id,
            side=fill.side,
            price=fill.price,
            shares=fill.shares,
            filled_at=fill.filled_at,
        )
        # Clear the resting slot so the market-maker requotes.
        self._executor.mark_filled(fill.order_id, fill.token_id, fill.side)
