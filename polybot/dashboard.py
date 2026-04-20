"""Local read-only web dashboard.

Serves a single HTML page at `/` plus small JSON endpoints at `/api/*`
from the same :8080 HTTP server the health checks use. The page shows
balance, positions, strategy params, recent fills, trial progress,
tuner history, and the latest research snapshot — everything a human
needs to monitor a paper trial without digging through files.

Read-only by design: no buttons that can cancel orders, modify config,
or move money. Someone with `curl localhost:8080` can only observe.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy import create_engine, desc, select
from sqlalchemy.orm import Session

from .circuit_breaker import CircuitBreaker
from .config import Config
from .executor import OrderExecutor
from .risk import RiskManager
from .state import FillRow, OrderRow
from .trial import TrialRunner

log = logging.getLogger(__name__)

TUNER_LOG = Path("state/tuner.log")
RESEARCH_LATEST = Path("state/research/latest.json")
DEFAULT_DB = "state/polybot.sqlite"


class Dashboard:
    """Aggregates live runtime state into JSON-ready dicts for the UI."""

    RESOURCES = (
        "overview", "positions", "strategies", "fills",
        "resting", "checkpoints", "tuner", "research",
    )

    def __init__(
        self,
        cfg: Config,
        risk: RiskManager,
        executor: OrderExecutor,
        breaker: CircuitBreaker,
        mode: str,
        trial: Optional[TrialRunner] = None,
        db_path: str = DEFAULT_DB,
    ):
        self._cfg = cfg
        self._risk = risk
        self._executor = executor
        self._breaker = breaker
        self._mode = mode
        self._trial = trial
        self._db_path = db_path

    def resource(self, name: str) -> Optional[dict]:
        handlers = {
            "overview": self.overview,
            "positions": self.positions,
            "strategies": self.strategies,
            "fills": self.fills,
            "resting": self.resting,
            "checkpoints": self.checkpoints,
            "tuner": self.tuner_history,
            "research": self.research,
        }
        fn = handlers.get(name)
        if fn is None:
            return None
        try:
            return {"data": fn()}
        except Exception as e:
            log.exception("dashboard.%s failed", name)
            return {"error": str(e)}

    # ---- sections ----

    def overview(self) -> dict:
        r = self._risk.state
        drawdown = max(0.0, r.peak_pnl_lifetime - r.realized_pnl_lifetime)
        starting = self._trial.state.starting_balance_usd if self._trial else None
        current_balance = (starting + r.realized_pnl_lifetime) if starting is not None else None
        pct = (r.realized_pnl_lifetime / starting * 100.0) if starting else None
        out: Dict[str, Any] = {
            "mode": self._mode,
            "starting_balance_usd": starting,
            "current_balance_usd": round(current_balance, 4) if current_balance is not None else None,
            "pnl_today": round(r.realized_pnl_today, 4),
            "pnl_lifetime": round(r.realized_pnl_lifetime, 4),
            "pnl_pct": round(pct, 3) if pct is not None else None,
            "peak_pnl": round(r.peak_pnl_lifetime, 4),
            "drawdown_from_peak": round(drawdown, 4),
            "resting_usd": round(r.resting_notional_usd, 2),
            "positions_usd": round(r.positions_notional_usd, 2),
            "resting_orders": self._executor.resting_count(),
            "n_positions_open": sum(
                1 for p in r.positions.values() if p.net_shares != 0
            ),
            "kill_switch": self._risk.kill_switch_active(),
            "breaker_tripped": self._breaker.tripped(),
            "breaker_reason": self._breaker.reason,
            "risk_limits": {
                "max_notional_usd": self._cfg.risk.max_notional_usd,
                "max_daily_loss_usd": self._cfg.risk.max_daily_loss_usd,
                "max_drawdown_from_peak_usd": self._cfg.risk.max_drawdown_from_peak_usd,
            },
        }
        if self._trial is not None:
            s = self._trial.state
            elapsed = s.hours_elapsed()
            out["trial"] = {
                "started_at": s.start_ts,
                "duration_hours": s.duration_hours,
                "elapsed_hours": round(elapsed, 3),
                "remaining_hours": round(s.hours_remaining(), 3),
                "progress_pct": round(
                    min(100.0, elapsed / s.duration_hours * 100.0), 2,
                ) if s.duration_hours > 0 else 0.0,
                "checkpoint_interval_hours": s.checkpoint_interval_hours,
                "n_checkpoints": len(s.checkpoints),
                "finalized": s.finalized,
            }
        return out

    def positions(self) -> List[dict]:
        out: List[dict] = []
        for tid, pos in self._risk.state.positions.items():
            if pos.net_shares == 0 and pos.realized_pnl == 0:
                continue
            out.append({
                "token_id": tid,
                "token_id_short": tid[:12] + "…",
                "net_shares": round(pos.net_shares, 4),
                "avg_cost": round(pos.avg_cost, 5),
                "realized_pnl": round(pos.realized_pnl, 4),
                "notional_usd": round(abs(pos.net_shares) * pos.avg_cost, 2),
            })
        out.sort(key=lambda r: abs(r["net_shares"]), reverse=True)
        return out

    def strategies(self) -> List[dict]:
        cfg = self._cfg
        mm = cfg.market_maker
        return [
            {
                "name": "Market maker",
                "enabled": mm.enabled,
                "summary": (
                    f"Places passive buy/sell quotes {mm.target_spread_bps:.0f} bps "
                    f"wide around mid, in ${mm.quote_size_usd:.0f} clips, refreshed "
                    f"every {mm.requote_interval_sec:.0f}s or when mid drifts "
                    f"{mm.requote_drift_bps:.0f} bps. Harvests Polymarket LP rewards "
                    f"and captures the spread."
                ),
                "detail": (
                    (
                        "Skews quotes toward flat inventory "
                        f"(aversion {mm.inventory_risk_aversion:.2f}). "
                        if mm.use_inventory_skew else ""
                    ) + (
                        "Widens spread when volatility rises."
                        if mm.use_adaptive_spread else ""
                    )
                ).strip(),
            },
            {
                "name": "Arbitrage",
                "enabled": cfg.arbitrage.enabled,
                "summary": (
                    f"Buys YES + NO when their prices sum to <$1 by at least "
                    f"{cfg.arbitrage.min_profit_bps:.0f} bps net of "
                    f"{cfg.arbitrage.taker_fee_bps:.0f} bps fees. "
                    f"Up to ${cfg.arbitrage.max_size_per_arb_usd:.0f} per trade. "
                    f"Risk-free if filled."
                ),
                "detail": "",
            },
            {
                "name": "Mean reversion",
                "enabled": cfg.mean_reversion.enabled,
                "summary": (
                    f"Fades moves that stretch more than "
                    f"{cfg.mean_reversion.zscore_trigger:.1f}σ from the "
                    f"{cfg.mean_reversion.window_sec:.0f}s rolling average, "
                    f"taking ${cfg.mean_reversion.position_size_usd:.0f} positions."
                ),
                "detail": "",
            },
            {
                "name": "Ladder",
                "enabled": cfg.ladder.enabled,
                "summary": (
                    f"Stacks {cfg.ladder.levels} quote levels per side, each "
                    f"{cfg.ladder.step_bps:.0f} bps deeper than the last. "
                    f"Each level is {int(cfg.ladder.size_decay*100)}% the size of "
                    f"the one before it. Lets the bot earn from multiple price "
                    f"points at once."
                ),
                "detail": "",
            },
            {
                "name": "Allocator",
                "enabled": cfg.allocator.enabled,
                "summary": (
                    f"Splits the bankroll across markets proportional to expected "
                    f"reward score (tighter × bigger quotes rank higher). "
                    f"Minimum ${cfg.allocator.min_per_market_usd:.0f} per market "
                    f"or skip it."
                ),
                "detail": "",
            },
            {
                "name": "Copy trading",
                "enabled": cfg.copy_trading.enabled,
                "summary": (
                    f"Mirrors up to {cfg.copy_trading.max_wallets_to_follow} top "
                    f"performing wallets with ${cfg.copy_trading.mirror_size_usd:.0f} "
                    f"positions. Off by default — enable after research validates "
                    f"which wallets are profitable."
                ),
                "detail": "",
            },
        ]

    def fills(self, limit: int = 50) -> List[dict]:
        if not Path(self._db_path).exists():
            return []
        engine = create_engine(f"sqlite:///{self._db_path}")
        out: List[dict] = []
        with Session(engine) as s:
            rows = list(s.scalars(
                select(FillRow).order_by(desc(FillRow.filled_at)).limit(limit)
            ))
            for f in rows:
                out.append({
                    "ts": f.filled_at,
                    "token_id_short": f.token_id[:12] + "…",
                    "side": f.side,
                    "price": round(f.price, 4),
                    "shares": round(f.shares, 4),
                    "notional_usd": round(f.price * f.shares, 2),
                })
        return out

    def resting(self) -> List[dict]:
        out: List[dict] = []
        for (tid, side, slot), ro in self._executor._resting.items():
            q = ro.placed.quote
            out.append({
                "token_id_short": tid[:12] + "…",
                "side": side,
                "slot": slot,
                "price": round(q.price, 4),
                "size": round(q.size, 2),
                "notional_usd": round(q.price * q.size, 2),
                "age_sec": round(time.time() - ro.placed.created_at, 1),
            })
        out.sort(key=lambda r: (r["token_id_short"], r["side"], r["slot"]))
        return out

    def checkpoints(self) -> List[dict]:
        if self._trial is None:
            return []
        s = self._trial.state
        out: List[dict] = []
        for i, cp in enumerate(s.checkpoints, 1):
            hours_in = (cp["ts"] - s.start_ts) / 3600.0
            out.append({
                "n": i,
                "ts": cp["ts"],
                "hours_in": round(hours_in, 2),
                "pnl_today": round(cp.get("pnl_today", 0.0), 4),
                "pnl_lifetime": round(cp.get("pnl_lifetime", 0.0), 4),
                "fills": cp.get("fills", 0),
                "volume_usd": round(cp.get("volume_usd", 0.0), 2),
                "resting_orders": cp.get("resting_orders", 0),
            })
        return out

    def tuner_history(self, limit: int = 30) -> List[dict]:
        if not TUNER_LOG.exists():
            return []
        lines = TUNER_LOG.read_text().strip().splitlines()
        out: List[dict] = []
        for raw in lines[-limit:]:
            ts_str, _, rest = raw.partition(" ")
            try:
                ts = int(ts_str)
            except ValueError:
                continue
            # rest is "{changes_dict} reason..."
            changes_end = rest.find("}")
            if changes_end == -1:
                continue
            changes_str = rest[: changes_end + 1]
            reason = rest[changes_end + 1 :].strip()
            try:
                # AutoTuner logs the dict with repr(); safe eval via literal_eval.
                import ast
                changes = ast.literal_eval(changes_str)
            except Exception:
                changes = {"_raw": changes_str}
            out.append({"ts": ts, "changes": changes, "reason": reason})
        out.reverse()  # newest first
        return out

    def research(self) -> dict:
        if not RESEARCH_LATEST.exists():
            return {}
        try:
            snap = json.loads(RESEARCH_LATEST.read_text())
        except Exception as e:
            return {"error": str(e)}
        # Trim heavy lists for the UI — we only want highlights.
        top_wallets = snap.get("top_wallets", [])[:10]
        top_markets = snap.get("top_markets_by_volume", [])[:10]
        rewarded = snap.get("reward_eligible_markets", [])[:10]
        recs = snap.get("recommendations", [])
        return {
            "generated_at": snap.get("generated_at"),
            "n_wallets": len(snap.get("top_wallets", [])),
            "n_markets": len(snap.get("top_markets_by_volume", [])),
            "n_rewarded": len(snap.get("reward_eligible_markets", [])),
            "top_wallets": [
                {
                    "address_short": w.get("address", "")[:10] + "…",
                    "pnl_usd": w.get("pnl_usd"),
                    "volume_usd": w.get("volume_usd"),
                    "trades": w.get("trades"),
                }
                for w in top_wallets
            ],
            "top_markets": [
                {
                    "question": m.get("question", "")[:80],
                    "volume_24h_usd": m.get("volume_24h_usd"),
                    "liquidity_usd": m.get("liquidity_usd"),
                }
                for m in top_markets
            ],
            "rewarded_markets": [
                {
                    "question": m.get("question", "")[:80],
                    "liquidity_usd": m.get("liquidity_usd"),
                }
                for m in rewarded
            ],
            "recommendations": recs,
            "notes": snap.get("notes", []),
        }


# ---------------------------------------------------------------------------
# Embedded HTML. Served at `/`. Vanilla JS + fetch; auto-refreshes every 5s.
# ---------------------------------------------------------------------------

HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Polybot</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  :root {
    --bg: #0b0e14;
    --panel: #141923;
    --panel2: #1b2230;
    --text: #e6edf3;
    --muted: #8b98a9;
    --green: #3fb950;
    --red: #f85149;
    --amber: #d29922;
    --blue: #58a6ff;
    --border: #30363d;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text",
      "Segoe UI", Helvetica, Arial, sans-serif;
    background: var(--bg); color: var(--text); font-size: 14px;
  }
  header {
    display: flex; align-items: center; gap: 16px;
    padding: 14px 20px; border-bottom: 1px solid var(--border);
    background: var(--panel); position: sticky; top: 0; z-index: 10;
  }
  header h1 { margin: 0; font-size: 18px; letter-spacing: 0.5px; }
  .badge {
    padding: 3px 10px; border-radius: 4px; font-size: 11px;
    font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px;
  }
  .badge-paper { background: rgba(88,166,255,.15); color: var(--blue); }
  .badge-live { background: rgba(248,81,73,.15); color: var(--red); }
  .badge-dry { background: rgba(210,153,34,.15); color: var(--amber); }
  .badge-ok { background: rgba(63,185,80,.15); color: var(--green); }
  .badge-bad { background: rgba(248,81,73,.15); color: var(--red); }
  .badge-warn { background: rgba(210,153,34,.15); color: var(--amber); }
  .spacer { flex: 1; }
  main { max-width: 1320px; margin: 0 auto; padding: 20px; }
  section {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 8px; padding: 16px 18px; margin-bottom: 16px;
  }
  h2 { margin: 0 0 12px 0; font-size: 14px; text-transform: uppercase;
       letter-spacing: 0.8px; color: var(--muted); font-weight: 600; }
  .grid {
    display: grid; gap: 14px;
    grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  }
  .metric {
    background: var(--panel2); border-radius: 6px; padding: 12px 14px;
  }
  .metric .label { color: var(--muted); font-size: 11px; text-transform: uppercase;
                   letter-spacing: 0.6px; }
  .metric .value { font-size: 20px; font-weight: 600; margin-top: 4px; }
  .metric .sub { color: var(--muted); font-size: 11px; margin-top: 2px; }
  .pos { color: var(--green); }
  .neg { color: var(--red); }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 7px 8px; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-weight: 500; font-size: 11px;
       text-transform: uppercase; letter-spacing: 0.5px; }
  tr:last-child td { border-bottom: none; }
  td.num { font-variant-numeric: tabular-nums; text-align: right; }
  th.num { text-align: right; }
  .progress {
    height: 6px; background: var(--panel2); border-radius: 3px; overflow: hidden;
    margin: 8px 0;
  }
  .progress > div { height: 100%; background: var(--blue); transition: width 0.6s; }
  .two-col { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  @media (max-width: 900px) { .two-col { grid-template-columns: 1fr; } }
  .strat-row {
    display: flex; align-items: flex-start; gap: 12px; padding: 10px 0;
    border-bottom: 1px solid var(--border);
  }
  .strat-row:last-child { border-bottom: none; }
  .strat-body { flex: 1; min-width: 0; }
  .strat-name { font-weight: 600; margin-bottom: 2px; }
  .strat-summary { color: var(--text); font-size: 13px; line-height: 1.45; }
  .strat-detail { color: var(--muted); font-size: 12px; line-height: 1.4;
                  margin-top: 3px; }
  .empty { color: var(--muted); font-style: italic; padding: 8px 0; }
  .rec { background: var(--panel2); padding: 10px 12px; border-radius: 6px;
         margin: 6px 0; }
  .rec-lever { font-weight: 600; }
  .rec-change { color: var(--amber); }
  .note { color: var(--muted); padding: 4px 0; }
  #footer { color: var(--muted); text-align: center; padding: 20px 0 10px;
            font-size: 11px; }
</style>
</head>
<body>
<header>
  <h1>POLYBOT</h1>
  <span id="mode-badge" class="badge badge-paper">—</span>
  <span id="health-badge" class="badge badge-ok">—</span>
  <span id="kill-badge" class="badge badge-ok" style="display:none">KILL</span>
  <span id="breaker-badge" class="badge badge-warn" style="display:none">BREAKER</span>
  <div class="spacer"></div>
  <span class="badge" style="background:var(--panel2); color:var(--muted)">
    <span id="last-update">—</span>
  </span>
</header>

<main>
  <section>
    <h2>Balance & PnL</h2>
    <div class="grid" id="overview-grid"></div>
  </section>

  <section id="trial-section" style="display:none">
    <h2>Trial progress</h2>
    <div class="progress"><div id="trial-bar" style="width:0%"></div></div>
    <div id="trial-info" class="note">—</div>
  </section>

  <div class="two-col">
    <section>
      <h2>Strategies</h2>
      <div id="strategies-list">—</div>
    </section>
    <section>
      <h2>Positions</h2>
      <div id="positions-wrap">—</div>
    </section>
  </div>

  <div class="two-col">
    <section>
      <h2>Resting orders</h2>
      <div id="resting-wrap">—</div>
    </section>
    <section>
      <h2>Recent fills</h2>
      <div id="fills-wrap">—</div>
    </section>
  </div>

  <section>
    <h2>Tuner changes — how the bot has self-improved</h2>
    <div id="tuner-wrap">—</div>
  </section>

  <section>
    <h2>Latest research analysis</h2>
    <div id="research-wrap">—</div>
  </section>

  <section>
    <h2>Checkpoint history</h2>
    <div id="checkpoints-wrap">—</div>
  </section>

  <div id="footer">
    Local read-only dashboard · auto-refresh 5s · paper mode = no real money
  </div>
</main>

<script>
const fmt = {
  usd: v => (v == null ? "—" : (v >= 0 ? "+" : "") + "$" + v.toFixed(2)),
  usdBare: v => (v == null ? "—" : "$" + v.toFixed(2)),
  pct: v => (v == null ? "—" : (v >= 0 ? "+" : "") + v.toFixed(2) + "%"),
  num: (v, d = 2) => (v == null ? "—" : v.toFixed(d)),
  cls: v => (v == null ? "" : (v >= 0 ? "pos" : "neg")),
  timeAgo: ts => {
    if (!ts) return "—";
    const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
    if (s < 60) return s + "s ago";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    if (s < 86400) return Math.floor(s / 3600) + "h ago";
    return Math.floor(s / 86400) + "d ago";
  },
  timeUtc: ts => ts ? new Date(ts * 1000).toISOString().replace("T"," ").slice(0,16) + " UTC" : "—",
};

async function get(path) {
  const r = await fetch(path, {cache: "no-store"});
  if (!r.ok) throw new Error(path + " → " + r.status);
  const j = await r.json();
  if (j.error) throw new Error(j.error);
  return j.data;
}

function metric(label, value, cls, sub) {
  return `<div class="metric">
    <div class="label">${label}</div>
    <div class="value ${cls || ""}">${value}</div>
    ${sub ? `<div class="sub">${sub}</div>` : ""}
  </div>`;
}

async function renderOverview() {
  const d = await get("/api/overview");
  // Top badges.
  const mb = document.getElementById("mode-badge");
  mb.textContent = d.mode;
  mb.className = "badge " + (d.mode === "LIVE" ? "badge-live" :
                             d.mode === "PAPER" ? "badge-paper" : "badge-dry");
  const hb = document.getElementById("health-badge");
  if (d.kill_switch || d.breaker_tripped) {
    hb.textContent = "UNHEALTHY"; hb.className = "badge badge-bad";
  } else { hb.textContent = "HEALTHY"; hb.className = "badge badge-ok"; }
  document.getElementById("kill-badge").style.display =
    d.kill_switch ? "inline" : "none";
  const bb = document.getElementById("breaker-badge");
  bb.style.display = d.breaker_tripped ? "inline" : "none";
  bb.textContent = "BREAKER: " + (d.breaker_reason || "tripped");

  const cells = [];
  if (d.current_balance_usd != null) {
    cells.push(metric("Balance",
      fmt.usdBare(d.current_balance_usd),
      fmt.cls(d.pnl_lifetime),
      "started $" + d.starting_balance_usd.toFixed(2)));
  }
  cells.push(metric("Lifetime PnL",
    fmt.usd(d.pnl_lifetime), fmt.cls(d.pnl_lifetime),
    d.pnl_pct != null ? fmt.pct(d.pnl_pct) : null));
  cells.push(metric("Today PnL",
    fmt.usd(d.pnl_today), fmt.cls(d.pnl_today)));
  cells.push(metric("Drawdown from peak",
    fmt.usdBare(d.drawdown_from_peak),
    d.drawdown_from_peak > 0 ? "neg" : "",
    "limit $" + d.risk_limits.max_drawdown_from_peak_usd.toFixed(0)));
  cells.push(metric("Resting notional",
    fmt.usdBare(d.resting_usd), null,
    d.resting_orders + " orders"));
  cells.push(metric("Open positions",
    String(d.n_positions_open), null,
    fmt.usdBare(d.positions_usd) + " notional"));
  cells.push(metric("Peak PnL",
    fmt.usd(d.peak_pnl), "pos"));
  document.getElementById("overview-grid").innerHTML = cells.join("");

  // Trial bar.
  if (d.trial) {
    const ts = document.getElementById("trial-section");
    ts.style.display = "block";
    document.getElementById("trial-bar").style.width = d.trial.progress_pct + "%";
    document.getElementById("trial-info").innerHTML =
      `<b>${d.trial.elapsed_hours.toFixed(1)}h</b> elapsed of
       ${d.trial.duration_hours.toFixed(0)}h
       (${d.trial.progress_pct.toFixed(1)}%) ·
       <b>${d.trial.remaining_hours.toFixed(1)}h</b> remaining ·
       ${d.trial.n_checkpoints} checkpoints ·
       checkpoint every ${d.trial.checkpoint_interval_hours}h
       ${d.trial.finalized ? "· <b>FINALIZED</b>" : ""}`;
  }
}

async function renderStrategies() {
  const rows = await get("/api/strategies");
  if (!rows.length) {
    document.getElementById("strategies-list").innerHTML =
      `<div class="empty">no strategies</div>`; return;
  }
  const html = rows.map(s => {
    const cls = s.enabled ? "badge-ok" : "badge-warn";
    const badge = s.enabled ? "ON" : "off";
    const detail = s.detail ? `<div class="strat-detail">${s.detail}</div>` : "";
    return `<div class="strat-row">
      <span class="badge ${cls}">${badge}</span>
      <div class="strat-body">
        <div class="strat-name">${s.name}</div>
        <div class="strat-summary">${s.summary}</div>
        ${detail}
      </div>
    </div>`;
  }).join("");
  document.getElementById("strategies-list").innerHTML = html;
}

async function renderPositions() {
  const rows = await get("/api/positions");
  const wrap = document.getElementById("positions-wrap");
  if (!rows.length) { wrap.innerHTML = `<div class="empty">no open positions</div>`; return; }
  wrap.innerHTML = `<table><thead><tr>
    <th>Market</th><th class="num">Net shares</th><th class="num">Avg cost</th>
    <th class="num">Notional</th><th class="num">Realized PnL</th>
  </tr></thead><tbody>` + rows.map(r => `<tr>
    <td><code>${r.token_id_short}</code></td>
    <td class="num">${r.net_shares.toFixed(2)}</td>
    <td class="num">${r.avg_cost.toFixed(3)}</td>
    <td class="num">${fmt.usdBare(r.notional_usd)}</td>
    <td class="num ${fmt.cls(r.realized_pnl)}">${fmt.usd(r.realized_pnl)}</td>
  </tr>`).join("") + `</tbody></table>`;
}

async function renderResting() {
  const rows = await get("/api/resting");
  const wrap = document.getElementById("resting-wrap");
  if (!rows.length) { wrap.innerHTML = `<div class="empty">no resting orders</div>`; return; }
  wrap.innerHTML = `<table><thead><tr>
    <th>Market</th><th>Side</th><th class="num">Slot</th><th class="num">Price</th>
    <th class="num">Size</th><th class="num">Notional</th><th class="num">Age</th>
  </tr></thead><tbody>` + rows.slice(0, 40).map(r => `<tr>
    <td><code>${r.token_id_short}</code></td>
    <td>${r.side}</td>
    <td class="num">${r.slot}</td>
    <td class="num">${r.price.toFixed(3)}</td>
    <td class="num">${r.size.toFixed(1)}</td>
    <td class="num">${fmt.usdBare(r.notional_usd)}</td>
    <td class="num">${r.age_sec.toFixed(0)}s</td>
  </tr>`).join("") + `</tbody></table>`;
}

async function renderFills() {
  const rows = await get("/api/fills");
  const wrap = document.getElementById("fills-wrap");
  if (!rows.length) { wrap.innerHTML = `<div class="empty">no fills yet</div>`; return; }
  wrap.innerHTML = `<table><thead><tr>
    <th>When</th><th>Market</th><th>Side</th><th class="num">Price</th>
    <th class="num">Shares</th><th class="num">Notional</th>
  </tr></thead><tbody>` + rows.slice(0, 25).map(r => `<tr>
    <td>${fmt.timeAgo(r.ts)}</td>
    <td><code>${r.token_id_short}</code></td>
    <td>${r.side}</td>
    <td class="num">${r.price.toFixed(3)}</td>
    <td class="num">${r.shares.toFixed(1)}</td>
    <td class="num">${fmt.usdBare(r.notional_usd)}</td>
  </tr>`).join("") + `</tbody></table>`;
}

async function renderTuner() {
  const rows = await get("/api/tuner");
  const wrap = document.getElementById("tuner-wrap");
  if (!rows.length) {
    wrap.innerHTML = `<div class="empty">no tuner changes yet — evaluated every 10 min</div>`;
    return;
  }
  wrap.innerHTML = `<table><thead><tr>
    <th>When</th><th>Changed</th><th>Reason</th>
  </tr></thead><tbody>` + rows.map(r => {
    const changes = Object.entries(r.changes)
      .map(([k, v]) => `<b>${k}</b>=<span class="rec-change">${v}</span>`)
      .join(" · ");
    return `<tr>
      <td>${fmt.timeAgo(r.ts)}</td>
      <td>${changes || "—"}</td>
      <td style="color:var(--muted)">${r.reason || ""}</td>
    </tr>`;
  }).join("") + `</tbody></table>`;
}

async function renderResearch() {
  const d = await get("/api/research");
  const wrap = document.getElementById("research-wrap");
  if (!d || !d.generated_at) {
    wrap.innerHTML = `<div class="empty">no research snapshot yet — first pass on startup</div>`;
    return;
  }
  let html = `<div class="note">Generated ${fmt.timeAgo(d.generated_at)} ·
    scanned ${d.n_wallets} wallets · ${d.n_markets} markets ·
    ${d.n_rewarded} reward-eligible</div>`;
  if (d.recommendations && d.recommendations.length) {
    html += `<h2 style="margin-top:14px">Recommendations</h2>`;
    d.recommendations.forEach(r => {
      html += `<div class="rec">
        <div class="rec-lever">${r.lever}</div>
        <div>current: <code>${r.current}</code> →
             recommended: <span class="rec-change">${r.recommended}</span>
             <span style="color:var(--muted)">(conf ${(r.confidence * 100).toFixed(0)}%)</span></div>
        <div style="color:var(--muted);font-size:12px">${r.justification || ""}</div>
      </div>`;
    });
  }
  if (d.top_markets && d.top_markets.length) {
    html += `<h2 style="margin-top:14px">Top markets by 24h volume</h2><table>
      <thead><tr><th>Question</th><th class="num">24h volume</th>
        <th class="num">Liquidity</th></tr></thead><tbody>`;
    d.top_markets.slice(0, 6).forEach(m => {
      html += `<tr>
        <td>${m.question}</td>
        <td class="num">${fmt.usdBare(m.volume_24h_usd || 0)}</td>
        <td class="num">${fmt.usdBare(m.liquidity_usd || 0)}</td>
      </tr>`;
    });
    html += `</tbody></table>`;
  }
  if (d.notes && d.notes.length) {
    html += `<h2 style="margin-top:14px">Notes</h2>`;
    d.notes.forEach(n => { html += `<div class="note">• ${n}</div>`; });
  }
  wrap.innerHTML = html;
}

async function renderCheckpoints() {
  const rows = await get("/api/checkpoints");
  const wrap = document.getElementById("checkpoints-wrap");
  if (!rows.length) { wrap.innerHTML = `<div class="empty">no checkpoints yet</div>`; return; }
  wrap.innerHTML = `<table><thead><tr>
    <th class="num">#</th><th class="num">Hours in</th><th class="num">PnL lifetime</th>
    <th class="num">Fills</th><th class="num">Volume</th><th class="num">Resting</th>
  </tr></thead><tbody>` + rows.map(r => `<tr>
    <td class="num">${r.n}</td>
    <td class="num">${r.hours_in.toFixed(1)}h</td>
    <td class="num ${fmt.cls(r.pnl_lifetime)}">${fmt.usd(r.pnl_lifetime)}</td>
    <td class="num">${r.fills}</td>
    <td class="num">${fmt.usdBare(r.volume_usd)}</td>
    <td class="num">${r.resting_orders}</td>
  </tr>`).join("") + `</tbody></table>`;
}

async function refresh() {
  try {
    await Promise.all([
      renderOverview(), renderStrategies(), renderPositions(),
      renderResting(), renderFills(), renderTuner(),
      renderResearch(), renderCheckpoints(),
    ]);
    document.getElementById("last-update").textContent =
      "updated " + new Date().toLocaleTimeString();
  } catch (e) {
    document.getElementById("last-update").textContent = "error: " + e.message;
  }
}
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""
