import time

from polybot.config import AllocatorCfg, LadderCfg, MarketMakerCfg, RiskCfg
from polybot.executor import OrderExecutor
from polybot.risk import RiskManager
from polybot.strategies.market_maker import MarketMakerStrategy
from polybot.volatility import VolatilityTracker


class _StubClob:
    def place_limit(self, quote):
        return None

    def cancel(self, order_id):
        return True


def _build(vol: VolatilityTracker = None) -> MarketMakerStrategy:
    cfg = MarketMakerCfg(
        fast_move_bps=200, fast_move_lookback_sec=60,
        fast_move_cooldown_sec=60,
    )
    risk = RiskManager(RiskCfg())
    executor = OrderExecutor(_StubClob(), risk)
    return MarketMakerStrategy(
        cfg=cfg, executor=executor, risk=risk, volatility=vol,
    )


def test_fast_move_no_vol_means_no_cooldown(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = _build(vol=None)
    assert s._fast_move_active("t1", now=time.time()) is False


def test_fast_move_triggers_on_large_recent_move(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    vol = VolatilityTracker(window_sec=300, min_samples=2)
    now = time.time()
    # Seed with a 300bps jump over 30s.
    vol.update("t1", now - 30, 0.50)
    vol.update("t1", now, 0.515)  # 300 bps move
    s = _build(vol=vol)
    assert s._fast_move_active("t1", now=now) is True


def test_fast_move_ignores_small_move(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    vol = VolatilityTracker(window_sec=300, min_samples=2)
    now = time.time()
    vol.update("t1", now - 30, 0.50)
    vol.update("t1", now, 0.505)  # 100 bps move — below 200bps threshold
    s = _build(vol=vol)
    assert s._fast_move_active("t1", now=now) is False


def test_fast_move_cooldown_persists(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    vol = VolatilityTracker(window_sec=300, min_samples=2)
    now = time.time()
    vol.update("t1", now - 30, 0.50)
    vol.update("t1", now, 0.520)  # big move
    s = _build(vol=vol)
    assert s._fast_move_active("t1", now=now) is True
    # Even if we clear the volatility signal, cooldown should still be active.
    assert s._fast_move_active("t1", now=now + 10) is True
    # After cooldown expires, not active anymore (assuming signal also gone).
    vol2 = VolatilityTracker(window_sec=300, min_samples=2)
    s._vol = vol2  # replace with a fresh tracker so no delta is visible
    assert s._fast_move_active("t1", now=now + 120) is False
