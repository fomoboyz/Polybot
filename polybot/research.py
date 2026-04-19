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
class TraderStyle:
    address: str
    style: str  # "market_maker" | "directional" | "arbitrageur" | "mixed"
    pnl_usd: float
    avg_hold_sec: Optional[float] = None
    taker_ratio: Optional[float] = None  # fraction of taker fills vs maker
    markets_traded: int = 0
    reasoning: str = ""


@dataclass
class StrategyRecommendation:
    lever: str           # e.g. "market_maker.target_spread_bps"
    current: Any
    recommended: Any
    confidence: float    # 0..1
    justification: str


@dataclass
class ResearchSnapshot:
    generated_at: float
    top_wallets: List[WalletSummary] = field(default_factory=list)
    top_markets_by_volume: List[MarketSummary] = field(default_factory=list)
    top_markets_by_liquidity: List[MarketSummary] = field(default_factory=list)
    reward_eligible_markets: List[MarketSummary] = field(default_factory=list)
    trader_styles: List[TraderStyle] = field(default_factory=list)
    recommendations: List[StrategyRecommendation] = field(default_factory=list)
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

        try:
            snap.trader_styles = self._profile_top_wallets(snap.top_wallets[:15])
        except Exception as e:
            log.debug("trader profiling: %s", e)

        snap.recommendations = self._derive_recommendations(snap)

        self._persist(snap)
        return snap

    # ---- trader profiling ----

    def _profile_top_wallets(self, wallets: List[WalletSummary]) -> List[TraderStyle]:
        """For each top wallet, pull recent trades and classify their style."""
        out: List[TraderStyle] = []
        for w in wallets:
            trades = self._wallet_trades(w.address)
            if not trades:
                out.append(TraderStyle(
                    address=w.address, style="unknown", pnl_usd=w.pnl_usd,
                    reasoning="no-trade-data",
                ))
                continue
            style, hold, taker_ratio, markets = classify_trader(trades)
            out.append(TraderStyle(
                address=w.address, style=style, pnl_usd=w.pnl_usd,
                avg_hold_sec=hold, taker_ratio=taker_ratio, markets_traded=markets,
                reasoning=(
                    f"taker_ratio={taker_ratio:.2f} hold={hold or 0:.0f}s "
                    f"n_markets={markets}"
                ),
            ))
        return out

    def _wallet_trades(self, address: str) -> List[dict]:
        for url in (
            f"{self._secrets.data_host}/trades",
            f"{self._secrets.gamma_host}/trades",
        ):
            try:
                r = self._client.get(url, params={"user": address, "limit": 100})
                if r.status_code == 404:
                    continue
                r.raise_for_status()
                raw = r.json()
                entries = raw.get("data") if isinstance(raw, dict) else raw
                if isinstance(entries, list):
                    return entries
            except Exception:
                continue
        return []

    # ---- recommendations ----

    def _derive_recommendations(self, snap: ResearchSnapshot) -> List[StrategyRecommendation]:
        recs: List[StrategyRecommendation] = []
        mm = self._cfg.market_maker
        if snap.trader_styles:
            makers = [t for t in snap.trader_styles if t.style == "market_maker"]
            if makers and len(makers) / max(len(snap.trader_styles), 1) > 0.5:
                # Top earners are MMs — suggest we tighten spread further.
                recs.append(StrategyRecommendation(
                    lever="market_maker.target_spread_bps",
                    current=mm.target_spread_bps,
                    recommended=max(5.0, mm.target_spread_bps * 0.8),
                    confidence=0.5,
                    justification=(
                        f"{len(makers)}/{len(snap.trader_styles)} top wallets are "
                        f"market-makers; tightening spread may compete for LP rewards."
                    ),
                ))
            if any(t.style == "arbitrageur" for t in snap.trader_styles):
                recs.append(StrategyRecommendation(
                    lever="arbitrage.enabled",
                    current=self._cfg.arbitrage.enabled,
                    recommended=True,
                    confidence=0.4,
                    justification="Top wallets include arbitrageurs — ensure arb engine is on.",
                ))

        if snap.reward_eligible_markets and len(snap.reward_eligible_markets) > 10:
            recs.append(StrategyRecommendation(
                lever="scanner.max_concurrent_markets",
                current=self._cfg.scanner.max_concurrent_markets,
                recommended=min(50, max(self._cfg.scanner.max_concurrent_markets, 25)),
                confidence=0.3,
                justification=(
                    f"{len(snap.reward_eligible_markets)} reward-eligible markets "
                    "available — consider scaling concurrent markets."
                ),
            ))

        return recs

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


def classify_trader(trades: List[dict]) -> tuple:
    """Return (style, avg_hold_sec, taker_ratio, n_markets).

    Heuristic:
      - taker_ratio < 0.3 + many markets → market_maker
      - short holding + many markets + near-pair trades → arbitrageur
      - else directional / mixed
    """
    if not trades:
        return ("unknown", None, None, 0)
    sides_taker = [t for t in trades if str(t.get("side", "")).lower() == "taker" or t.get("is_taker")]
    taker_ratio = len(sides_taker) / max(len(trades), 1)

    markets = {t.get("tokenId") or t.get("asset") or t.get("market") for t in trades}
    markets.discard(None)
    n_markets = len(markets)

    # Extract timestamps (may be missing).
    ts_list = []
    for t in trades:
        ts = t.get("timestamp") or t.get("createdAt") or t.get("blockTime")
        try:
            ts_list.append(float(ts))
        except (TypeError, ValueError):
            continue
    hold = None
    if len(ts_list) >= 2:
        ts_list.sort()
        diffs = [ts_list[i + 1] - ts_list[i] for i in range(len(ts_list) - 1)]
        hold = sum(diffs) / len(diffs)

    if taker_ratio < 0.3 and n_markets >= 5:
        return ("market_maker", hold, taker_ratio, n_markets)
    if hold is not None and hold < 60 and n_markets >= 3 and taker_ratio > 0.6:
        return ("arbitrageur", hold, taker_ratio, n_markets)
    if taker_ratio > 0.7 and n_markets <= 3:
        return ("directional", hold, taker_ratio, n_markets)
    return ("mixed", hold, taker_ratio, n_markets)


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
        "trader_styles": [asdict(t) for t in snap.trader_styles],
        "recommendations": [asdict(r) for r in snap.recommendations],
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
    if snap.trader_styles:
        lines.append("## Trader style profiling")
        lines.append("| Wallet | Style | PnL | Hold(s) | Taker% | Markets |")
        lines.append("|---|---|---|---|---|---|")
        for t in snap.trader_styles[:15]:
            lines.append(
                f"| `{t.address[:10]}…` | {t.style} | ${t.pnl_usd:,.0f} | "
                f"{int(t.avg_hold_sec or 0)} | "
                f"{(t.taker_ratio or 0) * 100:.0f}% | {t.markets_traded} |"
            )
        lines.append("")
    if snap.recommendations:
        lines.append("## Recommendations (auto-derived)")
        lines.append("| Lever | Current | → | Recommended | Conf | Why |")
        lines.append("|---|---|---|---|---|---|")
        for r in snap.recommendations:
            lines.append(
                f"| `{r.lever}` | {r.current} | → | {r.recommended} | "
                f"{r.confidence:.0%} | {r.justification} |"
            )
        lines.append("")
    return "\n".join(lines)
