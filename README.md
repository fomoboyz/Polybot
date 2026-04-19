# Polybot — Autonomous Polymarket Trading Bot

Polybot is a self-managing Polymarket trading agent built to run 24/7, designed
to **maximize maker volume with minimal inventory loss** while **capturing LP
rewards and risk-free arbitrage profit**, and to **self-improve** via a paper
trading harness, a 6-hour auto-research loop, and an auto-tuner.

> ⚠️ **Real money, real risk.** Prediction markets are volatile, orders can fill
> adversely, smart-contract and platform risk exists, and resolution can go
> against you. Run on a funded Polymarket account you control, start in
> `--paper` mode, validate 24h, only then go live with a small bankroll.

---

## What's inside

### Four profit vectors

1. **Reward-driven market making (primary engine)**  
   Polymarket pays a ~$5M/month LP pool (concentrated in sports + live
   markets). Scoring is quadratic in distance-to-mid with a one-sided penalty
   `c ≈ 3`. Polybot quotes two-sided at the tightest spread its risk budget
   allows, then steps inside competitors via `join_if_wider_bps`.

2. **Avellaneda-Stoikov inventory-aware pricing**  
   On top of the reward optimum, the mid is shifted by `-γ σ² q` (q = net
   inventory). When long, both bid and ask move down → we sell more, buy
   less. Shuts off adverse selection before it bites.

3. **Intra-market YES+NO arbitrage**  
   Fires FOK on both legs when `ask(YES) + ask(NO) + fees < $1`. Risk-free
   modulo resolution + platform risk.

4. **Adaptive spread / volatility-scaled quotes**  
   Fast markets get wider quotes; calm markets get tight ones. Half-spread
   = max(target, σ × multiplier).

### Safety layers (bulletproof in depth)

- **Hard risk caps** — total notional, per-market notional, min order size,
  inventory skew ceiling.
- **Kill switch** — daily loss AND lifetime drawdown-from-peak. File-based,
  persists across restarts.
- **Circuit breakers** — error-burst, stale-feed, crash-detection (many
  markets moving simultaneously), API-failure-rate. Trip → cancel all →
  cooldown.
- **Graceful shutdown** — SIGTERM cancels every open order before exit.

### Self-improvement loop

- **Paper mode (`--paper`)** — real live order books from the public CLOB
  endpoint + in-memory fill simulator. Tracks the same metrics (volume, fill
  rate, PnL, estimated reward score) against real market moves. No wallet
  required. This is how you free-test the bot for 24h.
- **6h auto-research** — pulls top wallets (leaderboard), ranks markets by
  volume + liquidity + reward-eligibility, writes `state/research/*.md` +
  `latest.json`.
- **Auto-tuner** — every `evaluation_window_hours`, reads recent fills and
  nudges `target_spread_bps` toward the configured `target_fill_rate`. In
  paper/dry-run mode it can write config.yaml directly; in live mode it only
  recommends.
- **Copy-trading watchlist** — opt-in module mirrors new entries from the
  top-N wallets in the research snapshot, capped at `mirror_size_usd` and
  validated through the same risk manager as everything else.

---

## Architecture

```
            ┌───────────────────────── bot.py (loop) ────────────────────────┐
            │ safety → scan → book → vol → breaker → strategies → research  │
            └──┬───────────┬──────────────┬────────────────┬────────────┬───┘
               │           │              │                │            │
   ┌───────────▼──┐ ┌──────▼──────┐ ┌─────▼────────┐ ┌─────▼─────┐ ┌────▼────┐
   │  scanner.py  │ │ strategies/ │ │ research.py  │ │ tuner.py  │ │ copy_   │
   │ (gamma API,  │ │ market_maker│ │ (6h jobs)    │ │(auto-tune)│ │ trading │
   │ ranked)      │ │ arbitrage   │ └──────────────┘ └───────────┘ └─────────┘
   └───────┬──────┘ │ mean_revn   │
           │        └──────┬──────┘
           │               │
   ┌───────▼──────┐ ┌──────▼──────┐ ┌──────────────┐ ┌──────────────┐
   │  clob.py     │ │ executor.py │ │ risk.py      │ │ state.py     │
   │ live/paper/  │ │ place/cancel│ │ caps + kill  │ │ SQLite:      │
   │ dry-run      │ │ amend       │ │ switch       │ │ orders/fills │
   └──────────────┘ └─────────────┘ └──────────────┘ └──────────────┘
           │
   ┌───────▼──────┐ ┌──────────────┐ ┌──────────────┐
   │  paper.py    │ │ volatility.py│ │ circuit_     │
   │ fill sim     │ │ σ tracker    │ │ breaker.py   │
   └──────────────┘ └──────────────┘ └──────────────┘
```

---

## Quickstart

### 1. Install

```bash
git clone <your-fork>
cd Polybot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Config

```bash
cp config.example.yaml config.yaml
cp .env.example .env   # only needed for live mode
```

### 3. Free 24h test (paper mode, no wallet)

Streams real Polymarket order books, simulates fills, tracks full accounting.

```bash
python -m polybot --paper
```

Let it run, then report:

```bash
python -m polybot report --hours 24
```

Inspect `state/reports/*.md` for volume, fill rate, PnL, per-market breakdown.

### 4. One-shot research pass

```bash
python -m polybot research
```

Writes `state/research/<timestamp>.md` (human) and `latest.json` (machine).
When the bot is running it does this automatically every
`research.interval_hours`.

### 5. Live

```bash
# Fund Polygon-side proxy wallet with USDC.e, fill in .env, then:
python -m polybot run
```

### 6. 24/7 deployment

- `docker compose up -d`
- or copy `deploy/systemd/polybot.service` to `/etc/systemd/system/` and
  `systemctl enable --now polybot`.

---

## Config reference (changes vs. defaults)

| Field | Purpose |
|---|---|
| `risk.max_daily_loss_usd` | Kill-switch trigger (today) |
| `risk.max_drawdown_from_peak_usd` | Kill-switch trigger (peak-to-trough) |
| `risk.max_inventory_skew` | \|YES − NO shares\| before we stop one side |
| `market_maker.use_inventory_skew` | Turn on Avellaneda-Stoikov pricing |
| `market_maker.inventory_risk_aversion` | γ — how much we lean against inventory |
| `market_maker.use_adaptive_spread` | Widen spread when σ is high |
| `market_maker.sigma_to_spread_multiplier` | σ → half-spread coefficient |
| `breaker.crash_bps_per_min` | Per-market move size that counts as a "crash" |
| `breaker.crash_market_share` | Fraction of markets that must crash to trip |
| `research.interval_hours` | Auto-research cadence (default 6) |
| `research.auto_apply` | Tuner writes config.yaml (paper mode only) |
| `tuner.target_fill_rate` | Tuner targets this — too low → narrow spread, too high → widen |
| `copy_trading.enabled` | Opt-in; mirrors top wallets' trades |

---

## Operational playbook

1. **Day 1:** `--paper` for 24h. Run `report --hours 24`. Expect a volume
   number and a small PnL in either direction. If PnL ≪ 0, widen
   `target_spread_bps` and repeat.
2. **Day 2:** Enable `tuner.enabled: true` + `research.auto_apply: true`.
   Let the system iterate on itself for 48h.
3. **Day 3:** Go live with a small bankroll. Keep default risk caps.
4. **Ongoing:** Watch `state/research/latest.json` and `state/reports/*.md`.
   Enable `copy_trading` only after you've manually vetted the top wallets.

---

## Testing

```bash
python -m pytest tests/
```

---

## Sources

- [py-clob-client README](https://github.com/Polymarket/py-clob-client/blob/main/README.md)
- [Polymarket Liquidity Rewards docs](https://docs.polymarket.com/market-makers/liquidity-rewards)
- [Gamma Markets API](https://docs.polymarket.com/developers/gamma-markets-api/get-markets)
- [CLOB methods overview](https://docs.polymarket.com/developers/CLOB/clients/methods-overview)
- [Avellaneda-Stoikov (2008) — High-frequency trading in a limit order book](https://people.orie.cornell.edu/sfs33/LimitOrderBook.pdf)
- [Beyond Simple Arbitrage (Medium)](https://medium.com/illumination/beyond-simple-arbitrage-4-polymarket-strategies-bots-actually-profit-from-in-2026-ddacc92c5b4f)
- [Polymarket LP rewards — two-week deep dive](https://medium.com/@wanguolin/my-two-week-deep-dive-into-polymarket-liquidity-rewards-a-technical-postmortem-88d3a954a058)
