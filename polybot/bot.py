"""Orchestrator — the main 24/7 loop.

Tick sequence:
  1. Safety: kill switch & circuit breaker.
  2. Scan universe (cached, refreshed on interval).
  3. For each market, prefer a WebSocket book if fresh, else fall back to REST.
  4. Update volatility tracker.
  5. Paper mode: drain simulated fills into risk/state/executor.
  6. Run each strategy.
  7. Copy-trading signal.
  8. Periodic: research engine + auto-tuner.
  9. Metrics snapshot (rich + Prometheus).
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
from .dashboard import Dashboard
from .executor import OrderExecutor
from .gamma import GammaClient
from .health import HealthServer, HealthState
from .history import HistoryRecorder
from .metrics import Metrics
from .models import OrderBook
from .observability import Alerter, PrometheusRegistry, configure_logging
from .paper import SimFill
from .rate_limit import RateLimiter
from .reconciler import Reconciler
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
from .trial import TrialRunner
from .tuner import AutoTuner
from .volatility import VolatilityTracker
from .websocket_feed import WebSocketFeed

log = logging.getLogger(__name__)


class Polybot:
    def __init__(
        self,
        cfg: Config,
        secrets: Secrets,
        dry_run: bool = False,
        paper: bool = False,
        trial: Optional[TrialRunner] = None,
    ):
        self._trial = trial
        configure_logging(secrets.log_level, cfg.observability.json_logs)
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
        self._rate = RateLimiter()
        # Polymarket Gamma: 4000/10s overall, 300/10s on /markets. We stay
        # conservative to give headroom for scanner refresh + research.
        self._rate.configure("gamma", capacity=200, refill_per_sec=20)

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
        self._prom = PrometheusRegistry(cfg.observability)
        self._prom.start()
        self._alerter = Alerter(cfg.observability)

        self._ws: Optional[WebSocketFeed] = None
        if cfg.websocket.enabled:
            self._ws = WebSocketFeed(
                url=cfg.websocket.url,
                ping_interval_sec=cfg.websocket.ping_interval_sec,
            )
            self._ws.start()

        self._research: Optional[ResearchEngine] = (
            ResearchEngine(cfg, secrets) if cfg.research.enabled else None
        )
        self._copy: Optional[CopyTrader] = (
            CopyTrader(cfg, secrets, self._clob, self._risk)
            if cfg.copy_trading.enabled else None
        )
        self._tuner = AutoTuner(cfg, live=not (dry_run or paper))
        self._reconciler = Reconciler(cfg.reconciler, self._clob, self._risk)
        self._recorder = HistoryRecorder(cfg.history) if cfg.history.enabled else None

        self._health = HealthState(max_tick_age_sec=cfg.health.max_tick_age_sec)
        self._health.register_provider(self._health_snapshot)
        mode = "LIVE" if not (dry_run or paper) else ("PAPER" if paper else "DRY-RUN")
        self._dashboard = Dashboard(
            cfg=cfg,
            risk=self._risk,
            executor=self._executor,
            breaker=self._breaker,
            mode=mode,
            trial=trial,
        )
        self._health.attach_dashboard(self._dashboard)
        self._health_server: Optional[HealthServer] = None
        if cfg.health.enabled:
            self._health_server = HealthServer(
                self._health, port=cfg.health.port, host=cfg.health.host,
            )
            self._health_server.start()

        self._strategies: List[Strategy] = [
            MarketMakerStrategy(
                cfg.market_maker, self._executor, self._risk, self._vol,
                ladder_cfg=cfg.ladder,
                allocator_cfg=cfg.allocator,
                risk_cfg=cfg.risk,
            ),
            ArbitrageStrategy(cfg.arbitrage, self._clob, self._risk),
            MeanReversionStrategy(cfg.mean_reversion, self._clob, self._risk),
        ]
        self._shutdown = False
        signal.signal(signal.SIGINT, self._on_signal)
        signal.signal(signal.SIGTERM, self._on_signal)

        self._last_breaker_trip: Optional[str] = None

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
        self._alerter.fire("info", f"polybot started mode={mode}")
        try:
            while not self._shutdown:
                if self._trial is not None and self._trial.should_exit():
                    log.info("trial duration reached — finalizing")
                    self._trial.finalize(self._risk)
                    break
                t0 = time.time()
                try:
                    self._tick()
                except Exception as e:
                    log.exception("tick error: %s", e)
                    self._breaker.record_error()
                if self._trial is not None and self._trial.checkpoint_due():
                    try:
                        self._trial.record_checkpoint(
                            self._risk, self._executor.resting_count(),
                        )
                    except Exception as e:
                        log.warning("trial checkpoint error: %s", e)
                if self._prom.tick_latency is not None:
                    self._prom.tick_latency.observe(time.time() - t0)
                time.sleep(self._cfg.loop.tick_interval_sec)
        finally:
            log.info("cancelling all open orders before exit…")
            self._executor.cancel_everything()
            self._gamma.close()
            if self._research is not None:
                self._research.close()
            if self._copy is not None:
                self._copy.close()
            if self._ws is not None:
                self._ws.stop()
            if self._health_server is not None:
                self._health_server.stop()
            self._alerter.fire("info", "polybot stopped")
            self._alerter.close()

    # ---- tick ----

    def _tick(self) -> None:
        if self._risk.kill_switch_active():
            log.warning("kill switch active — cancelling and idling")
            self._executor.cancel_everything()
            time.sleep(30)
            return

        if self._breaker.tripped():
            if self._breaker.reason != self._last_breaker_trip:
                self._alerter.fire("warning", f"breaker tripped: {self._breaker.reason}")
                self._last_breaker_trip = self._breaker.reason
            self._executor.cancel_everything()
            if self._prom.breaker_tripped is not None:
                self._prom.breaker_tripped.set(1)
            time.sleep(5)
            return
        if self._prom.breaker_tripped is not None:
            self._prom.breaker_tripped.set(0)

        self._rate.acquire("gamma")
        markets = self._scanner.active_markets()
        if not markets:
            return

        # Sync WS subscription list to active universe.
        if self._ws is not None:
            self._ws.set_tokens([m.token_id for m in markets])

        books = self._fetch_books(markets)

        mid_moves_bps: Dict[str, float] = {}
        now = time.time()
        for token_id, book in books.items():
            if book.midpoint is not None:
                self._vol.update(token_id, now, book.midpoint)
                self._breaker.record_book(token_id, now)
                delta = self._vol.midpoint_delta_bps(token_id, lookback_sec=60.0)
                if delta is not None:
                    mid_moves_bps[token_id] = delta

        breaker_trip = self._breaker.check(
            active_tokens=[m.token_id for m in markets],
            mid_moves_bps=mid_moves_bps,
        )
        if breaker_trip:
            self._executor.cancel_everything()
            return

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

        if self._reconciler.due():
            try:
                self._reconciler.run()
            except Exception as e:
                log.warning("reconciler error: %s", e)

        if self._recorder is not None:
            try:
                self._recorder.record(books)
            except Exception as e:
                log.debug("history recorder error: %s", e)

        self._publish_metrics(len(books))
        self._health.mark_tick()

    # ---- helpers ----

    def _fetch_books(self, markets) -> Dict[str, OrderBook]:
        """Prefer WebSocket feed, fall back to REST when ws is stale or missing."""
        books: Dict[str, OrderBook] = {}
        max_age_ms = self._cfg.websocket.max_staleness_sec * 1000
        now_ms = time.time() * 1000
        for market in markets:
            book = None
            if self._ws is not None:
                ws_book = self._ws.get(market.token_id)
                if ws_book is not None and (now_ms - ws_book.timestamp_ms) < max_age_ms:
                    book = ws_book
            if book is None:
                try:
                    book = self._clob.get_order_book(market.token_id)
                    self._breaker.record_api(True)
                except Exception as e:
                    log.debug("book fetch %s failed: %s", market.token_id[:10], e)
                    self._breaker.record_api(False)
                    continue
            if book and book.midpoint is not None:
                books[market.token_id] = book
        return books

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
        self._executor.mark_filled(fill.order_id, fill.token_id, fill.side)
        if self._prom.orders_filled is not None:
            self._prom.orders_filled.labels(side=fill.side).inc()
        if self._prom.volume_usd is not None:
            self._prom.volume_usd.inc(fill.price * fill.shares)

    def _health_snapshot(self) -> dict:
        snap = self._risk.snapshot()
        snap["breaker_tripped"] = self._breaker.tripped()
        snap["breaker_reason"] = self._breaker.reason
        if self._ws is not None:
            snap["ws_connected"] = self._ws.connected
            snap["ws_disconnects"] = self._ws.disconnect_count
            age = self._ws.age_sec()
            snap["ws_last_msg_age_sec"] = round(age, 2) if age is not None else None
        return snap

    def _publish_metrics(self, n_books: int) -> None:
        self._metrics.tick(
            self._risk,
            resting_orders=self._executor.resting_count(),
            n_markets=n_books,
        )
        if self._prom.pnl_usd is not None:
            self._prom.pnl_usd.set(self._risk.state.realized_pnl_today)
        if self._prom.resting_orders is not None:
            self._prom.resting_orders.set(self._executor.resting_count())
        if self._prom.active_markets is not None:
            self._prom.active_markets.set(n_books)
