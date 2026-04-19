"""Rolling realized-volatility tracker per token.

Feeds the inventory-aware pricer: σ is used to:
  1. Widen the spread when the market is moving fast (adaptive spread).
  2. Size the inventory-skew term in Avellaneda-Stoikov pricing.

The tracker uses a wall-clock-windowed std-dev of midpoint returns.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional, Tuple


@dataclass
class _Series:
    points: Deque[Tuple[float, float]]  # (ts, midpoint)


class VolatilityTracker:
    def __init__(self, window_sec: int = 300, min_samples: int = 8):
        self._window = window_sec
        self._min_samples = min_samples
        self._series: Dict[str, _Series] = {}

    def update(self, token_id: str, ts: float, midpoint: float) -> None:
        s = self._series.get(token_id)
        if s is None:
            s = _Series(points=deque(maxlen=4096))
            self._series[token_id] = s
        s.points.append((ts, midpoint))
        cutoff = ts - self._window
        while s.points and s.points[0][0] < cutoff:
            s.points.popleft()

    def sigma(self, token_id: str) -> Optional[float]:
        """Std dev of per-tick mid deltas over the window. Returns None if
        we don't have enough samples yet."""
        s = self._series.get(token_id)
        if s is None or len(s.points) < self._min_samples:
            return None
        mids = [m for _t, m in s.points]
        diffs = [mids[i] - mids[i - 1] for i in range(1, len(mids))]
        if not diffs:
            return None
        mean = sum(diffs) / len(diffs)
        var = sum((d - mean) ** 2 for d in diffs) / len(diffs)
        return math.sqrt(var)

    def midpoint_delta_bps(self, token_id: str, lookback_sec: float = 30.0) -> Optional[float]:
        """Magnitude of midpoint change over the last `lookback_sec`, in bps."""
        s = self._series.get(token_id)
        if s is None or len(s.points) < 2:
            return None
        now_ts, now_mid = s.points[-1]
        target = now_ts - lookback_sec
        for ts, mid in reversed(s.points):
            if ts <= target:
                if mid == 0:
                    return None
                return abs(now_mid - mid) / mid * 10000.0
        first_ts, first_mid = s.points[0]
        if first_mid == 0:
            return None
        return abs(now_mid - first_mid) / first_mid * 10000.0
