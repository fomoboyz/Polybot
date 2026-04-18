# Polybot — Autonomous Polymarket Trading Bot

Polybot is a self-managing Polymarket trading agent that runs 24/7 and is designed
to **maximize maker volume with minimal inventory loss** while **capturing LP
rewards and risk-free arbitrage profit**.

> ⚠️ **Real money, real risk.** Prediction markets are volatile, orders can fill
> adversely, smart-contract and platform risk exists, and resolution can go
> against you. Run on a funded Polymarket account you control, start with a
> small bankroll, and read the code before deploying.

---

## Research summary — what actually works on Polymarket in 2026

The research phase (cited sources at the bottom) converged on four profit
vectors. Polybot combines them.

### 1. Reward-driven market making (the core engine)

Polymarket pays a daily pool (currently $5M/month concentrated in sports + live
markets) to resting maker orders. Scoring is **quadratic in spread**: an order
posted at the size-cutoff-adjusted midpoint scores dramatically more than one a
few cents away. Key rules Polybot encodes:

- `Score_per_order ≈ ((max_spread - order_spread) / max_spread)² × size`
- One-sided liquidity is penalized: `Q_min = max(Q_one/c, Q_two/c)` with `c=3`
  when midpoint ∈ [0.10, 0.90], and **requires** two-sided quoting when
  midpoint is in the tails. Polybot always quotes both sides in the book edges.
- Snapshots are taken ~every minute; rewards are paid daily at 00:00 UTC.
- Quadratic scoring means **being tightest wins disproportionately**: Polybot
  tracks competitors' top-of-book and steps inside by 1 tick when its risk
  budget allows.

This is the primary engine: it generates **volume** (reward-eligible markets
are also the deepest markets) and earns **two revenue streams** — the bid/ask
spread captured on fills plus daily LP rewards.

### 2. Intra-market YES / NO arbitrage

On Polymarket, YES and NO for the same binary outcome must sum to $1.00 at
resolution. When `best_ask(YES) + best_ask(NO) < 1 − fees − slippage`, buying
both legs is a **risk-free** position (modulo resolution risk). After the 2%
taker fee this needs a ~2–3% gap; Polybot's arb scanner fires on gaps ≥ the
configured threshold.

### 3. Cross-market / conditional arbitrage

Related markets (e.g. "Will X win primary?" vs "Will X win election?") impose
probabilistic constraints. Polybot runs a secondary scanner for obvious
violations (A⊂B but P(A) > P(B)). Disabled by default; enable only after you
read the logic — it's the easiest to misconfigure.

### 4. Mean reversion on thin markets

Low-info events (obscure sports, long-dated politics) over-react to small
trades. Polybot will **fade** 1-sigma moves with a small capped position,
using the market maker's inventory-skew module to cap exposure.

---

## Architecture

```
          ┌────────────────────────────────────────────────────────┐
          │                      bot.py (loop)                     │
          └───────┬────────────────┬─────────────┬─────────────────┘
                  │                │             │
          ┌───────▼──────┐ ┌───────▼────────┐ ┌──▼─────────────┐
          │  scanner.py  │ │  strategies/   │ │   executor.py  │
          │ (gamma API,  │ │ market_maker   │ │ (place/cancel  │
          │  filters)    │ │ arbitrage      │ │  with retries) │
          └───────┬──────┘ │ mean_reversion │ └──┬─────────────┘
                  │        └───────┬────────┘    │
          ┌───────▼──────┐ ┌───────▼────────┐ ┌──▼─────────────┐
          │  clob.py     │ │   risk.py      │ │   state.py     │
          │ (CLOB REST,  │ │ (limits, kill- │ │  (SQLite: PnL, │
          │ signed EIP712)│ │  switch, skew)│ │  orders, fills)│
          └──────────────┘ └────────────────┘ └────────────────┘
```

- **scanner** discovers reward-eligible, liquid markets via the Gamma API
  and ranks them by `reward_pool / competitive_depth`.
- **reward_optimizer** computes the tightest spread Polybot can post while
  satisfying the risk budget, using the quadratic scoring formula.
- **market_maker** turns that into a pair of limit orders, re-quoted every
  N seconds or when midpoint drifts > `requote_threshold`.
- **arbitrage** scans books in parallel and fires IOC orders when the
  YES+NO sum gap crosses the fee-adjusted threshold.
- **risk** rejects any order that would breach per-market notional, total
  notional, drawdown, or position-skew limits. A kill switch halts all
  strategies if daily PnL breaches `max_daily_loss_usd`.
- **executor** tracks order state, cancels stale orders, handles nonces
  and retries on rate-limit / 5xx.
- **state** persists orders, fills, positions, and PnL to SQLite for
  restart safety.

---

## Quickstart

### 1. Install

```bash
git clone <your-fork>
cd Polybot
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Create a Polymarket account and fund it

Fund your Polygon-side proxy wallet with USDC.e. If you use the Polymarket
web UI (email/Magic signup), pull your proxy-wallet address and export the
private key from settings. `signature_type=1`.

### 3. Configure

```bash
cp .env.example .env          # fill in PK_PRIVATE_KEY + FUNDER
cp config.example.yaml config.yaml
```

Edit `config.yaml` to set risk limits. Defaults are conservative —
`max_notional_usd: 100`, `max_daily_loss_usd: 20`.

### 4. Dry run first

```bash
python -m polybot --dry-run
```

Logs quotes Polybot *would* place. Watch for a few hours. Confirm the scanner
is picking reasonable markets.

### 5. Go live

```bash
python -m polybot
```

### 6. 24/7 deployment

- Docker: `docker compose up -d`
- systemd: copy `deploy/systemd/polybot.service` to `/etc/systemd/system/`,
  `systemctl enable --now polybot`.

---

## Config reference

See `config.example.yaml` — every field is documented inline. The fields that
matter most:

| Field | What it does |
|---|---|
| `risk.max_notional_usd` | Cap on total resting + filled notional |
| `risk.max_per_market_usd` | Cap per individual token_id |
| `risk.max_daily_loss_usd` | Kill-switch trigger |
| `risk.max_inventory_skew` | Max \|YES_size − NO_size\| before we stop one side |
| `market_maker.target_spread_bps` | Desired spread in basis points |
| `market_maker.min_edge_over_mid_bps` | Never cross the mid; never post worse than this |
| `market_maker.requote_drift_bps` | Requote when midpoint moves this much |
| `arbitrage.min_profit_bps` | Min sum-gap to fire (after 2% fee assumption) |

---

## Safety design

Polybot inherits three invariants that are asserted at every tick:

1. **No order is placed if risk.validate() returns False.** All 4 limits above
   are checked pre-flight.
2. **Inventory is bounded per market.** The market maker pulls its bid once
   long inventory exceeds `max_inventory_skew`, and pulls its offer once short.
3. **Kill switch is persistent.** Tripping `max_daily_loss_usd` writes a file
   to `state/KILL`. Restart won't resume until you delete that file.

---

## Sources

- [py-clob-client README](https://github.com/Polymarket/py-clob-client/blob/main/README.md)
- [Polymarket Liquidity Rewards docs](https://docs.polymarket.com/market-makers/liquidity-rewards)
- [Gamma Markets API](https://docs.polymarket.com/developers/gamma-markets-api/get-markets)
- [CLOB methods overview](https://docs.polymarket.com/developers/CLOB/clients/methods-overview)
- [Beyond Simple Arbitrage (Medium)](https://medium.com/illumination/beyond-simple-arbitrage-4-polymarket-strategies-bots-actually-profit-from-in-2026-ddacc92c5b4f)
- [Polymarket Arbitrage — Trade the Outcome](https://www.tradetheoutcome.com/polymarket-strategy-2026/)
- [PolymarketGuide Rewards](https://polymarketguide.gitbook.io/polymarketguide/trading/rewards)
- [Two-week deep dive into LP rewards (Medium)](https://medium.com/@wanguolin/my-two-week-deep-dive-into-polymarket-liquidity-rewards-a-technical-postmortem-88d3a954a058)
