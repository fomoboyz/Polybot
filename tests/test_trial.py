import json
import time
from pathlib import Path

import pytest

from polybot.config import RiskCfg
from polybot.risk import RiskManager
from polybot.trial import TRIAL_DIR, TRIAL_STATE_FILE, TrialRunner


@pytest.fixture
def workdir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    yield tmp_path


def test_trial_creates_state_file(workdir):
    TrialRunner(duration_hours=1, checkpoint_interval_hours=0.1)
    assert TRIAL_STATE_FILE.exists()
    data = json.loads(TRIAL_STATE_FILE.read_text())
    assert data["duration_hours"] == 1


def test_trial_resumes(workdir):
    t1 = TrialRunner(duration_hours=1)
    start = t1.state.start_ts
    # Reload — should read the existing state, not reset.
    t2 = TrialRunner(duration_hours=99)  # duration_hours ignored on resume
    assert t2.state.start_ts == start


def test_fresh_flag_wipes_state(workdir):
    t1 = TrialRunner(duration_hours=1)
    start = t1.state.start_ts
    time.sleep(0.01)
    t2 = TrialRunner(duration_hours=1, fresh=True)
    assert t2.state.start_ts > start


def test_checkpoint_due_honors_interval(workdir):
    t = TrialRunner(duration_hours=1, checkpoint_interval_hours=1)
    assert t.checkpoint_due() is True  # last=0 initially


def test_record_checkpoint_writes_markdown(workdir):
    risk = RiskManager(RiskCfg())
    risk.state.realized_pnl_lifetime = 1.23
    risk.state.peak_pnl_lifetime = 1.23
    t = TrialRunner(duration_hours=1, checkpoint_interval_hours=0.01)
    t.record_checkpoint(risk, resting_orders=3)
    progress_files = list(TRIAL_DIR.glob("progress-*.md"))
    assert progress_files
    assert (TRIAL_DIR / "progress-latest.md").exists()
    assert len(t.state.checkpoints) == 1


def test_finalize_writes_pass_report(workdir):
    risk = RiskManager(RiskCfg())
    risk.state.realized_pnl_lifetime = 5.0
    risk.state.peak_pnl_lifetime = 5.0
    t = TrialRunner(duration_hours=1, starting_balance_usd=100)
    # Simulate accumulated checkpoints.
    t.state.checkpoints = [
        {"ts": t.state.start_ts + 3600, "pnl_lifetime": 2.0, "fills": 10, "volume_usd": 300},
        {"ts": t.state.start_ts + 7200, "pnl_lifetime": 5.0, "fills": 15, "volume_usd": 800},
    ]
    out = t.finalize(risk)
    body = Path(out).read_text()
    assert "Final Report" in body
    assert "PASS" in body or "STRONG" in body


def test_finalize_flags_fail(workdir):
    risk = RiskManager(RiskCfg())
    risk.state.realized_pnl_lifetime = -20.0
    risk.state.peak_pnl_lifetime = 0.0
    t = TrialRunner(duration_hours=1, starting_balance_usd=100)
    out = t.finalize(risk)
    assert "FAIL" in Path(out).read_text()


def test_should_exit_false_when_fresh(workdir):
    t = TrialRunner(duration_hours=1)
    assert t.should_exit() is False


def test_should_exit_true_when_past_end(workdir):
    t = TrialRunner(duration_hours=1)
    # Force expiry by rewriting start_ts to well in the past.
    t.state.start_ts = time.time() - 7200
    assert t.should_exit() is True
