"""Minimal metrics sink — logs to stdout via rich and mirrors to SQLite."""

from __future__ import annotations

import logging
import time

from rich.console import Console
from rich.table import Table

from .risk import RiskManager
from .state import Store

log = logging.getLogger(__name__)
_console = Console()


class Metrics:
    def __init__(self, store: Store, interval_sec: int = 30):
        self._store = store
        self._interval = interval_sec
        self._last = 0.0

    def tick(self, risk: RiskManager, resting_orders: int, n_markets: int) -> None:
        now = time.time()
        if now - self._last < self._interval:
            return
        self._last = now
        snap = risk.snapshot()
        snap["resting_orders"] = resting_orders
        snap["active_markets"] = n_markets

        table = Table(title="Polybot", show_header=True, header_style="bold")
        table.add_column("Metric")
        table.add_column("Value", justify="right")
        for k, v in snap.items():
            table.add_row(k, str(v))
        _console.print(table)

        for k, v in snap.items():
            if isinstance(v, (int, float)):
                self._store.record_metric(now, k, float(v))
