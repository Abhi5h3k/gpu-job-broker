"""Smoke test for replay.py: runs a small slice of the real trace through
both policies and checks nothing crashes and the output makes basic
sense. The full 494-row trace is exercised manually (see README.md) --
this keeps the automated suite fast.
"""

from __future__ import annotations

from pathlib import Path

from app.config import SchedulingPolicy
from replay import load_trace, run_policy, summarize

TRACE_PATH = Path(__file__).resolve().parent.parent / "data" / "01a_trace.csv"


def test_replay_runs_both_policies_on_a_slice_of_the_real_trace():
    rows = load_trace(TRACE_PATH)[:80]
    assert len(rows) == 80

    for policy in (SchedulingPolicy.BALANCED, SchedulingPolicy.LATENCY_FIRST):
        session, broker, job_ids = run_policy(rows, policy)
        assert len(job_ids) == 80
        summary = summarize(session, broker, job_ids)

        assert summary["total_cost"] > 0
        assert sum(summary["outcomes"].values()) == 80
        for stats in summary["wait_by_team"].values():
            assert stats["p50"] <= stats["p90"] <= stats["p99"]
        session.close()


def test_load_trace_handles_mixed_timezone_offsets_and_is_sorted():
    rows = load_trace(TRACE_PATH)
    assert len(rows) == 494
    assert rows == sorted(rows, key=lambda r: r.submitted_at)
