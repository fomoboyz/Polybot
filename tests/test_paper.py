from polybot.models import BookLevel, OrderBook, Quote
from polybot.paper import PaperBroker


def _book(bid=0.48, ask=0.52, size=100):
    return OrderBook(
        token_id="t1",
        bids=[BookLevel(price=bid, size=size)],
        asks=[BookLevel(price=ask, size=size)],
        timestamp_ms=0,
    )


def test_limit_buy_fills_when_ask_crosses():
    b = PaperBroker(fill_probability=1.0, seed=1)
    b.place_limit(Quote(token_id="t1", side="BUY", price=0.50, size=20))
    # ask dropped to 0.50 → our bid @ 0.50 should fill.
    fills = b.match_fills({"t1": _book(bid=0.48, ask=0.50)})
    assert len(fills) == 1
    assert fills[0].side == "BUY"
    assert fills[0].price == 0.50


def test_limit_sell_fills_when_bid_crosses():
    b = PaperBroker(fill_probability=1.0, seed=1)
    b.place_limit(Quote(token_id="t1", side="SELL", price=0.50, size=20))
    fills = b.match_fills({"t1": _book(bid=0.50, ask=0.55)})
    assert len(fills) == 1
    assert fills[0].side == "SELL"


def test_no_fill_when_uncrossed():
    b = PaperBroker(fill_probability=1.0, seed=1)
    b.place_limit(Quote(token_id="t1", side="BUY", price=0.40, size=20))
    fills = b.match_fills({"t1": _book(bid=0.48, ask=0.52)})
    assert fills == []


def test_fill_probability():
    # With fill_probability=0 the order should never fill even when crossed.
    b = PaperBroker(fill_probability=0.0, seed=1)
    b.place_limit(Quote(token_id="t1", side="BUY", price=0.55, size=20))
    fills = b.match_fills({"t1": _book(bid=0.50, ask=0.55)})
    assert fills == []


def test_market_order_fills_at_top_of_book():
    b = PaperBroker(fill_probability=1.0, seed=1)
    fill = b.place_market("t1", "BUY", 10.0, _book(bid=0.48, ask=0.52))
    assert fill is not None
    assert fill.price == 0.52
    assert round(fill.shares, 4) == round(10.0 / 0.52, 4)


def test_cancel_removes_order():
    b = PaperBroker(fill_probability=1.0, seed=1)
    oid = b.place_limit(Quote(token_id="t1", side="BUY", price=0.50, size=20))
    assert oid is not None
    assert b.cancel(oid) is True
    fills = b.match_fills({"t1": _book(bid=0.48, ask=0.50)})
    assert fills == []
