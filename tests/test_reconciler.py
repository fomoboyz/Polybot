from unittest.mock import MagicMock

from polybot.config import ReconcilerCfg, RiskCfg
from polybot.models import Position
from polybot.reconciler import Reconciler
from polybot.risk import RiskManager


def _clob(orders):
    c = MagicMock()
    c.dry_run = False
    c.paper = False
    c.get_open_orders = MagicMock(return_value=orders)
    return c


def test_reconciler_disabled_no_op():
    r = RiskManager(RiskCfg())
    rec = Reconciler(ReconcilerCfg(enabled=False), _clob([]), r)
    result = rec.run()
    assert result.discrepancies == {}


def test_paper_mode_skips_reconcile():
    r = RiskManager(RiskCfg())
    c = MagicMock(); c.dry_run = False; c.paper = True
    rec = Reconciler(ReconcilerCfg(enabled=True), c, r)
    rec.run()
    c.get_open_orders.assert_not_called()


def test_detects_drift_and_auto_corrects():
    r = RiskManager(RiskCfg())
    r.state.positions["t1"] = Position(token_id="t1", net_shares=5.0)
    orders = [{"asset_id": "t1", "side": "BUY", "size_matched": 10.0}]
    rec = Reconciler(
        ReconcilerCfg(enabled=True, auto_correct=True, interval_sec=1),
        _clob(orders), r,
    )
    result = rec.run()
    assert "t1" in result.discrepancies
    assert r.state.positions["t1"].net_shares == 10.0


def test_drift_without_auto_correct_logged_not_fixed():
    r = RiskManager(RiskCfg())
    r.state.positions["t1"] = Position(token_id="t1", net_shares=5.0)
    orders = [{"asset_id": "t1", "side": "BUY", "size_matched": 10.0}]
    rec = Reconciler(
        ReconcilerCfg(enabled=True, auto_correct=False, interval_sec=1),
        _clob(orders), r,
    )
    result = rec.run()
    assert "t1" in result.discrepancies
    assert r.state.positions["t1"].net_shares == 5.0  # untouched
