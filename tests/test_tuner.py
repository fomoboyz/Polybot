import os
import time
from pathlib import Path

import pytest

from polybot.config import Config, TunerCfg, MarketMakerCfg, ResearchCfg
from polybot.state import Store
from polybot.tuner import AutoTuner


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
    assert t.run() is None


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
    assert "target_spread_bps" in res.changes
    assert res.changes["target_spread_bps"] < 100


def test_tuner_widens_spread_on_high_fill_rate(isolated_state):
    _seed_orders_and_fills(n_orders=100, n_fills=80)  # 80%
    cfg = Config(
        tuner=TunerCfg(enabled=True, target_fill_rate=0.3, learning_rate=0.5),
        market_maker=MarketMakerCfg(target_spread_bps=30),
    )
    t = AutoTuner(cfg, live=False)
    res = t.run()
    assert res is not None
    assert res.changes["target_spread_bps"] > 30
