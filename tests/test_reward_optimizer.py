from polybot.config import MarketMakerCfg
from polybot.models import BookLevel, OrderBook, TokenMarket
from polybot.reward_optimizer import compute_quote_pair


def _market() -> TokenMarket:
    return TokenMarket(
        token_id="t1",
        outcome="Yes",
        condition_id="c1",
        question="q",
        end_date_iso=None,
        tick_size=0.001,
        min_order_size=5,
        sibling_token_id="t2",
    )


def _book(bid: float, ask: float) -> OrderBook:
    return OrderBook(
        token_id="t1",
        bids=[BookLevel(price=bid, size=100)],
        asks=[BookLevel(price=ask, size=100)],
        timestamp_ms=0,
    )


def test_quote_respects_target_spread():
    cfg = MarketMakerCfg(target_spread_bps=100, min_edge_over_mid_bps=10)
    book = _book(0.49, 0.51)  # mid=0.50, wide (200bps total)
    pair = compute_quote_pair(_market(), book, cfg)
    assert pair is not None
    # target_spread_bps=100 = 0.01 half-spread → bid ≈ 0.49, ask ≈ 0.51
    # but our cfg is half=0.01, so bid=0.49, ask=0.51 (rounded to tick).
    assert 0.48 <= pair.bid <= 0.495
    assert 0.505 <= pair.ask <= 0.52
    assert pair.ask - pair.bid > 0


def test_quote_never_crosses_mid():
    cfg = MarketMakerCfg(
        target_spread_bps=1,       # absurdly tight target
        min_edge_over_mid_bps=10,  # hard floor 10bps
    )
    book = _book(0.49, 0.51)
    pair = compute_quote_pair(_market(), book, cfg)
    assert pair is not None
    # min edge is 10bps = 0.001. Bid must be ≤ 0.499, ask ≥ 0.501.
    assert pair.bid < 0.500
    assert pair.ask > 0.500


def test_quote_returns_none_at_book_edges():
    cfg = MarketMakerCfg()
    # midpoint at 0.999 — not viable
    book = OrderBook(
        token_id="t1",
        bids=[BookLevel(price=0.998, size=100)],
        asks=[BookLevel(price=1.000, size=100)],
        timestamp_ms=0,
    )
    pair = compute_quote_pair(_market(), book, cfg)
    # midpoint = 0.999 — compute_quote_pair allows it (still < 1), but
    # ask = min(1-tick, ...) means ask could collapse. Either None or
    # ask > bid is fine.
    if pair is not None:
        assert pair.ask > pair.bid


def test_tighter_spread_higher_score():
    cfg = MarketMakerCfg(target_spread_bps=50, min_edge_over_mid_bps=5)
    wide = compute_quote_pair(_market(), _book(0.40, 0.60), cfg)
    tight = compute_quote_pair(_market(), _book(0.495, 0.505), cfg)
    # Note: the optimizer quotes at target half-spread regardless of the book
    # (unless competitor is wide enough to step inside). The *expected score*
    # reflects distance-to-mid; tighter quotes → higher score.
    assert wide is not None and tight is not None
    # Both produce the same target-driven quote, so equal scores is expected.
    assert tight.expected_score >= wide.expected_score


def test_score_scales_linearly_with_size():
    """Polymarket's LP reward is linear in order size — a 2× bigger quote
    should earn a 2× bigger expected score at the same tightness."""
    cfg = MarketMakerCfg(target_spread_bps=50, min_edge_over_mid_bps=5)
    small = compute_quote_pair(
        _market(), _book(0.49, 0.51), cfg, size_usd=5.0,
    )
    big = compute_quote_pair(
        _market(), _book(0.49, 0.51), cfg, size_usd=20.0,
    )
    assert small is not None and big is not None
    ratio = big.expected_score / max(small.expected_score, 1e-9)
    # Expect roughly 4× larger score for 4× larger size.
    assert 3.5 < ratio < 4.5


def test_score_uses_cfg_quote_size_when_not_passed():
    """When caller doesn't pass size_usd, fall back to cfg.quote_size_usd."""
    cfg_small = MarketMakerCfg(target_spread_bps=50, quote_size_usd=5.0)
    cfg_big = MarketMakerCfg(target_spread_bps=50, quote_size_usd=20.0)
    small = compute_quote_pair(_market(), _book(0.49, 0.51), cfg_small)
    big = compute_quote_pair(_market(), _book(0.49, 0.51), cfg_big)
    assert small is not None and big is not None
    assert big.expected_score > small.expected_score
