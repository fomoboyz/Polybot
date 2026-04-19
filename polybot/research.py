"""Automatic research pass.

Every `research.interval_hours`, pulls:

  1. Top traders by PnL from Polymarket's public data API, when available.
  2. Top reward-earning markets from the Gamma API.
  3. Recent volume leaders.

Produces a markdown report at `state/research/<timestamp>.md` and a machine-
readable `state/research/latest.json` that the auto-tuner and the copy-trading
watchlist consume.

All network calls are best-effort: if an endpoint is down or its schema has
drifted, the module logs, degrades gracefully, and still writes whatever it
could collect.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from .config import Config, ResearchCfg, Secrets

log = logging.getLogger(__name__)


@dataclass
class WalletSummary:
    address: str
    pnl_usd: float
    volume_usd: float
    trades: int = 0
    win_rate: Optional[float] = None


@dataclass
class MarketSummary:
    token_id: str
    condition_id: str
    question: str
    volume_24h_usd: float
    liquidity_usd: float
    spread_bps: Optional[float] = None
    rewards_enabled: bool = False


@dataclass
class ResearchSnapshot:
    generated_at: float
    top_wallets: List[WalletSummary] = field(default_factory=list)
    top_markets_by_volume: List[MarketSummary] = field(default_factory=list)
    top_markets_by_liquidity: List[MarketSummary] = field(default_factory=list)
    reward_eligible_markets: List[MarketSummary] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


class ResearchEngine:
    def __init__(self, cfg: Config, secrets: Secrets):
        self._cfg = cfg
        self._secrets = secrets
        self._client = httpx.Client(timeout=cfg.loop.http_timeout_sec)
        self._last_run: float = 0.0

    # ---- scheduling ----

    def due(self) -> bool:
        interval_sec = self._cfg.research.interval_hours * 3600.0
        return (time.time() - self._last_run) >= interval_sec

    def run(self) -> ResearchSnapshot:
        self._last_run = time.time()
        snap = ResearchSnapshot(generated_at=self._last_run)

        try:
            snap.top_wallets = self._fetch_top_wallets()
        except Exception as e:
            msg = f"top_wallets: {e}"
            log.warning(msg)
            snap.notes.append(msg)

        try:
            markets = self._fetch_markets()
            snap.top_markets_by_volume = sorted(
                markets, key=lambda m: m.volume_24h_usd, reverse=True
            )[:25]
            snap.top_markets_by_liquidity = sorted(
                markets, key=lambda m: m.liquidity_usd, reverse=True
            )[:25]
            snap.reward_eligible_markets = [m for m in markets if m.rewards_enabled][:25]
        except Exception as e:
            msg = f"markets: {e}"
            log.warning(msg)
            snap.notes.append(msg)

        self._persist(snap)
        return snap

    # ---- fetchers ----

    def _fetch_top_wallets(self) -> List[WalletSummary]:
        """Best-effort leaderboard fetch. Tries a few likely endpoints."""
        cfg = self._cfg.research
        candidates = [
            (
                f"{self._secrets.data_host}/leaderboards/top",
                {"window": cfg.leaderboard_window, "limit": cfg.top_wallets},
            ),
            (
                f"{self._secrets.data_host}/leaderboard",
                {"window": cfg.leaderboard_window, "limit": cfg.top_wallets},
            ),
            (
                f"{self._secrets.gamma_host}/leaderboard",
                {"window": cfg.leaderboard_window, "limit": cfg.top_wallets},
            ),
        ]
        last_err: Optional[Exception] = None
        for url, params in candidates:
            try:
                r = self._client.get(url, params=params)
                if r.status_code == 404:
                    continue
                r.raise_for_status()
                raw = r.json()
                entries = raw.get("data") if isinstance(raw, dict) else raw
                if not isinstance(entries, list) or not entries:
                    continue
                return [
                    WalletSummary(
                        address=str(e.get("address") or e.get("user") or e.get("proxyWallet") or ""),
                        pnl_usd=float(e.get("pnl") or e.get("profit") or 0.0),
                        volume_usd=float(e.get("volume") or e.get("totalVolume") or 0.0),
                        trades=int(e.get("trades") or e.get("tradeCount") or 0),
                        win_rate=(
                            float(e["winRate"]) if "winRate" in e and e["winRate"] is not None else None
                        ),
                    )
                    for e in entries
                    if e.get("address") or e.get("user") or e.get("proxyWallet")
                ]
            except Exception as e:
                last_err = e
                continue
        if last_err:
            raise last_err
        return []

    def _fetch_markets(self) -> List[MarketSummary]:
        cfg = self._cfg.research
        params = {
            "active": "true",
            "closed": "false",
            "limit": 500,
            "order": "volume24hr",
            "ascending": "false",
            "liquidity_num_min": self._cfg.scanner.min_liquidity_usd,
        }
        r = self._client.get(f"{self._secrets.gamma_host}/markets", params=params)
        r.raise_for_status()
        raw = r.json()
        entries = raw.get("data") if isinstance(raw, dict) else raw
        out: List[MarketSummary] = []
        for m in entries or []:
            tokens = _extract_token_ids(m)
            if not tokens:
                continue
            tok = tokens[0]
            out.append(MarketSummary(
                token_id=tok,
                condition_id=str(m.get("conditionId") or m.get("condition_id") or ""),
                question=str(m.get("question") or m.get("title") or ""),
                volume_24h_usd=float(m.get("volume24hrNum") or m.get("volume24hr") or 0.0 or 0),
                liquidity_usd=float(m.get("liquidityNum") or m.get("liquidity") or 0.0 or 0),
                rewards_enabled=_rewards_enabled(m),
            ))
        _ = cfg  # unused for now, but kept for future filters
        return out

    # ---- persistence ----

    def _persist(self, snap: ResearchSnapshot) -> None:
        out_dir = Path(self._cfg.research.report_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(snap.generated_at))
        md = out_dir / f"{stamp}.md"
        md.write_text(_render_markdown(snap))
        latest = out_dir / "latest.json"
        latest.write_text(json.dumps(_snapshot_to_jsonable(snap), indent=2))
        log.info("research snapshot written to %s (+%s)", md, latest.name)

    def close(self) -> None:
        self._client.close()


def _extract_token_ids(m: Dict[str, Any]) -> List[str]:
    ids = m.get("clobTokenIds") or m.get("clob_token_ids") or []
    if isinstance(ids, str):
        try:
            ids = json.loads(ids)
        except json.JSONDecodeError:
            ids = []
    tokens = m.get("tokens") or []
    if isinstance(tokens, list) and tokens:
        return [str(t.get("token_id") or t.get("tokenId") or "") for t in tokens if t]
    return [str(x) for x in ids or []]


def _rewards_enabled(m: Dict[str, Any]) -> bool:
    for k in ("rewardsEnabled", "rewards_enabled", "rewardsMinSize"):
        if m.get(k):
            return True
    rewards = m.get("rewards")
    if isinstance(rewards, dict) and rewards.get("rates"):
        return True
    if isinstance(rewards, list) and rewards:
        return True
    return False


def _snapshot_to_jsonable(snap: ResearchSnapshot) -> dict:
    return {
        "generated_at": snap.generated_at,
        "top_wallets": [asdict(w) for w in snap.top_wallets],
        "top_markets_by_volume": [asdict(m) for m in snap.top_markets_by_volume],
        "top_markets_by_liquidity": [asdict(m) for m in snap.top_markets_by_liquidity],
        "reward_eligible_markets": [asdict(m) for m in snap.reward_eligible_markets],
        "notes": snap.notes,
    }


def _render_markdown(snap: ResearchSnapshot) -> str:
    lines = [
        f"# Polybot Research — {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(snap.generated_at))}",
        "",
    ]
    if snap.notes:
        lines.append("## Notes")
        lines.extend(f"- {n}" for n in snap.notes)
        lines.append("")
    if snap.top_wallets:
        lines.append("## Top wallets (copy-trading candidates)")
        lines.append("| # | Wallet | PnL | Volume | Trades |")
        lines.append("|---|---|---|---|---|")
        for i, w in enumerate(snap.top_wallets[:25], 1):
            lines.append(
                f"| {i} | `{w.address[:10]}…` | ${w.pnl_usd:,.0f} | "
                f"${w.volume_usd:,.0f} | {w.trades} |"
            )
        lines.append("")
    if snap.top_markets_by_volume:
        lines.append("## Top markets by 24h volume")
        lines.append("| # | Market | Vol | Liq | Rewards |")
        lines.append("|---|---|---|---|---|")
        for i, m in enumerate(snap.top_markets_by_volume[:20], 1):
            q = (m.question or "")[:70]
            lines.append(
                f"| {i} | {q} | ${m.volume_24h_usd:,.0f} | "
                f"${m.liquidity_usd:,.0f} | {'✓' if m.rewards_enabled else ''} |"
            )
        lines.append("")
    if snap.reward_eligible_markets:
        lines.append("## Reward-eligible markets (priority for market-making)")
        lines.append("| # | Market | Liq | Vol |")
        lines.append("|---|---|---|---|")
        for i, m in enumerate(snap.reward_eligible_markets[:20], 1):
            q = (m.question or "")[:70]
            lines.append(
                f"| {i} | {q} | ${m.liquidity_usd:,.0f} | "
                f"${m.volume_24h_usd:,.0f} |"
            )
        lines.append("")
    return "\n".join(lines)
