from typing import Dict, List

from polybot.config import MarketMakerCfg, ScannerCfg
from polybot.models import BookLevel, OrderBook, TokenMarket
from polybot.scanner import MarketScanner


class _StubGamma:
    def __init__(self, markets: List[TokenMarket]):
        self._markets = markets

    def list_markets(self, **kwargs) -> List[TokenMarket]:
        return self._markets


class _StubClob:
    def __init__(self, books: Dict[str, OrderBook]):
        self._books = books

    def get_order_book(self, token_id: str) -> OrderBook:
        return self._books[token_id]


def _mk(token_id: str) -> TokenMarket:
    return TokenMarket(
        token_id=token_id,
        outcome="Yes",
        condition_id="c" + token_id,
        question="q " + token_id,
        end_date_iso=None,
        liquidity_usd=50_000,
    )


def _book(tid: str, bid: float, ask: float) -> OrderBook:
    return OrderBook(
        token_id=tid,
        bids=[BookLevel(price=bid, size=1000)],
        asks=[BookLevel(price=ask, size=1000)],
        timestamp_ms=0,
    )


def test_scanner_drops_near_certainty_markets():
    """A market with mid=0.99 is dead — passive quotes never fill.
    The scanner should filter it out even if liquidity/volume pass."""
    dead = _mk("dead")      # mid ≈ 0.99
    alive = _mk("alive")    # mid ≈ 0.50
    books = {
        "dead": _book("dead", 0.988, 0.992),
        "alive": _book("alive", 0.49, 0.51),
    }
    scanner = MarketScanner(
        gamma=_StubGamma([dead, alive]),
        clob=_StubClob(books),
        scanner_cfg=ScannerCfg(
            min_mid_price=0.15, max_mid_price=0.85,
            max_concurrent_markets=5,
        ),
        mm_cfg=MarketMakerCfg(),
    )
    active = scanner.active_markets()
    ids = {m.token_id for m in active}
    assert "alive" in ids
    assert "dead" not in ids


def test_scanner_keeps_balanced_markets():
    m1 = _mk("m1")   # mid 0.50
    m2 = _mk("m2")   # mid 0.30
    books = {
        "m1": _book("m1", 0.49, 0.51),
        "m2": _book("m2", 0.29, 0.31),
    }
    scanner = MarketScanner(
        gamma=_StubGamma([m1, m2]),
        clob=_StubClob(books),
        scanner_cfg=ScannerCfg(
            min_mid_price=0.15, max_mid_price=0.85,
            max_concurrent_markets=5,
        ),
        mm_cfg=MarketMakerCfg(),
    )
    active = scanner.active_markets()
    ids = {m.token_id for m in active}
    assert ids == {"m1", "m2"}


def test_scanner_drops_markets_below_min_mid():
    """Sibling side of a near-certainty market (mid ≈ 0.01)."""
    dead = _mk("dead_low")
    books = {"dead_low": _book("dead_low", 0.008, 0.012)}
    scanner = MarketScanner(
        gamma=_StubGamma([dead]),
        clob=_StubClob(books),
        scanner_cfg=ScannerCfg(
            min_mid_price=0.15, max_mid_price=0.85,
            max_concurrent_markets=5,
        ),
        mm_cfg=MarketMakerCfg(),
    )
    active = scanner.active_markets()
    assert active == []


def test_scanner_drops_jumpy_markets_when_vol_tracker_provided():
    """A market that just moved 1200 bps in 60s is too hot to quote
    passively — scanner should drop it."""
    from polybot.volatility import VolatilityTracker
    import time

    jumpy = _mk("jumpy")
    calm = _mk("calm")
    books = {
        "jumpy": _book("jumpy", 0.49, 0.51),
        "calm": _book("calm", 0.49, 0.51),
    }
    vol = VolatilityTracker(window_sec=300, min_samples=2)
    now = time.time()
    # Seed volatility: jumpy moved 50% in 30s, calm moved 0.4%.
    vol.update("jumpy", now - 30, 0.50)
    vol.update("jumpy", now, 0.56)   # 1200 bps
    vol.update("calm", now - 30, 0.50)
    vol.update("calm", now, 0.502)   # 40 bps

    scanner = MarketScanner(
        gamma=_StubGamma([jumpy, calm]),
        clob=_StubClob(books),
        scanner_cfg=ScannerCfg(
            min_mid_price=0.15, max_mid_price=0.85,
            max_recent_move_bps=800,
            recent_move_lookback_sec=60,
            max_concurrent_markets=5,
        ),
        mm_cfg=MarketMakerCfg(),
        volatility=vol,
    )
    active = scanner.active_markets()
    ids = {m.token_id for m in active}
    assert "calm" in ids
    assert "jumpy" not in ids


def test_scanner_keeps_all_markets_when_no_vol_tracker():
    """Without volatility data, the jumpy-filter is skipped (first run)."""
    m = _mk("m1")
    books = {"m1": _book("m1", 0.49, 0.51)}
    scanner = MarketScanner(
        gamma=_StubGamma([m]),
        clob=_StubClob(books),
        scanner_cfg=ScannerCfg(
            min_mid_price=0.15, max_mid_price=0.85,
            max_concurrent_markets=5,
        ),
        mm_cfg=MarketMakerCfg(),
        volatility=None,
    )
    active = scanner.active_markets()
    assert {m.token_id for m in active} == {"m1"}
