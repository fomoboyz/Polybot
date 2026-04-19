from polybot.allocator import Allocator
from polybot.config import AllocatorCfg, RiskCfg
from polybot.models import TokenMarket
from polybot.reward_optimizer import QuotePair


def _mkt(tid):
    return TokenMarket(
        token_id=tid, outcome="Yes", condition_id="c", question="q",
        end_date_iso=None, tick_size=0.001, min_order_size=5,
    )


def _pair(score):
    return QuotePair(bid=0.49, ask=0.51, spread=0.02, expected_score=score, reservation_price=0.5)


def test_allocator_disabled_returns_empty():
    a = Allocator(AllocatorCfg(enabled=False), RiskCfg())
    assert a.allocate([(_mkt("a"), _pair(1))], 100) == {}


def test_allocator_splits_proportionally_to_score():
    a = Allocator(AllocatorCfg(enabled=True, min_per_market_usd=1), RiskCfg(max_per_market_usd=1000))
    ranked = [(_mkt("a"), _pair(2)), (_mkt("b"), _pair(1))]
    sizes = a.allocate(ranked, 90)
    assert abs(sizes["a"] - 60) < 1e-6
    assert abs(sizes["b"] - 30) < 1e-6


def test_allocator_respects_per_market_cap():
    a = Allocator(AllocatorCfg(enabled=True, min_per_market_usd=1), RiskCfg(max_per_market_usd=10))
    ranked = [(_mkt("a"), _pair(5)), (_mkt("b"), _pair(1))]
    sizes = a.allocate(ranked, 100)
    assert sizes["a"] <= 10 + 1e-6
    assert sizes["b"] <= 10 + 1e-6


def test_allocator_skips_below_min_order():
    a = Allocator(AllocatorCfg(enabled=True, min_per_market_usd=0.1), RiskCfg(min_order_usd=5))
    # Tiny budget split across many markets → some drop below min.
    ranked = [(_mkt(f"m{i}"), _pair(1)) for i in range(20)]
    sizes = a.allocate(ranked, 20)
    for v in sizes.values():
        assert v >= 5 - 1e-6


def test_allocator_fallback_when_zero_score():
    a = Allocator(AllocatorCfg(enabled=True), RiskCfg(max_per_market_usd=50))
    ranked = [(_mkt("a"), _pair(0)), (_mkt("b"), _pair(0))]
    sizes = a.allocate(ranked, 60)
    assert sizes["a"] == sizes["b"]
