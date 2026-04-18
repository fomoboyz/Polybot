"""Thin wrapper over the Polymarket Gamma public API (market discovery)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .models import TokenMarket

log = logging.getLogger(__name__)


class GammaClient:
    def __init__(self, host: str, timeout_sec: int = 10):
        self._client = httpx.Client(base_url=host, timeout=timeout_sec)

    def close(self) -> None:
        self._client.close()

    @retry(
        reraise=True,
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=1, max=16),
        retry=retry_if_exception_type((httpx.HTTPError,)),
    )
    def _get(self, path: str, params: dict) -> list:
        r = self._client.get(path, params=params)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict):
            return data.get("data", [])
        return data

    def list_markets(
        self,
        min_liquidity: float,
        min_volume_24h: float,
        rewards_only: bool,
        exclude_closing_within_hours: float,
        limit: int = 200,
    ) -> List[TokenMarket]:
        """Discover active, liquid markets and return them as `TokenMarket`s
        (one per outcome token).
        """
        cutoff = datetime.now(timezone.utc) + timedelta(
            hours=exclude_closing_within_hours
        )
        params = {
            "active": "true",
            "closed": "false",
            "limit": limit,
            "order": "liquidity",
            "ascending": "false",
            "liquidity_num_min": min_liquidity,
            "volume_num_min": min_volume_24h,
        }
        raw = self._get("/markets", params)
        out: List[TokenMarket] = []
        for m in raw:
            if rewards_only and not _has_rewards(m):
                continue
            end_iso = m.get("endDate") or m.get("end_date_iso")
            if end_iso and _parse_iso(end_iso) < cutoff:
                continue
            tokens = _token_pair_from_market(m)
            if tokens is None:
                continue
            out.extend(tokens)
        return out


def _has_rewards(m: dict) -> bool:
    # Gamma surfaces rewards info in a few shapes depending on market type.
    for key in ("rewardsEnabled", "rewards_enabled", "rewardsMinSize"):
        if m.get(key):
            return True
    rewards = m.get("rewards")
    if isinstance(rewards, dict) and rewards.get("rates"):
        return True
    if isinstance(rewards, list) and rewards:
        return True
    return False


def _parse_iso(s: str) -> datetime:
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return datetime.fromisoformat(s)
    except ValueError:
        return datetime.max.replace(tzinfo=timezone.utc)


def _token_pair_from_market(m: dict) -> Optional[List[TokenMarket]]:
    """Convert a gamma market dict into its two outcome TokenMarkets."""
    condition_id = m.get("conditionId") or m.get("condition_id")
    question = m.get("question") or m.get("title") or ""
    end_date = m.get("endDate") or m.get("end_date_iso")
    tick = float(m.get("orderPriceMinTickSize") or m.get("minTickSize") or 0.001)
    min_size = float(m.get("orderMinSize") or m.get("minOrderSize") or 5.0)
    liq = float(m.get("liquidityNum") or m.get("liquidity") or 0.0 or 0)
    vol = float(m.get("volume24hrNum") or m.get("volume24hr") or 0.0 or 0)
    rewards_enabled = _has_rewards(m)

    # Gamma embeds tokens either as a structured list or as two parallel arrays.
    tokens = m.get("tokens")
    outcomes: list[tuple[str, str]] = []  # (token_id, outcome)
    if isinstance(tokens, list) and tokens:
        for t in tokens:
            tid = t.get("token_id") or t.get("tokenId")
            name = t.get("outcome") or t.get("name") or ""
            if tid:
                outcomes.append((str(tid), str(name)))
    else:
        ids_raw = m.get("clobTokenIds") or m.get("clob_token_ids") or []
        names_raw = m.get("outcomes") or []
        if isinstance(ids_raw, str):
            import json
            try:
                ids_raw = json.loads(ids_raw)
            except json.JSONDecodeError:
                ids_raw = []
        if isinstance(names_raw, str):
            import json
            try:
                names_raw = json.loads(names_raw)
            except json.JSONDecodeError:
                names_raw = []
        for i, tid in enumerate(ids_raw or []):
            nm = names_raw[i] if i < len(names_raw) else ""
            outcomes.append((str(tid), str(nm)))

    if len(outcomes) != 2 or not condition_id:
        return None

    yes_id, no_id = outcomes[0][0], outcomes[1][0]
    result = []
    for tid, outcome in outcomes:
        sibling = no_id if tid == yes_id else yes_id
        result.append(
            TokenMarket(
                token_id=tid,
                outcome=outcome,
                condition_id=str(condition_id),
                question=question,
                end_date_iso=end_date,
                min_order_size=min_size,
                tick_size=tick,
                liquidity_usd=liq,
                volume_24h_usd=vol,
                rewards_enabled=rewards_enabled,
                sibling_token_id=sibling,
            )
        )
    return result


def group_by_condition(markets: Iterable[TokenMarket]) -> dict[str, List[TokenMarket]]:
    out: dict[str, List[TokenMarket]] = {}
    for m in markets:
        out.setdefault(m.condition_id, []).append(m)
    return out
