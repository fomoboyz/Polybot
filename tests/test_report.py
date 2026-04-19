import time

from polybot.report import aggregate, render_markdown
from polybot.state import Store


def test_aggregate_handles_empty_db(tmp_path):
    db = tmp_path / "empty.sqlite"
    Store(str(db))  # creates schema
    agg = aggregate(str(db), hours=24)
    assert agg.orders_placed == 0
    assert agg.fills == 0
    assert agg.filled_volume_usd == 0.0


def test_aggregate_counts_fills(tmp_path):
    db = tmp_path / "t.sqlite"
    s = Store(str(db))
    now = time.time()
    s.record_order("o1", "tok", "BUY", 0.5, 10, now)
    s.record_order("o2", "tok", "SELL", 0.6, 10, now)
    s.record_fill("o1", "tok", "BUY", 0.5, 10, now)
    s.record_fill("o2", "tok", "SELL", 0.6, 10, now)
    agg = aggregate(str(db), hours=24)
    assert agg.orders_placed == 2
    assert agg.fills == 2
    assert round(agg.filled_volume_usd, 4) == round(0.5 * 10 + 0.6 * 10, 4)


def test_render_markdown_writes_file(tmp_path):
    db = tmp_path / "t.sqlite"
    Store(str(db))
    agg = aggregate(str(db), hours=1)
    out = tmp_path / "report.md"
    content = render_markdown(agg, out)
    assert out.exists()
    assert "# Polybot Performance" in content
