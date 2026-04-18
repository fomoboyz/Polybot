from polybot.models import Position


def test_position_open_long_then_close_at_profit():
    p = Position(token_id="t")
    p.apply_fill("BUY", 10, 0.5)
    assert p.net_shares == 10
    assert p.avg_cost == 0.5
    p.apply_fill("SELL", 10, 0.6)
    assert p.net_shares == 0
    assert round(p.realized_pnl, 6) == round(1.0, 6)


def test_position_averaging():
    p = Position(token_id="t")
    p.apply_fill("BUY", 10, 0.4)
    p.apply_fill("BUY", 10, 0.6)
    assert p.net_shares == 20
    assert round(p.avg_cost, 6) == 0.5


def test_position_flip():
    p = Position(token_id="t")
    p.apply_fill("BUY", 10, 0.5)
    p.apply_fill("SELL", 15, 0.6)
    # Closed 10 @ 0.5→0.6 = +1.0 PnL, then short 5 @ 0.6.
    assert p.net_shares == -5
    assert round(p.avg_cost, 6) == 0.6
    assert round(p.realized_pnl, 6) == 1.0
