# 7-Day $100 Trial Plan

## Honest baseline — what the research says

Polymarket's minimum order is $1 for market, 5 shares for limit orders — so
$100 is workable, but barely. The key constraints at this size:

1. **LP rewards likely near zero.** The reward formula is quadratic in
   distance-to-mid, and professional MMs with $10k–$100k can always be
   tighter. Expect $0–$10 in LP rewards across the whole week.
2. **$1 minimum payout per market per day.** Many days the bot's score in
   a given market won't clear $1 and rewards are forfeited.
3. **Adverse selection is the real risk.** One bad fill on a thin market
   can wipe a full day of maker-spread capture.

So the real revenue engine at $100 is **captured bid-ask spread + selective
arbitrage**, with LP rewards as a bonus.

## Success criteria for the $500 unlock

| Metric | Floor (no-go) | Pass | Strong |
|---|---|---|---|
| Realized PnL after fees | < −$5 | ≥ −$5 | ≥ +$3 |
| Total volume | < $300 | $500–$1,500 | ≥ $2,000 |
| Max drawdown from peak | > $12 | ≤ $12 | ≤ $6 |
| Kill-switch trips | ≥ 2 | ≤ 1 | 0 |
| Uptime | < 90% | ≥ 95% | ≥ 99% |

If we hit the **Pass** column across the board, the bot has demonstrated
safe operation with meaningful activity and the $500 top-up is justified.
**Strong** means the strategy is actually working and scales.

## 7-day schedule

All timestamps UTC. The bot runs autonomously; the "actions" column is what
*you* do — everything else the bot handles.

| Day | Phase | Your action | Bot config | What to expect |
|---|---|---|---|---|
| **-2** | Paper pre-flight | Deploy VPS / Docker, run `--paper` | `config.100usd.yaml` | Validates wiring on real books, no money at risk |
| **-1** | Paper validation | `polybot report --hours 24` | same | Confirm volume + fill behavior look sane |
| **0** | Go live | Fund wallet with $100 USDC.e on Polygon, start live | `config.100usd.yaml` | First live fills within ~1h |
| **1** | Observation | Watch `/status`, review hourly | unchanged | Daily PnL likely flat ± $2 |
| **2** | First tuner cycle | Check `state/tuner.log` | tuner nudges `target_spread_bps` | Spread widens/tightens 10-20% based on fill rate |
| **3** | Mid-week review | Run backtest sweep (see below) | if PnL ≥ $0, flip `ladder.enabled: true` | Ladders start capturing 2x levels per side |
| **4–5** | Stable op | Daily report check | unchanged | Volume ramps as bot settles |
| **6** | Pre-report | Review `state/research/latest.json` | unchanged | Auto-recommendations inform next config |
| **7** | Post-mortem | Generate 7d report, decide next step | — | Success criteria check |

## Exact commands

### One-time setup

```bash
# VPS (DigitalOcean $5/mo droplet is enough, or any Linux box):
git clone <your-fork> polybot && cd polybot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Create .env from .env.example, fill in PK_PRIVATE_KEY + PK_FUNDER
# (Polymarket → Cash → ⋮ → Export Private Key; funder is your proxy address)
cp .env.example .env && $EDITOR .env

# Use the $100 preset
export POLYBOT_CONFIG=config.100usd.yaml
```

### Paper phase (Day -2 → Day 0)

```bash
python -m polybot --paper   # runs forever; Ctrl-C to stop
# In another shell, after 24h:
python -m polybot report --hours 24
```

### Live phase (Day 0 → Day 7)

```bash
# Under systemd or docker-compose for auto-restart.
docker compose up -d                             # or systemctl start polybot
# Watch logs:
docker compose logs -f polybot
# Health check:
curl localhost:8080/status | jq
# Daily report (run end of each day):
python -m polybot report --hours 24
```

### Mid-week backtest sweep (Day 3)

```bash
# History recorder has been collecting since Day 0. Compare three configs:
python -m polybot backtest --hours 72 --target-spread-bps 30
python -m polybot backtest --hours 72 --target-spread-bps 40
python -m polybot backtest --hours 72 --target-spread-bps 60
# Pick the highest-volume config with PnL ≥ 0 and update config.100usd.yaml
```

## Your concrete next steps (right now)

1. **Spin up a VPS** (any Linux box with 1GB RAM, always-on network).
2. **Clone this repo** and install deps.
3. **Create a Polymarket account** at polymarket.com, complete KYC if
   required, deposit **$100 USDC.e on Polygon**.
4. **Export the private key** (Cash → ⋮ → Export Private Key) and note
   your **proxy wallet address**.
5. **Fill in `.env`** with those two values.
6. **Start in `--paper` mode** for 48h. Share the `state/reports/*.md`
   output so we can validate before going live.
7. Once paper looks clean, **start with `POLYBOT_CONFIG=config.100usd.yaml
   docker compose up -d`** and let it run.
8. **Set up a webhook** (Slack/Discord) for alerts — optional but useful.

## What I can't do and need from you

- I can't fund the wallet or sign transactions — that requires your key.
- I can't run the bot on my side 24/7 — it needs to run on a machine you
  own and control.
- I'll iterate on strategy parameters and code based on paper/live reports
  you share back. Daily `state/reports/*.md` + `state/research/latest.json`
  are enough signal for me to tune.

## Risk disclosures (read once)

- Real prediction markets, real USD. Order fills are final.
- Polymarket is not available in all jurisdictions. Confirm you can legally
  use it.
- A platform outage, smart-contract bug, or market-resolution dispute can
  cause partial/total loss outside the bot's risk controls.
- **Starting bankroll: $100. Worst realistic case in 7 days: lose $12** (the
  drawdown-from-peak kill switch). Typical case: ±$5.
