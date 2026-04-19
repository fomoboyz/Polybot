from polybot.research import classify_trader


def test_classify_empty_trades():
    style, _, _, n = classify_trader([])
    assert style == "unknown"
    assert n == 0


def test_classify_market_maker():
    trades = [
        {"side": "maker", "is_taker": False, "tokenId": f"t{i}", "timestamp": i * 100}
        for i in range(10)
    ]
    style, _, taker_ratio, n = classify_trader(trades)
    assert style == "market_maker"
    assert taker_ratio == 0.0
    assert n == 10


def test_classify_directional():
    trades = [
        {"side": "taker", "is_taker": True, "tokenId": "single", "timestamp": i}
        for i in range(5)
    ]
    style, _, taker_ratio, n = classify_trader(trades)
    assert style == "directional"
    assert n == 1
    assert taker_ratio == 1.0


def test_classify_arbitrageur():
    # Taker-heavy + very short holds + several markets → arbitrageur.
    trades = []
    for i in range(10):
        trades.append({
            "side": "taker",
            "is_taker": True,
            "tokenId": f"m{i % 5}",
            "timestamp": i * 5,  # 5-second cadence
        })
    style, hold, taker_ratio, n = classify_trader(trades)
    assert style == "arbitrageur"
    assert hold is not None and hold < 60
    assert taker_ratio > 0.6
    assert n == 5
