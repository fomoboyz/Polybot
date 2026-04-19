"""Circuit breakers.

The bot trips these breakers autonomously in response to adverse conditions
and un-trips after a cooldown. While tripped, strategies don't run and all
resting orders are cancelled.

Trip conditions:
  - error_burst:       too many exceptions in a short window
  - stale_feed:        the freshest book we've seen is older than `max_age_sec`
  - quote_crosses:     the number of markets where mid moved by >crash_bps in
                       a short window exceeds a fraction of the active universe
  - api_failure_rate:  ratio of failed HTTP calls exceeds a threshold
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional

log = logging.getLogger(__name__)


@dataclass
class BreakerConfig:
    max_errors_per_minute: int = 30
    max_feed_age_sec: float = 120.0
    crash_bps_per_min: float = 500.0  # 5% mid move in 60s
    crash_market_share: float = 0.3   # ≥30% of markets crashing simultaneously
    max_api_failure_rate: float = 0.5
    cooldown_sec: int = 60


@dataclass
class CircuitBreaker:
    cfg: BreakerConfig
    _errors: Deque[float] = field(default_factory=deque)
    _api_results: Deque[tuple] = field(default_factory=deque)  # (ts, ok)
    _last_book_ts: Dict[str, float] = field(default_factory=dict)
    _tripped_until: float = 0.0
    _trip_reason: Optional[str] = None

    def record_error(self) -> None:
        self._errors.append(time.time())
        self._trim(self._errors, window=60.0)

    def record_api(self, ok: bool) -> None:
        self._api_results.append((time.time(), ok))
        # Keep last 200 calls.
        while len(self._api_results) > 200:
            self._api_results.popleft()

    def record_book(self, token_id: str, ts: float) -> None:
        self._last_book_ts[token_id] = ts

    def tripped(self) -> bool:
        return time.time() < self._tripped_until

    @property
    def reason(self) -> Optional[str]:
        return self._trip_reason if self.tripped() else None

    def check(self, active_tokens: List[str], mid_moves_bps: Dict[str, float]) -> Optional[str]:
        """Evaluate breakers. Trip on first failing condition. Returns the
        trip reason if a new trip happened, else None."""
        if self.tripped():
            return None

        now = time.time()
        self._trim(self._errors, window=60.0)
        if len(self._errors) > self.cfg.max_errors_per_minute:
            return self._trip(f"error_burst:{len(self._errors)}/min")

        if active_tokens:
            stale = sum(
                1 for tid in active_tokens
                if (now - self._last_book_ts.get(tid, now)) > self.cfg.max_feed_age_sec
            )
            if stale == len(active_tokens) and stale > 0:
                return self._trip("stale_feed:all")

        if active_tokens and mid_moves_bps:
            crashing = sum(
                1 for tid in active_tokens
                if mid_moves_bps.get(tid, 0.0) >= self.cfg.crash_bps_per_min
            )
            if crashing / max(len(active_tokens), 1) >= self.cfg.crash_market_share:
                return self._trip(f"crash:{crashing}/{len(active_tokens)}")

        if len(self._api_results) >= 20:
            recent = list(self._api_results)[-40:]
            fails = sum(1 for _, ok in recent if not ok)
            if fails / len(recent) > self.cfg.max_api_failure_rate:
                return self._trip(f"api_failure_rate:{fails}/{len(recent)}")

        return None

    def _trip(self, reason: str) -> str:
        self._tripped_until = time.time() + self.cfg.cooldown_sec
        self._trip_reason = reason
        log.error("circuit breaker TRIPPED: %s (cooldown %ds)", reason, self.cfg.cooldown_sec)
        return reason

    def reset(self) -> None:
        self._tripped_until = 0.0
        self._trip_reason = None

    @staticmethod
    def _trim(dq: Deque[float], window: float) -> None:
        cutoff = time.time() - window
        while dq and dq[0] < cutoff:
            dq.popleft()
