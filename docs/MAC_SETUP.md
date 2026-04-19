# Mac setup — running the 7-day paper trial

Works on any Mac (MacBook, Mac Mini, iMac). Paper mode only — no real money,
no wallet required. The bot reads live Polymarket books and simulates fills.

## 0. Install once

```bash
# Python 3.12+ (Mac system Python works, but homebrew is cleaner)
brew install python@3.12

# Clone and install
git clone <your-fork> polybot && cd polybot
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 1. Start the trial

```bash
python -m polybot trial --days 7
```

That one command:
- Reads real Polymarket books via WebSocket.
- Simulates fills against them with a $100 paper bankroll.
- Every **3 hours**: runs a research pass, tunes parameters, writes a
  progress report to `state/trial/progress-<timestamp>.md` and updates
  `state/trial/progress-latest.md`.
- After 7 days (168 hours): writes `state/trial/final-report.md` and exits.

## 2. Keep it running

### Option A — Mac Mini / always-on Mac (recommended)

Use `launchd` so macOS auto-starts the bot on boot and auto-restarts on
crash. Save this as `~/Library/LaunchAgents/com.polybot.trial.plist`,
editing the two `PATH_TO_POLYBOT` placeholders:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>          <string>com.polybot.trial</string>
  <key>WorkingDirectory</key><string>PATH_TO_POLYBOT</string>
  <key>ProgramArguments</key>
  <array>
    <string>PATH_TO_POLYBOT/.venv/bin/python</string>
    <string>-m</string>
    <string>polybot</string>
    <string>trial</string>
    <string>--days</string>
    <string>7</string>
  </array>
  <key>RunAtLoad</key>      <true/>
  <key>KeepAlive</key>      <true/>
  <key>StandardOutPath</key><string>PATH_TO_POLYBOT/state/polybot.out.log</string>
  <key>StandardErrorPath</key><string>PATH_TO_POLYBOT/state/polybot.err.log</string>
</dict>
</plist>
```

Then load it:

```bash
launchctl load ~/Library/LaunchAgents/com.polybot.trial.plist
# check it's running:
launchctl list | grep polybot
```

To stop it:
```bash
launchctl unload ~/Library/LaunchAgents/com.polybot.trial.plist
```

Also prevent the Mac from sleeping:
- **System Settings → Lock Screen → Turn display off when inactive** =
  acceptable (display sleep is fine).
- **System Settings → Energy / Battery → Prevent automatic sleeping** = ON.
- If it's a MacBook you're closing the lid on: `caffeinate -s -i -d` in a
  background terminal, or use the "Amphetamine" app, otherwise macOS puts
  background processes to sleep.

### Option B — Laptop, lid-open

```bash
caffeinate -s -i -d python -m polybot trial --days 7
```

`caffeinate` prevents system/idle/display sleep while the bot runs. Kill
it with Ctrl-C.

## 3. Monitor progress

```bash
# Live logs:
tail -f state/polybot.out.log

# Bot health:
curl -s localhost:8080/status | jq

# Latest progress report:
cat state/trial/progress-latest.md

# All checkpoints:
ls state/trial/

# Prometheus metrics (optional):
curl -s localhost:9464/metrics | head -30
```

## 4. Migrating from laptop to Mac Mini mid-trial

Trial state is fully on disk, so it's portable:

```bash
# On the laptop (bot stopped):
tar czf polybot-state.tgz state/

# Copy to the Mac Mini (same repo checkout), then:
tar xzf polybot-state.tgz
python -m polybot trial --days 7   # resumes from the existing state.json
```

The trial's `start_ts` is persisted, so "remaining hours" is preserved
across the move. Don't pass `--fresh` unless you really want to restart.

## 5. Starting fresh

```bash
rm -rf state/trial state/polybot.sqlite state/book_history.sqlite
python -m polybot trial --days 7 --fresh
```

## 6. Stopping early

Ctrl-C (or `launchctl unload`) gracefully cancels all resting simulated
orders and writes a checkpoint. Relaunching without `--fresh` resumes.

## 7. What the bot does on your machine

- Opens outbound HTTPS to `clob.polymarket.com`, `gamma-api.polymarket.com`,
  `data-api.polymarket.com`.
- Opens outbound WebSocket to `ws-subscriptions-clob.polymarket.com`.
- Binds **loopback only** (127.0.0.1) on ports `8080` (health) and `9464`
  (Prometheus) — nothing exposed to the network.
- Writes to `./state/` (SQLite + markdown reports). Grows roughly
  **10–30 MB per day** (book history snapshots with 72h retention).
- CPU: under 5% on a modern Mac; memory: under 150 MB steady-state.
- No credentials required. No wallet. No real money can move.
