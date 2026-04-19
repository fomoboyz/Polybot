import json

from polybot.models import OrderBook, BookLevel
from polybot.websocket_feed import WebSocketFeed, _apply_change, _parse_levels


def test_parse_levels_sorts():
    lvls = _parse_levels(
        [{"price": "0.5", "size": "10"}, {"price": "0.6", "size": "5"}],
        descending=True,
    )
    assert [l.price for l in lvls] == [0.6, 0.5]


def test_apply_change_inserts_and_removes():
    lvls = [BookLevel(price=0.50, size=10)]
    _apply_change(lvls, 0.55, 5, descending=False)
    assert len(lvls) == 2
    assert any(abs(l.price - 0.55) < 1e-9 for l in lvls)
    # size=0 removes the level.
    _apply_change(lvls, 0.50, 0, descending=False)
    assert all(abs(l.price - 0.50) > 1e-9 for l in lvls)


def test_book_event_updates_cache():
    feed = WebSocketFeed()
    event = {
        "event_type": "book",
        "asset_id": "tok1",
        "timestamp": 12345,
        "bids": [{"price": "0.48", "size": "100"}],
        "asks": [{"price": "0.52", "size": "80"}],
    }
    feed._handle_raw(json.dumps(event))
    b = feed.get("tok1")
    assert b is not None
    assert b.best_bid and abs(b.best_bid.price - 0.48) < 1e-9
    assert b.best_ask and abs(b.best_ask.price - 0.52) < 1e-9


def test_price_change_merges_into_existing_book():
    feed = WebSocketFeed()
    # Seed with a book.
    feed._handle_raw(json.dumps({
        "event_type": "book", "asset_id": "tok2", "timestamp": 1,
        "bids": [{"price": "0.48", "size": "100"}],
        "asks": [{"price": "0.52", "size": "80"}],
    }))
    # Price_change: bid level size change.
    feed._handle_raw(json.dumps({
        "event_type": "price_change", "asset_id": "tok2", "timestamp": 2,
        "changes": [{"price": "0.48", "size": "50", "side": "buy"}],
    }))
    b = feed.get("tok2")
    assert b and b.best_bid and b.best_bid.size == 50


def test_best_bid_ask_event_seeds_book():
    feed = WebSocketFeed()
    feed._handle_raw(json.dumps({
        "event_type": "best_bid_ask", "asset_id": "tok3", "timestamp": 1,
        "best_bid": {"price": "0.40", "size": "10"},
        "best_ask": {"price": "0.60", "size": "10"},
    }))
    b = feed.get("tok3")
    assert b is not None and b.best_bid and b.best_ask


def test_pong_message_is_ignored():
    feed = WebSocketFeed()
    feed._handle_raw("PONG")  # should not raise
    assert feed.get("anything") is None
