import os
import time

import pytest

from polybot.backtest import Backtester
from polybot.config import Config, HistoryCfg, MarketMakerCfg
from polybot.history import HistoryRecorder
from polybot.models import BookLevel, OrderBook


@pytest.fixture
def recorder(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = HistoryCfg(snapshot_interval_sec=0.01, db_path=str(tmp_path / "hist.sqlite"))
    return HistoryRecorder(cfg)


def _book(mid: float, depth: float = 100) -> OrderBook:
    return OrderBook(
        token_id="t1",
        bids=[BookLevel(price=mid - 0.005, size=depth)],
        asks=[BookLevel(price=mid + 0.005, size=depth)],
        timestamp_ms=int(time.time() * 1000),
    )


def test_recorder_stores_and_retrieves(recorder):
    recorder.record({"t1": _book(0.5)})
    time.sleep(0.02)
    recorder.record({"t1": _book(0.51)})
    snaps = list(recorder.iter_snapshots(token_ids=["t1"]))
    assert len(snaps) == 2


def test_recorder_skips_within_interval(tmp_path):
    cfg = HistoryCfg(snapshot_interval_sec=60, db_path=str(tmp_path / "h.sqlite"))
    rec = HistoryRecorder(cfg)
    rec.record({"t1": _book(0.5)})
    rec.record({"t1": _book(0.51)})  # too soon
    snaps = list(rec.iter_snapshots())
    assert len(snaps) == 1


def test_backtester_produces_fills(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    hist = HistoryCfg(snapshot_interval_sec=0.01, db_path=str(tmp_path / "h.sqlite"))
    rec = HistoryRecorder(hist)
    # Record a series where the market oscillates around mid — our bot should
    # get filled when the market crosses our quotes.
    for i in range(20):
        mid = 0.50 + (0.005 if i % 2 == 0 else -0.005)
        rec.record({"t1": _book(mid, depth=50)})
        time.sleep(0.02)

    cfg = Config(
        history=hist,
        market_maker=MarketMakerCfg(
            target_spread_bps=100,
            min_edge_over_mid_bps=10,
            quote_size_usd=10,
            requote_drift_bps=1,
            requote_interval_sec=1,
            use_inventory_skew=False,
            use_adaptive_spread=False,
        ),
    )
    result = Backtester(cfg, rec).run(fill_probability=1.0)
    assert result.n_snapshots >= 15
    assert result.n_tokens == 1
    assert result.orders_placed > 0
