from polybot.config import LadderCfg, MarketMakerCfg
from polybot.ladder import ladder_quotes
from polybot.models import TokenMarket
from polybot.reward_optimizer import QuotePair


def _m():
    return TokenMarket(
        token_id="t1", outcome="Yes", condition_id="c", question="q",
        end_date_iso=None, tick_size=0.001, min_order_size=5,
    )


def _pair(bid=0.49, ask=0.51):
    return QuotePair(bid=bid, ask=ask, spread=ask - bid, expected_score=1.0, reservation_price=0.5)


def test_ladder_emits_expected_levels():
    quotes = ladder_quotes(_m(), _pair(), MarketMakerCfg(), LadderCfg(levels=3, step_bps=20, size_decay=0.5), 10)
    assert len(quotes) == 6  # 3 buy + 3 sell
    buys = [q for q in quotes if q.side == "BUY"]
    sells = [q for q in quotes if q.side == "SELL"]
    assert [q.slot for q in buys] == [0, 1, 2]
    assert [q.slot for q in sells] == [0, 1, 2]
    # Wider outer levels.
    assert buys[1].price < buys[0].price
    assert sells[1].price > sells[0].price


def test_ladder_size_decays():
    quotes = ladder_quotes(_m(), _pair(), MarketMakerCfg(), LadderCfg(levels=3, step_bps=10, size_decay=0.5), 20)
    buys = sorted([q for q in quotes if q.side == "BUY"], key=lambda q: q.slot)
    assert buys[0].size >= buys[1].size >= buys[2].size


def test_ladder_single_level():
    quotes = ladder_quotes(_m(), _pair(), MarketMakerCfg(), LadderCfg(levels=1), 10)
    assert len(quotes) == 2


def test_ladder_respects_min_size():
    quotes = ladder_quotes(_m(), _pair(), MarketMakerCfg(), LadderCfg(levels=5, step_bps=5, size_decay=0.1), 1)
    for q in quotes:
        assert q.size >= 5  # min_order_size


def test_ladder_skips_levels_out_of_range():
    # Very narrow mid (0.005) → outer levels would go below tick.
    pair = QuotePair(bid=0.003, ask=0.997, spread=0.994, expected_score=0, reservation_price=0.5)
    quotes = ladder_quotes(_m(), pair, MarketMakerCfg(), LadderCfg(levels=5, step_bps=100), 10)
    # Only the first level is valid.
    assert all(0 < q.price < 1 for q in quotes)
