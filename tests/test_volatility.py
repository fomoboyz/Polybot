from polybot.volatility import VolatilityTracker


def test_sigma_needs_min_samples():
    v = VolatilityTracker(window_sec=1000, min_samples=8)
    for i in range(5):
        v.update("t1", i, 0.5 + 0.001 * i)
    assert v.sigma("t1") is None


def test_sigma_computed_once_enough_samples():
    v = VolatilityTracker(window_sec=1000, min_samples=3)
    for i in range(10):
        v.update("t1", i, 0.5 + 0.01 * (i % 2))  # oscillating
    s = v.sigma("t1")
    assert s is not None and s > 0


def test_window_eviction():
    v = VolatilityTracker(window_sec=5, min_samples=3)
    for i in range(10):
        v.update("t1", i, 0.5)
    # Only points within window remain; all identical → sigma = 0
    s = v.sigma("t1")
    assert s is not None and s == 0


def test_midpoint_delta_bps():
    v = VolatilityTracker(window_sec=1000, min_samples=2)
    v.update("t1", 0.0, 0.50)
    v.update("t1", 60.0, 0.55)
    d = v.midpoint_delta_bps("t1", lookback_sec=60.0)
    assert d is not None and d > 900  # ~1000 bps (10%) move
