"""Prometheus metrics, structured JSON logging, and webhook alerting."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional

try:
    from prometheus_client import Counter, Gauge, Histogram, start_http_server
except ImportError:  # pragma: no cover — optional
    Counter = Gauge = Histogram = None  # type: ignore
    start_http_server = None  # type: ignore

import httpx

from .config import ObservabilityCfg

log = logging.getLogger(__name__)


class PrometheusRegistry:
    """Lazy registry so importing the module doesn't bind a port."""

    def __init__(self, cfg: ObservabilityCfg):
        self._cfg = cfg
        self._started = False
        self.orders_placed = None
        self.orders_cancelled = None
        self.orders_filled = None
        self.volume_usd = None
        self.pnl_usd = None
        self.tick_latency = None
        self.resting_orders = None
        self.active_markets = None
        self.breaker_tripped = None

    def start(self) -> None:
        if self._started or start_http_server is None or Counter is None:
            if not self._cfg.prometheus_enabled:
                return
            if start_http_server is None:
                log.warning("prometheus_client missing — metrics endpoint disabled")
                return
        if not self._cfg.prometheus_enabled:
            return
        self.orders_placed = Counter("polybot_orders_placed_total", "Orders placed", ["side"])
        self.orders_cancelled = Counter("polybot_orders_cancelled_total", "Orders cancelled")
        self.orders_filled = Counter("polybot_orders_filled_total", "Orders filled", ["side"])
        self.volume_usd = Counter("polybot_volume_usd_total", "Filled volume USD")
        self.pnl_usd = Gauge("polybot_pnl_usd_today", "Realized PnL today (USD)")
        self.tick_latency = Histogram("polybot_tick_latency_seconds", "Bot tick latency")
        self.resting_orders = Gauge("polybot_resting_orders", "Currently resting orders")
        self.active_markets = Gauge("polybot_active_markets", "Markets being quoted")
        self.breaker_tripped = Gauge("polybot_breaker_tripped", "1 if breaker tripped else 0")
        try:
            start_http_server(self._cfg.prometheus_port)
            self._started = True
            log.info("prometheus on :%d", self._cfg.prometheus_port)
        except OSError as e:
            log.warning("prometheus start failed: %s", e)


class Alerter:
    """Fires a webhook on critical events. Best-effort; never raises."""

    def __init__(self, cfg: ObservabilityCfg):
        self._cfg = cfg
        self._client = httpx.Client(timeout=5) if cfg.alert_webhook_url else None

    def fire(self, severity: str, message: str, **extra: Any) -> None:
        if not self._cfg.alert_webhook_url or self._client is None:
            return
        try:
            self._client.post(
                self._cfg.alert_webhook_url,
                json={
                    "severity": severity,
                    "message": message,
                    "ts": int(time.time()),
                    "service": "polybot",
                    **extra,
                },
            )
        except Exception as e:
            log.debug("alert webhook failed: %s", e)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": int(record.created * 1000),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str, json_logs: bool = False) -> None:
    root = logging.getLogger()
    for h in root.handlers[:]:
        root.removeHandler(h)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    handler = logging.StreamHandler()
    if json_logs:
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
        ))
    root.addHandler(handler)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)
