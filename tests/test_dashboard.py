import json
import time
import urllib.request
from pathlib import Path

import pytest

from polybot.circuit_breaker import BreakerConfig, CircuitBreaker
from polybot.clob import PlacedOrder
from polybot.config import Config
from polybot.dashboard import Dashboard
from polybot.executor import OrderExecutor, RestingOrder
from polybot.health import HealthServer, HealthState
from polybot.models import Quote
from polybot.risk import RiskManager
from polybot.state import Store
from polybot.trial import TrialRunner


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    yield tmp_path


def _build(mode: str = "PAPER", trial=None):
    cfg = Config()
    risk = RiskManager(cfg.risk)
    # Executor needs a clob object only to place orders; we stub it since
    # we inject resting orders directly.
    class _FakeClob: pass
    executor = OrderExecutor(_FakeClob(), risk)
    breaker = CircuitBreaker(BreakerConfig())
    return Dashboard(cfg, risk, executor, breaker, mode=mode, trial=trial), risk, executor


def test_overview_basic(workdir):
    d, risk, _ = _build()
    ov = d.resource("overview")
    assert "data" in ov
    data = ov["data"]
    assert data["mode"] == "PAPER"
    assert data["pnl_lifetime"] == 0.0
    assert data["kill_switch"] is False
    assert data["breaker_tripped"] is False
    assert data["starting_balance_usd"] is None  # no trial attached


def test_overview_with_trial(workdir):
    trial = TrialRunner(duration_hours=168, checkpoint_interval_hours=3)
    d, risk, _ = _build(trial=trial)
    risk.state.realized_pnl_lifetime = 5.0
    risk.state.peak_pnl_lifetime = 7.0
    data = d.resource("overview")["data"]
    assert data["starting_balance_usd"] == 100.0
    assert data["current_balance_usd"] == 105.0
    assert data["pnl_lifetime"] == 5.0
    assert data["drawdown_from_peak"] == 2.0
    assert data["trial"]["duration_hours"] == 168
    assert 0.0 <= data["trial"]["progress_pct"] <= 100.0


def test_positions_lists_nonzero(workdir):
    d, risk, _ = _build()
    # Open a position via fills.
    risk.on_fill("tok-a", "BUY", 10.0, 0.5)
    risk.on_fill("tok-b", "BUY", 5.0, 0.3)
    rows = d.resource("positions")["data"]
    tokens = {r["token_id"] for r in rows}
    assert "tok-a" in tokens and "tok-b" in tokens
    row = next(r for r in rows if r["token_id"] == "tok-a")
    assert row["net_shares"] == 10.0
    assert row["avg_cost"] == 0.5


def test_strategies_reports_enabled_flags(workdir):
    d, _, _ = _build()
    rows = d.resource("strategies")["data"]
    names = {r["name"] for r in rows}
    assert {"Market maker", "Arbitrage", "Ladder", "Allocator"} <= names
    mm = next(r for r in rows if r["name"] == "Market maker")
    assert mm["summary"]  # plain-English description is non-empty
    assert "bps" in mm["summary"]


def test_fills_queries_sqlite(workdir):
    d, _, _ = _build()
    # Seed a fill.
    store = Store("state/polybot.sqlite")
    now = time.time()
    store.record_fill("o1", "tok-a", "BUY", 0.5, 10.0, now)
    rows = d.resource("fills")["data"]
    assert len(rows) == 1
    assert rows[0]["side"] == "BUY"
    assert rows[0]["notional_usd"] == 5.0


def test_fills_empty_when_no_db(workdir):
    d, _, _ = _build()
    assert d.resource("fills")["data"] == []


def test_resting_lists_orders(workdir):
    d, _, executor = _build()
    quote = Quote(token_id="tok-a", side="BUY", price=0.5, size=10.0, slot=0)
    placed = PlacedOrder(order_id="o1", quote=quote, created_at=time.time())
    executor._resting[("tok-a", "BUY", 0)] = RestingOrder(
        placed=placed, midpoint_at_place=0.5,
    )
    rows = d.resource("resting")["data"]
    assert len(rows) == 1
    assert rows[0]["side"] == "BUY"
    assert rows[0]["price"] == 0.5
    assert rows[0]["notional_usd"] == 5.0


def test_checkpoints_from_trial(workdir):
    trial = TrialRunner(duration_hours=168)
    trial.state.checkpoints = [
        {"ts": trial.state.start_ts + 3600, "pnl_today": 1.0,
         "pnl_lifetime": 1.0, "fills": 3, "volume_usd": 50.0, "resting_orders": 4},
    ]
    d, _, _ = _build(trial=trial)
    rows = d.resource("checkpoints")["data"]
    assert len(rows) == 1
    assert rows[0]["n"] == 1
    assert rows[0]["fills"] == 3


def test_tuner_history_parses_log(workdir):
    Path("state").mkdir()
    Path("state/tuner.log").write_text(
        "1700000000 {'target_spread_bps': 45.0} fill_rate=0.1<target/2 → narrow\n"
        "1700003600 {'target_spread_bps': 60.0, 'min_edge_over_mid_bps': 20}"
        " pnl=-$12 → defensive widen\n"
    )
    d, _, _ = _build()
    rows = d.resource("tuner")["data"]
    assert len(rows) == 2
    # Newest first.
    assert rows[0]["ts"] == 1700003600
    assert rows[0]["changes"]["target_spread_bps"] == 60.0
    assert rows[0]["changes"]["min_edge_over_mid_bps"] == 20
    assert "defensive" in rows[0]["reason"]


def test_tuner_history_empty_when_no_file(workdir):
    d, _, _ = _build()
    assert d.resource("tuner")["data"] == []


def test_research_reads_latest(workdir):
    Path("state/research").mkdir(parents=True)
    Path("state/research/latest.json").write_text(json.dumps({
        "generated_at": 1700000000,
        "top_wallets": [{"address": "0xaaaa1111bbbb2222", "pnl_usd": 1000, "volume_usd": 10000, "trades": 50}],
        "top_markets_by_volume": [
            {"question": "Will X happen?", "volume_24h_usd": 50000, "liquidity_usd": 20000},
        ],
        "reward_eligible_markets": [
            {"question": "Rewarded?", "liquidity_usd": 30000},
        ],
        "recommendations": [{
            "lever": "market_maker.target_spread_bps",
            "current": 50, "recommended": 40, "confidence": 0.8,
            "justification": "tighter spreads in deep markets",
        }],
        "notes": ["probe note"],
    }))
    d, _, _ = _build()
    data = d.resource("research")["data"]
    assert data["n_wallets"] == 1
    assert data["top_wallets"][0]["address_short"].startswith("0xaaaa1111")
    assert data["recommendations"][0]["lever"] == "market_maker.target_spread_bps"
    assert data["notes"] == ["probe note"]


def test_research_empty_when_no_file(workdir):
    d, _, _ = _build()
    assert d.resource("research")["data"] == {}


def test_unknown_resource_returns_none(workdir):
    d, _, _ = _build()
    assert d.resource("not_a_thing") is None


# ---- HTTP integration ----

def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_http_serves_dashboard_html(workdir):
    d, _, _ = _build()
    state = HealthState()
    state.attach_dashboard(d)
    port = _free_port()
    srv = HealthServer(state, port=port, host="127.0.0.1")
    srv.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2) as r:
            body = r.read().decode("utf-8")
            assert r.status == 200
            assert "<title>Polybot</title>" in body
            assert "/api/overview" in body  # JS references the endpoints
    finally:
        srv.stop()


def test_http_api_overview(workdir):
    d, _, _ = _build()
    state = HealthState()
    state.attach_dashboard(d)
    port = _free_port()
    srv = HealthServer(state, port=port, host="127.0.0.1")
    srv.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/overview", timeout=2) as r:
            body = json.loads(r.read())
            assert r.status == 200
            assert body["data"]["mode"] == "PAPER"
    finally:
        srv.stop()


def test_http_api_unknown_returns_404(workdir):
    import urllib.error
    d, _, _ = _build()
    state = HealthState()
    state.attach_dashboard(d)
    port = _free_port()
    srv = HealthServer(state, port=port, host="127.0.0.1")
    srv.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/not_a_thing", timeout=2)
        assert ctx.value.code == 404
    finally:
        srv.stop()


def test_http_root_is_404_without_dashboard(workdir):
    import urllib.error
    state = HealthState()  # no dashboard attached
    port = _free_port()
    srv = HealthServer(state, port=port, host="127.0.0.1")
    srv.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=2)
        assert ctx.value.code == 404
    finally:
        srv.stop()
