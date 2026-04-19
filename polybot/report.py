"""Aggregates SQLite records into a human-readable performance report.

Usable as a library or via `python -m polybot report --hours 24`.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from .state import FillRow, MetricRow, OrderRow

log = logging.getLogger(__name__)


@dataclass
class Aggregate:
    window_hours: float
    orders_placed: int
    orders_cancelled: int
    fills: int
    filled_volume_usd: float
    realized_pnl: float
    fill_rate: float
    per_token_pnl: Dict[str, float]
    per_token_volume: Dict[str, float]
    latest_metrics: Dict[str, float]


def aggregate(db_path: str = "state/polybot.sqlite", hours: float = 24.0) -> Aggregate:
    # Ensure schema exists even if the DB is fresh/missing.
    from .state import Store
    Store(db_path)
    engine = create_engine(f"sqlite:///{db_path}")
    cutoff = time.time() - hours * 3600.0

    with Session(engine) as s:
        orders = list(s.scalars(select(OrderRow).where(OrderRow.created_at >= cutoff)))
        fills = list(s.scalars(select(FillRow).where(FillRow.filled_at >= cutoff)))
        metrics = list(
            s.scalars(
                select(MetricRow).where(MetricRow.ts >= cutoff).order_by(MetricRow.ts.desc())
            )
        )

    per_token_pnl: Dict[str, float] = defaultdict(float)
    per_token_volume: Dict[str, float] = defaultdict(float)
    latest_fill_price_by_token: Dict[str, Tuple[str, float]] = {}
    running_cost: Dict[str, List[Tuple[float, float, str]]] = defaultdict(list)

    filled_volume = 0.0
    for f in fills:
        notional = f.price * f.shares
        filled_volume += notional
        per_token_volume[f.token_id] += notional
        # Simple weighted-avg realized PnL per token.
        lots = running_cost[f.token_id]
        lots.append((f.shares, f.price, f.side))
        per_token_pnl[f.token_id] += _pnl_contribution(f.side, f.shares, f.price, lots)
        latest_fill_price_by_token[f.token_id] = (f.side, f.price)

    cancelled = sum(1 for o in orders if o.status == "cancelled")
    realized_pnl = sum(per_token_pnl.values())
    fill_rate = (len(fills) / max(len(orders), 1))

    latest: Dict[str, float] = {}
    seen = set()
    for m in metrics:
        if m.name in seen:
            continue
        seen.add(m.name)
        latest[m.name] = m.value

    return Aggregate(
        window_hours=hours,
        orders_placed=len(orders),
        orders_cancelled=cancelled,
        fills=len(fills),
        filled_volume_usd=filled_volume,
        realized_pnl=realized_pnl,
        fill_rate=fill_rate,
        per_token_pnl=dict(per_token_pnl),
        per_token_volume=dict(per_token_volume),
        latest_metrics=latest,
    )


def _pnl_contribution(side: str, shares: float, price: float, lots: list) -> float:
    # Very rough — the Position class does exact accounting; this is just for
    # reports to keep the report tool self-contained.
    if not lots:
        return 0.0
    # Take the average cost of opposite-side lots before this one.
    opp = "BUY" if side == "SELL" else "SELL"
    opp_lots = [(sh, pr) for sh, pr, s in lots[:-1] if s == opp]
    if not opp_lots:
        return 0.0
    total_sh = sum(sh for sh, _ in opp_lots)
    if total_sh <= 0:
        return 0.0
    avg = sum(sh * pr for sh, pr in opp_lots) / total_sh
    closed = min(total_sh, shares)
    if side == "SELL":
        return closed * (price - avg)
    return closed * (avg - price)


def render_markdown(agg: Aggregate, output: Path) -> str:
    lines = []
    lines.append(f"# Polybot Performance — last {agg.window_hours:.1f}h\n")
    lines.append(f"_Generated: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}_\n")
    lines.append("## Summary\n")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Orders placed | {agg.orders_placed} |")
    lines.append(f"| Orders cancelled | {agg.orders_cancelled} |")
    lines.append(f"| Fills | {agg.fills} |")
    lines.append(f"| Filled volume (USD) | ${agg.filled_volume_usd:,.2f} |")
    lines.append(f"| Fill rate | {agg.fill_rate:.1%} |")
    lines.append(f"| Realized PnL | ${agg.realized_pnl:+.4f} |")
    lines.append("")
    if agg.latest_metrics:
        lines.append("## Latest snapshot\n")
        for k, v in agg.latest_metrics.items():
            lines.append(f"- `{k}`: {v}")
        lines.append("")
    if agg.per_token_volume:
        lines.append("## Top markets by volume\n")
        lines.append("| Token | Volume (USD) | PnL (USD) |")
        lines.append("|---|---|---|")
        top = sorted(agg.per_token_volume.items(), key=lambda x: x[1], reverse=True)[:20]
        for tid, vol in top:
            pnl = agg.per_token_pnl.get(tid, 0.0)
            lines.append(f"| `{tid[:12]}…` | ${vol:,.2f} | ${pnl:+.4f} |")
        lines.append("")
    content = "\n".join(lines)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(content)
    return content


def run_report(hours: float = 24.0, db: str = "state/polybot.sqlite") -> Path:
    agg = aggregate(db_path=db, hours=hours)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    out = Path("state/reports") / f"report-{int(hours)}h-{stamp}.md"
    render_markdown(agg, out)
    log.info("report written to %s", out)
    return out
