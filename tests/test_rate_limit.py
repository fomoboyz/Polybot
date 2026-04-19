import time

from polybot.rate_limit import RateLimiter


def test_unconfigured_key_is_unlimited():
    r = RateLimiter()
    assert r.acquire("anything") is True


def test_configured_bucket_blocks_when_empty():
    r = RateLimiter()
    r.configure("k", capacity=2, refill_per_sec=100)
    assert r.acquire("k") is True
    assert r.acquire("k") is True
    # Non-blocking third call should fail (tokens briefly zero).
    assert r.acquire("k", block=False) is False


def test_refill_allows_further_acquires():
    r = RateLimiter()
    r.configure("k", capacity=1, refill_per_sec=50)
    assert r.acquire("k") is True
    t0 = time.time()
    r.acquire("k")  # should block briefly and then succeed
    assert time.time() - t0 < 0.2
