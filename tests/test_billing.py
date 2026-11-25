"""Billing math: the 10-minute-per-node floor, idle/overhead attribution,
and GPU-hours. These tests work out the expected numbers by hand (see the
comments) rather than just asserting "it ran without crashing."
"""

from __future__ import annotations

import pytest

from app.billing import compute_cost_report
from app.config import NODE_READY_MIN_MINUTES, SchedulingPolicy
from app.models import JobState, Priority
from tests.conftest import START, make_broker


def test_short_job_is_billed_the_ten_minute_floor_not_actual_time():
    """A node that's only actually alive for a couple of minutes must
    still be billed for the full 10-minute floor (doc: "Billing is per
    second with a 10-minute minimum per node"). Uses HIGH priority so the
    node is on-demand at the plain catalog price ($1.00/hr for gpu-s),
    keeping the expected-cost math simple; a short idle_reaper_minutes
    makes the node tear down well before 10 real minutes have passed, so
    the floor is actually what's being exercised here.
    """
    broker = make_broker(policy=SchedulingPolicy.BALANCED, idle_reaper_minutes=1)
    job = broker.submit_job(team="vision", gpus=1, estimated_minutes=1, priority=Priority.HIGH)
    # Boot (4 min, scripted) + run 1 minute + the 1-minute idle grace period.
    broker.advance(10)
    assert broker.get_job(job.id).state == JobState.SUCCEEDED

    node = broker.list_nodes()[0]
    assert node.is_interruptible is False
    actual_lifetime = (node.terminated_at - node.launched_at).total_seconds()
    assert actual_lifetime < 600, (
        "the node's real lifetime should be well under the 10-minute floor"
    )

    report = compute_cost_report(broker.session, START, broker.clock.now, broker.clock.now)
    # gpu-s on-demand is $1.00/hr -> the 10-minute floor costs exactly
    # 1.00 * (600/3600) = $0.1667, no matter how the floor-stretched time
    # is split across running/idle/overhead segments.
    assert report.total_cost == pytest.approx(1.00 * 600 / 3600, rel=1e-6)


def test_idle_time_is_billed_to_the_team_that_last_ran_there():
    broker = make_broker(policy=SchedulingPolicy.BALANCED, idle_reaper_minutes=100)
    job = broker.submit_job(team="vision", gpus=1, estimated_minutes=5, priority=Priority.NORMAL)
    broker.advance(
        NODE_READY_MIN_MINUTES + 5 + 10
    )  # boot + run + sit idle a while (not yet reaped)
    assert broker.get_job(job.id).state == JobState.SUCCEEDED

    report = compute_cost_report(broker.session, START, broker.clock.now, broker.clock.now)
    vision = report.teams["vision"]
    assert vision.idle_cost > 0, "idle time after vision's job finished should be billed to vision"
    assert report.platform_overhead_cost > 0, (
        "boot time before the job started should be platform overhead"
    )


def test_never_ready_node_bills_only_platform_overhead_never_a_team():
    from tests.conftest import make_provider

    # request_capacity consumes one .random() roll (failure-probability
    # check only -- the incident-start hazard is now ticked separately,
    # once per simulated minute, by the broker's heartbeat). The first
    # value here keeps the first launch's capacity check passing; the
    # second forces plan_launch's never-ready branch; the third is sticky
    # for the retry that follows, so it succeeds cleanly instead of
    # failing forever.
    provider = make_provider(random_values=[0.99, 0.0, 0.99])
    broker = make_broker(policy=SchedulingPolicy.BALANCED, provider=provider)
    job = broker.submit_job(team="vision", gpus=1, estimated_minutes=5, priority=Priority.NORMAL)
    broker.advance(
        16
    )  # just past the 15-minute never-ready report deadline, before a retry can land

    refreshed = broker.get_job(job.id)
    assert refreshed.state != JobState.FAILED, (
        "the job must be requeued, not blamed for the provider's failure"
    )
    report = compute_cost_report(broker.session, START, broker.clock.now, broker.clock.now)
    assert "vision" not in report.teams or report.teams["vision"].total_cost == 0
    assert report.platform_overhead_cost > 0


def test_gpu_hours_matches_the_docs_literal_definition():
    """Doc: GPU-hours = GPUs requested * hours run. A 4-GPU job that runs
    for exactly 30 minutes should report 4 * 0.5 = 2.0 GPU-hours."""
    broker = make_broker(policy=SchedulingPolicy.BALANCED)
    job = broker.submit_job(team="speech", gpus=4, estimated_minutes=30, priority=Priority.NORMAL)
    broker.advance(NODE_READY_MIN_MINUTES + 30 + 1)
    assert broker.get_job(job.id).state == JobState.SUCCEEDED

    report = compute_cost_report(broker.session, START, broker.clock.now, broker.clock.now)
    assert report.teams["speech"].gpu_hours == pytest.approx(4 * 0.5, rel=1e-3)
