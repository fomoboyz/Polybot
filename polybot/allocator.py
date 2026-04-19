"""Kelly-lite capital allocator.

Given the active universe and their expected-score weights, splits the
available market-making capital across markets proportional to score,
capped by per-market limits. This replaces the flat `quote_size_usd`
where appropriate.

The allocation is "Kelly-lite" — not classical full-Kelly — because on
prediction markets the payoff distribution is unknown and we don't want to
size aggressively on thin evidence. We use proportional-to-edge with a
hard max per market.

Usage:
    alloc = Allocator(cfg)
    sizes = alloc.allocate(ranked_pairs, total_budget_usd)
    size_usd = sizes[market.token_id]
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from .config import AllocatorCfg, RiskCfg
from .models import TokenMarket
from .reward_optimizer import QuotePair


class Allocator:
    def __init__(self, alloc_cfg: AllocatorCfg, risk_cfg: RiskCfg):
        self._cfg = alloc_cfg
        self._risk_cfg = risk_cfg

    @property
    def enabled(self) -> bool:
        return self._cfg.enabled

    def allocate(
        self,
        ranked: List[Tuple[TokenMarket, QuotePair]],
        total_budget_usd: float,
    ) -> Dict[str, float]:
        if not self._cfg.enabled or not ranked or total_budget_usd <= 0:
            return {}
        scores = [max(0.0, p.expected_score) for _m, p in ranked]
        total_score = sum(scores)
        if total_score <= 0:
            # Fall back to equal split.
            per = min(self._risk_cfg.max_per_market_usd, total_budget_usd / len(ranked))
            return {m.token_id: per for m, _ in ranked}
        out: Dict[str, float] = {}
        # Proportional to score, clamped by [min_per_market, max_per_market].
        for (m, _p), s in zip(ranked, scores):
            share = total_budget_usd * (s / total_score)
            share = min(self._risk_cfg.max_per_market_usd, share)
            share = max(self._cfg.min_per_market_usd, share) if share > 0 else 0.0
            if share >= self._risk_cfg.min_order_usd:
                out[m.token_id] = share
        # If clamping pushed us over budget, scale down uniformly.
        total = sum(out.values())
        if total > total_budget_usd and total > 0:
            factor = total_budget_usd / total
            out = {k: v * factor for k, v in out.items()}
        return out
