import json
import os
import time
from pathlib import Path

import pytest

from polybot.config import Config, TunerCfg, MarketMakerCfg, ResearchCfg
from polybot.state import Store
from polybot.tuner import AutoTuner, TUNER_LOG


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # Ensure fresh state dir inside tmp.
    Path("state").mkdir(exist_ok=True)
    yield tmp_path


def _seed_orders_and_fills(n_orders: int, n_fills: int) -> None:
    s = Store("state/polybot.sqlite")
    now = time.time()
    for i in range(n_orders):
        s.record_order(f"o{i}", "tok", "BUY", 0.5, 10, now)
    for i in range(n_fills):
        s.record_fill(f"o{i}", "tok", "BUY", 0.5, 10, now)


def test_tuner_skips_when_low_order_volume(isolated_state):
    _seed_orders_and_fills(n_orders=5, n_fills=0)
    cfg = Config(tuner=TunerCfg(enabled=True, evaluation_window_hours=24))
    t = AutoTuner(cfg, live=False)
    res = t.run()
    # With no research snapshot and too-few orders, tuner has nothing to do.
    assert res is not None
    assert res.applied is False
    assert res.changes == {}


def test_tuner_narrows_spread_on_low_fill_rate(isolated_state):
    _seed_orders_and_fills(n_orders=100, n_fills=2)  # ~2% fill rate
    cfg = Config(
        tuner=TunerCfg(enabled=True, target_fill_rate=0.3, learning_rate=0.5),
        market_maker=MarketMakerCfg(target_spread_bps=100),
        research=ResearchCfg(auto_apply=False),
    )
    t = AutoTuner(cfg, live=False)
    res = t.run()
    assert res is not None
    assert "market_maker.target_spread_bps" in res.changes
    assert res.changes["market_maker.target_spread_bps"] < 100


def test_tuner_widens_spread_on_high_fill_rate(isolated_state):
    _seed_orders_and_fills(n_orders=100, n_fills=80)  # 80%
    cfg = Config(
        tuner=TunerCfg(enabled=True, target_fill_rate=0.3, learning_rate=0.5),
        market_maker=MarketMakerCfg(target_spread_bps=30),
    )
    t = AutoTuner(cfg, live=False)
    res = t.run()
    assert res is not None
    assert res.changes["market_maker.target_spread_bps"] > 30


# ---- research → tuner wiring ----

def _write_research(recs: list) -> None:
    Path("state/research").mkdir(parents=True, exist_ok=True)
    Path("state/research/latest.json").write_text(json.dumps({
        "generated_at": time.time(),
        "top_wallets": [],
        "top_markets_by_volume": [],
        "top_markets_by_liquidity": [],
        "reward_eligible_markets": [],
        "trader_styles": [],
        "recommendations": recs,
        "notes": [],
    }))


def test_tuner_applies_high_confidence_research(isolated_state):
    _write_research([{
        "lever": "market_maker.target_spread_bps",
        "current": 50.0,
        "recommended": 28.0,
        "confidence": 0.8,
        "justification": "top traders market-make at 28bps",
    }])
    cfg = Config(
        tuner=TunerCfg(
            enabled=True, research_confidence_threshold=0.5,
            min_orders_for_feedback=1000,  # force skip local feedback
        ),
        market_maker=MarketMakerCfg(target_spread_bps=50),
        research=ResearchCfg(auto_apply=True),
    )
    t = AutoTuner(cfg, live=False)
    res = t.run()
    assert res is not None
    assert res.applied is True
    assert res.research_changes["market_maker.target_spread_bps"] == 28.0
    # Config was actually mutated.
    assert cfg.market_maker.target_spread_bps == 28.0


def test_tuner_skips_low_confidence_research(isolated_state):
    _write_research([{
        "lever": "market_maker.target_spread_bps",
        "current": 50.0,
        "recommended": 28.0,
        "confidence": 0.2,
        "justification": "weak signal",
    }])
    cfg = Config(
        tuner=TunerCfg(
            enabled=True, research_confidence_threshold=0.5,
            min_orders_for_feedback=1000,
        ),
        market_maker=MarketMakerCfg(target_spread_bps=50),
    )
    t = AutoTuner(cfg, live=False)
    res = t.run()
    assert res is not None
    assert res.applied is False
    assert res.research_changes == {}


def test_tuner_rejects_unknown_lever(isolated_state):
    _write_research([{
        "lever": "secrets.private_key",         # not in allowlist
        "current": None,
        "recommended": "0xdeadbeef",
        "confidence": 0.95,
        "justification": "malicious",
    }])
    cfg = Config(
        tuner=TunerCfg(enabled=True, min_orders_for_feedback=1000),
        research=ResearchCfg(auto_apply=True),
    )
    t = AutoTuner(cfg, live=False)
    res = t.run()
    assert res is not None
    assert res.research_changes == {}


def test_tuner_clips_research_to_bounds(isolated_state):
    # Lever bounds for target_spread_bps are (5, 150). Recommend 500.
    _write_research([{
        "lever": "market_maker.target_spread_bps",
        "current": 50.0,
        "recommended": 500.0,
        "confidence": 0.9,
        "justification": "extreme",
    }])
    cfg = Config(
        tuner=TunerCfg(enabled=True, min_orders_for_feedback=1000),
        research=ResearchCfg(auto_apply=True),
    )
    t = AutoTuner(cfg, live=False)
    res = t.run()
    assert res is not None
    assert res.research_changes["market_maker.target_spread_bps"] == 150.0


def test_tuner_applies_bool_lever(isolated_state):
    _write_research([{
        "lever": "arbitrage.enabled",
        "current": False,
        "recommended": True,
        "confidence": 0.7,
        "justification": "top traders arb",
    }])
    from polybot.config import ArbitrageCfg
    cfg = Config(
        tuner=TunerCfg(enabled=True, min_orders_for_feedback=1000),
        arbitrage=ArbitrageCfg(enabled=False),
        research=ResearchCfg(auto_apply=True),
    )
    t = AutoTuner(cfg, live=False)
    res = t.run()
    assert res is not None
    assert res.research_changes["arbitrage.enabled"] is True
    assert cfg.arbitrage.enabled is True


def test_tuner_research_works_when_fill_feedback_skipped(isolated_state):
    """The critical user-facing behavior: even with tiny order count, the
    tuner still applies research recommendations."""
    _seed_orders_and_fills(n_orders=3, n_fills=0)
    _write_research([{
        "lever": "market_maker.target_spread_bps",
        "current": 50.0,
        "recommended": 35.0,
        "confidence": 0.7,
        "justification": "recommend tighter",
    }])
    cfg = Config(
        tuner=TunerCfg(enabled=True, min_orders_for_feedback=20),
        research=ResearchCfg(auto_apply=True),
    )
    t = AutoTuner(cfg, live=False)
    res = t.run()
    assert res is not None
    assert res.applied is True
    assert res.research_changes["market_maker.target_spread_bps"] == 35.0


def test_tuner_logs_research_changes_to_log(isolated_state):
    _write_research([{
        "lever": "market_maker.target_spread_bps",
        "current": 50.0,
        "recommended": 40.0,
        "confidence": 0.8,
        "justification": "tighter",
    }])
    cfg = Config(
        tuner=TunerCfg(enabled=True, min_orders_for_feedback=1000),
        research=ResearchCfg(auto_apply=True),
    )
    AutoTuner(cfg, live=False).run()
    log = TUNER_LOG.read_text()
    assert "market_maker.target_spread_bps" in log
    assert "research:" in log


def test_tuner_lookback_hours_decouples_from_interval(isolated_state):
    """With interval=10 min but lookback=60 min, the aggregator should
    read 60 minutes of fills, so a low-fill-rate signal from older data
    is still visible."""
    s = Store("state/polybot.sqlite")
    now = time.time()
    # 30 minutes ago — inside 1h lookback, outside 10-min interval.
    for i in range(80):
        s.record_order(f"o{i}", "tok", "BUY", 0.5, 10, now - 1800)
    for i in range(2):
        s.record_fill(f"o{i}", "tok", "BUY", 0.5, 10, now - 1800)
    cfg = Config(
        tuner=TunerCfg(
            enabled=True,
            evaluation_window_hours=0.167,   # 10 min
            lookback_hours=1.0,              # 60 min
            target_fill_rate=0.3, learning_rate=0.5,
            min_orders_for_feedback=20,
        ),
        market_maker=MarketMakerCfg(target_spread_bps=80),
        research=ResearchCfg(auto_apply=False),
    )
    res = AutoTuner(cfg, live=False).run()
    assert res is not None
    # Low fill rate should trigger narrowing.
    assert res.changes["market_maker.target_spread_bps"] < 80
