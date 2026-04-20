import time

from polybot.models import BookLevel, OrderBook, Quote, TokenMarket
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


# ---- inside-spread fills ----

def _market(volume_24h_usd: float = 10_000.0) -> TokenMarket:
    return TokenMarket(
        token_id="t1",
        outcome="Yes",
        condition_id="c1",
        question="q",
        end_date_iso=None,
        volume_24h_usd=volume_24h_usd,
    )


def test_inside_spread_buy_eventually_fills_on_liquid_market():
    """A BUY inside the spread on a $50k/24h market should reliably fill
    within a reasonable number of 2-second ticks."""
    b = PaperBroker(fill_probability=0.0, seed=42)  # force cross path off
    b.place_limit(Quote(token_id="t1", side="BUY", price=0.495, size=20))
    markets = {"t1": _market(volume_24h_usd=50_000.0)}

    # Prime last_match_ts
    b.match_fills({"t1": _book(bid=0.49, ask=0.50)}, markets=markets)

    all_fills = []
    # Simulate 20 minutes of 2-sec ticks.
    for _ in range(600):
        b._last_match_ts -= 2.0  # pretend 2s elapsed per call
        fills = b.match_fills({"t1": _book(bid=0.49, ask=0.50)}, markets=markets)
        all_fills.extend(fills)
        if all_fills:
            break
    assert all_fills, "expected at least one inside-spread fill on a liquid market"
    assert all_fills[0].side == "BUY"
    assert all_fills[0].price == 0.495


def test_inside_spread_sell_fills_when_quoting_inside_ask():
    b = PaperBroker(fill_probability=0.0, seed=7)
    b.place_limit(Quote(token_id="t1", side="SELL", price=0.505, size=20))
    markets = {"t1": _market(volume_24h_usd=50_000.0)}
    b.match_fills({"t1": _book(bid=0.49, ask=0.51)}, markets=markets)

    fills = []
    for _ in range(600):
        b._last_match_ts -= 2.0
        fills.extend(b.match_fills(
            {"t1": _book(bid=0.49, ask=0.51)}, markets=markets,
        ))
        if fills:
            break
    assert fills, "expected an inside-spread SELL fill"
    assert fills[0].side == "SELL"


def test_inside_spread_path_requires_markets_arg():
    """Without a markets dict we fall back to cross-only (old behavior)."""
    b = PaperBroker(fill_probability=0.0, seed=1)
    b.place_limit(Quote(token_id="t1", side="BUY", price=0.495, size=20))
    b.match_fills({"t1": _book(bid=0.49, ask=0.50)})
    for _ in range(500):
        b._last_match_ts = (b._last_match_ts or time.time()) - 2.0
        fills = b.match_fills({"t1": _book(bid=0.49, ask=0.50)})  # no markets=
        assert fills == []


def test_not_quoted_inside_means_no_inside_fill():
    """If our BUY is at or below the live best_bid we're NOT inside the
    spread — no inside-fill path fires."""
    b = PaperBroker(fill_probability=0.0, seed=1)
    b.place_limit(Quote(token_id="t1", side="BUY", price=0.48, size=20))
    markets = {"t1": _market(volume_24h_usd=1_000_000.0)}
    b.match_fills({"t1": _book(bid=0.49, ask=0.50)}, markets=markets)
    for _ in range(200):
        b._last_match_ts -= 2.0
        fills = b.match_fills({"t1": _book(bid=0.49, ask=0.50)}, markets=markets)
        assert fills == []
