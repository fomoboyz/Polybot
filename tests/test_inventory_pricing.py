from polybot.config import MarketMakerCfg
from polybot.models import BookLevel, OrderBook, TokenMarket
from polybot.reward_optimizer import compute_quote_pair


def _market():
    return TokenMarket(
        token_id="t1", outcome="Yes", condition_id="c1", question="q",
        end_date_iso=None, tick_size=0.001, min_order_size=5, sibling_token_id="t2",
    )


def _book(bid=0.49, ask=0.51, size=100):
    return OrderBook(
        token_id="t1",
        bids=[BookLevel(price=bid, size=size)],
        asks=[BookLevel(price=ask, size=size)],
        timestamp_ms=0,
    )


def test_long_inventory_lowers_reservation_price():
    cfg = MarketMakerCfg(
        use_inventory_skew=True, inventory_risk_aversion=5.0, max_skew_shift=0.05,
        target_spread_bps=50, min_edge_over_mid_bps=10,
    )
    sigma = 0.05
    flat = compute_quote_pair(_market(), _book(), cfg, inventory_shares=0, sigma=sigma)
    long = compute_quote_pair(_market(), _book(), cfg, inventory_shares=20, sigma=sigma)
    assert flat is not None and long is not None
    # Long → reservation below mid → both bid and ask shift down.
    assert long.reservation_price < flat.reservation_price
    assert long.bid < flat.bid
    assert long.ask < flat.ask


def test_short_inventory_raises_reservation_price():
    cfg = MarketMakerCfg(
        use_inventory_skew=True, inventory_risk_aversion=5.0, max_skew_shift=0.05,
    )
    sigma = 0.05
    flat = compute_quote_pair(_market(), _book(), cfg, inventory_shares=0, sigma=sigma)
    short = compute_quote_pair(_market(), _book(), cfg, inventory_shares=-20, sigma=sigma)
    assert short is not None and flat is not None
    assert short.reservation_price > flat.reservation_price


def test_max_skew_shift_caps_movement():
    cfg = MarketMakerCfg(
        use_inventory_skew=True, inventory_risk_aversion=1000.0, max_skew_shift=0.001,
    )
    pair = compute_quote_pair(_market(), _book(), cfg, inventory_shares=500, sigma=0.1)
    assert pair is not None
    assert abs(pair.reservation_price - 0.5) <= 0.002  # capped


def test_adaptive_spread_widens_on_high_sigma():
    cfg = MarketMakerCfg(
        use_adaptive_spread=True, sigma_to_spread_multiplier=10.0,
        target_spread_bps=20, min_edge_over_mid_bps=10,
    )
    calm = compute_quote_pair(_market(), _book(), cfg, sigma=0.001)
    wild = compute_quote_pair(_market(), _book(), cfg, sigma=0.01)
    assert calm is not None and wild is not None
    assert wild.spread >= calm.spread


def test_inventory_pricing_disabled_by_default_still_works():
    cfg = MarketMakerCfg()  # defaults now include use_inventory_skew=True
    pair = compute_quote_pair(_market(), _book(), cfg, inventory_shares=0, sigma=None)
    assert pair is not None
