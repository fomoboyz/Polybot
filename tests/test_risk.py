from polybot.config import RiskCfg
from polybot.models import Quote
from polybot.risk import RiskManager


def _q(token="t1", side="BUY", price=0.5, size=20):
    return Quote(token_id=token, side=side, price=price, size=size)


def test_min_order_rejected():
    r = RiskManager(RiskCfg(min_order_usd=5))
    assert r.validate(_q(price=0.5, size=1)) is not None  # $0.50


def test_total_notional_cap():
    r = RiskManager(RiskCfg(max_notional_usd=15, max_per_market_usd=100))
    r.on_rest(_q(price=0.5, size=20))  # uses $10
    assert r.validate(_q(price=0.5, size=20)) is not None  # would go $20


def test_per_market_cap():
    r = RiskManager(RiskCfg(max_per_market_usd=8, max_notional_usd=1000))
    assert r.validate(_q(price=0.5, size=20)) is not None  # $10 > $8


def test_kill_switch_blocks():
    r = RiskManager(RiskCfg())
    r._trip_kill_switch("test")
    try:
        assert r.validate(_q()) == "kill-switch-active"
    finally:
        from polybot.risk import KILL_FILE
        if KILL_FILE.exists():
            KILL_FILE.unlink()


def test_fill_updates_pnl():
    r = RiskManager(RiskCfg())
    r.on_fill("t1", "BUY", 10, 0.5)
    r.on_fill("t1", "SELL", 10, 0.6)
    assert round(r.state.realized_pnl_today, 6) == 1.0


def test_skew_blocks_adding_to_heavy_side():
    r = RiskManager(RiskCfg(max_inventory_skew=5))
    # Build up a long position of 10 shares.
    r.on_fill("t1", "BUY", 10, 0.5)
    # Adding more longs should be rejected.
    assert r.validate(_q(side="BUY", price=0.5, size=20)) is not None
    # But closing (SELL) is fine.
    assert r.validate(_q(side="SELL", price=0.5, size=20)) is None
