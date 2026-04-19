"""Token-bucket rate limiter.

Polymarket rate-limits the Gamma API (per their docs: ~300/10s on /markets,
4000/10s overall). We add a client-side token bucket per endpoint group so
bursts self-throttle instead of triggering server 429s.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class _Bucket:
    capacity: float
    refill_per_sec: float
    tokens: float
    last: float


class RateLimiter:
    def __init__(self):
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def configure(self, key: str, capacity: float, refill_per_sec: float) -> None:
        with self._lock:
            self._buckets[key] = _Bucket(
                capacity=capacity,
                refill_per_sec=refill_per_sec,
                tokens=capacity,
                last=time.monotonic(),
            )

    def acquire(self, key: str, tokens: float = 1.0, block: bool = True) -> bool:
        """Consume `tokens`. Blocks until available if `block=True`, else returns False."""
        while True:
            with self._lock:
                b = self._buckets.get(key)
                if b is None:
                    return True
                now = time.monotonic()
                elapsed = now - b.last
                b.tokens = min(b.capacity, b.tokens + elapsed * b.refill_per_sec)
                b.last = now
                if b.tokens >= tokens:
                    b.tokens -= tokens
                    return True
                wait = (tokens - b.tokens) / b.refill_per_sec
            if not block:
                return False
            time.sleep(max(0.01, min(wait, 1.0)))
