import time

from polybot.circuit_breaker import BreakerConfig, CircuitBreaker


def test_error_burst_trips():
    cb = CircuitBreaker(BreakerConfig(max_errors_per_minute=5, cooldown_sec=1))
    for _ in range(10):
        cb.record_error()
    assert cb.check([], {}) is not None
    assert cb.tripped() is True
    assert "error_burst" in (cb.reason or "")


def test_crash_trips_when_many_markets_move():
    cfg = BreakerConfig(crash_bps_per_min=100, crash_market_share=0.5, cooldown_sec=1)
    cb = CircuitBreaker(cfg)
    now = time.time()
    cb.record_book("a", now)
    cb.record_book("b", now)
    cb.record_book("c", now)
    res = cb.check(["a", "b", "c"], {"a": 200.0, "b": 200.0, "c": 10.0})
    assert res is not None and "crash" in res


def test_api_failure_rate_trips():
    cb = CircuitBreaker(BreakerConfig(max_api_failure_rate=0.3, cooldown_sec=1))
    for _ in range(25):
        cb.record_api(False)
    for _ in range(5):
        cb.record_api(True)
    res = cb.check([], {})
    assert res is not None and "api_failure_rate" in res


def test_healthy_does_not_trip():
    cb = CircuitBreaker(BreakerConfig())
    cb.record_book("a", time.time())
    assert cb.check(["a"], {"a": 10.0}) is None
    assert cb.tripped() is False


def test_cooldown_resets():
    cb = CircuitBreaker(BreakerConfig(cooldown_sec=0))
    for _ in range(100):
        cb.record_error()
    cb.check([], {})
    assert cb.tripped() is False  # cooldown=0 means tripped_until is now
