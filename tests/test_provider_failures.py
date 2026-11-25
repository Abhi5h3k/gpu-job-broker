"""The three ways the fake provider can misbehave, each forced
deterministically via ScriptedRandom instead of hoping a seed cooperates:
a capacity-unavailable burst, a node that never becomes ready, and an
interruptible node getting reclaimed mid-job.
"""

from __future__ import annotations

import pytest

from app.config import NODE_READY_MIN_MINUTES, SchedulingPolicy
from app.models import JobState, NodeState, Priority
from tests.conftest import make_broker, make_provider


def test_capacity_unavailable_leaves_job_queued_and_bills_nothing():
    """First attempt: force an isolated (non-incident) denial. Then let
    the heartbeat's next incident-start tick fire, so the retry lands
    during an active incident and succeeds anyway (0.99 clears even the
    higher during-incident failure threshold). The job must stay queued
    (not fail) throughout, and nothing should be billed for the denied
    attempt since no node was ever created for it."""
    provider = make_provider(
        random_values=[0.0, 0.0, 0.99]
    )  # denied, incident starts, retry succeeds
    broker = make_broker(policy=SchedulingPolicy.BALANCED, provider=provider)
    job = broker.submit_job(team="vision", gpus=1, estimated_minutes=5, priority=Priority.NORMAL)

    assert job.state == JobState.QUEUED
    assert broker.list_nodes() == [], "a denied launch must not create a node row"

    # The heartbeat keeps retrying once a job is capacity-blocked; give it
    # enough simulated time for a later retry to succeed and the job to run.
    broker.advance(20)
    assert broker.get_job(job.id).state in (JobState.RUNNING, JobState.SUCCEEDED)


def test_never_ready_node_requeues_the_job_without_failing_it():
    # See test_billing.py's never-ready test for why this needs 3 values:
    # one to keep the first launch's capacity check passing, one to force
    # the never-ready branch, one sticky value so the retry afterwards
    # succeeds cleanly.
    provider = make_provider(random_values=[0.99, 0.0, 0.99])
    broker = make_broker(policy=SchedulingPolicy.BALANCED, provider=provider)
    job = broker.submit_job(team="vision", gpus=1, estimated_minutes=5, priority=Priority.NORMAL)

    broker.advance(14)  # just before the 15-minute never-ready report deadline
    assert broker.get_job(job.id).state == JobState.QUEUED
    assert broker.list_nodes()[0].state == NodeState.BOOTING

    broker.advance(2)  # now past the deadline
    first_node = broker.list_nodes()[0]
    assert first_node.state == NodeState.FAILED
    assert broker.get_job(job.id).state != JobState.FAILED, "the job itself must not be blamed"

    broker.advance(10)  # the retried launch (scripted to succeed) has time to land
    assert broker.get_job(job.id).state in (JobState.RUNNING, JobState.SUCCEEDED)


def test_interruptible_node_reclaim_pauses_and_resumes_the_job():
    """A job on an interruptible node that gets reclaimed must be requeued
    with its remaining run time preserved (frozen, not reset) -- it should
    not have to start over, and should not be billed twice for the same
    work. We let it run a while on a never-reclaiming provider, then force
    a reclaim by directly scripting the next random roll to fire."""
    provider = make_provider(
        random_values=[0.99]
    )  # never fails/never-ready/reclaims until we say so
    broker = make_broker(policy=SchedulingPolicy.BALANCED, provider=provider)
    job = broker.submit_job(team="vision", gpus=1, estimated_minutes=30, priority=Priority.NORMAL)
    broker.advance(NODE_READY_MIN_MINUTES + 5)  # boot, then run for part of its 30 minutes
    running = broker.get_job(job.id)
    assert running.state == JobState.RUNNING
    node = broker.list_nodes()[0]
    assert node.is_interruptible is True, (
        "non-HIGH-priority jobs should land on an interruptible node"
    )

    # The next heartbeat tick rolls the incident-start check *before* the
    # reclaim check (see broker.py's _process_heartbeat), so the first
    # value here just needs to clear that harmlessly (0.99, no incident);
    # the second (0.0) forces that same heartbeat's reclaim roll to fire;
    # every roll after that (future incident ticks, the retry launch's
    # checks) succeeds cleanly via the sticky 0.99 so the job can actually
    # resume and finish.
    provider._rng._random_values = [0.99, 0.0, 0.99]  # type: ignore[attr-defined]
    broker.advance(3)

    reclaimed = broker.get_job(job.id)
    assert reclaimed.requeue_count == 1
    assert reclaimed.state == JobState.QUEUED
    assert 0 < reclaimed.remaining_run_minutes < 30, (
        "remaining time must be frozen partway through, not reset to 30 or dropped to 0"
    )

    # Let it find a new node and finish out its remaining time. The total
    # time actually spent running, across both nodes, must equal the job's
    # full duration exactly -- no double-counting, no lost time.
    broker.advance(60)
    finished = broker.get_job(job.id)
    assert finished.state == JobState.SUCCEEDED
    assert finished.total_run_minutes_accrued == pytest.approx(30, rel=1e-6)
