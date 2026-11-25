"""Scheduling policy behavior: fairness/aging anti-starvation under
Balanced, the max_nodes cap, the idle reaper, and the direct-to-broker
oversized-GPU guard (bypassing the API schema, the way replay.py does).
"""

from __future__ import annotations

from app.config import SchedulingPolicy
from app.models import JobState, Node, NodeState, Priority
from tests.conftest import make_broker


def test_low_priority_job_is_not_starved_forever_under_balanced():
    """A low-priority job submitted first, behind a stream of normal-
    priority jobs from a different team, must eventually age up and get a
    turn -- this is the whole point of the aging mechanism (doc:
    "starvation ... should not occur")."""
    broker = make_broker(policy=SchedulingPolicy.BALANCED, max_nodes=1)
    low = broker.submit_job(team="low_team", gpus=1, estimated_minutes=5, priority=Priority.LOW)

    # Flood the single node slot with a steady stream of short normal
    # jobs from a different team so the queue is never naturally empty.
    for _ in range(40):
        broker.submit_job(team="busy_team", gpus=1, estimated_minutes=5, priority=Priority.NORMAL)
        broker.advance(10)

    refreshed = broker.get_job(low.id)
    assert refreshed.state in (JobState.RUNNING, JobState.SUCCEEDED), (
        "a low-priority job must not be starved indefinitely once enough time has passed to age it up"
    )


def test_high_priority_job_jumps_ahead_of_queued_normal_jobs():
    broker = make_broker(policy=SchedulingPolicy.BALANCED, max_nodes=1)
    # Occupy the only node slot so both new submissions queue up behind it.
    broker.submit_job(team="a", gpus=1, estimated_minutes=100, priority=Priority.NORMAL)
    broker.advance(10)  # node boots and starts running the occupant

    normal_job = broker.submit_job(team="b", gpus=1, estimated_minutes=5, priority=Priority.NORMAL)
    high_job = broker.submit_job(team="c", gpus=1, estimated_minutes=5, priority=Priority.HIGH)

    ordered = broker.list_queue()
    ordered_ids = [j.id for j in ordered]
    assert ordered_ids.index(high_job.id) < ordered_ids.index(normal_job.id)


def test_high_priority_job_gets_an_on_demand_not_interruptible_node():
    """Doc: "high priority" must mean something beyond queue order. The
    Balanced policy's answer: high-priority jobs always get a reliable
    on-demand node; everything else prefers a cheaper interruptible one."""
    broker = make_broker(policy=SchedulingPolicy.BALANCED)
    job = broker.submit_job(team="a", gpus=1, estimated_minutes=5, priority=Priority.HIGH)
    broker.advance(6)  # past the 4-minute scripted boot time, but before the 5-minute job finishes
    running = broker.get_job(job.id)
    assert running.state == JobState.RUNNING
    node = broker.session.get(Node, running.node_id)
    assert node.is_interruptible is False


def test_max_nodes_cap_is_enforced():
    broker = make_broker(policy=SchedulingPolicy.BALANCED, max_nodes=2)
    jobs = [
        broker.submit_job(team=f"team{i}", gpus=1, estimated_minutes=200, priority=Priority.NORMAL)
        for i in range(5)
    ]
    broker.advance(10)  # enough for any launched nodes to become ready and start running

    alive = [n for n in broker.list_nodes() if n.state in (NodeState.BOOTING, NodeState.READY)]
    assert len(alive) <= 2

    running = sum(1 for j in jobs if broker.get_job(j.id).state == JobState.RUNNING)
    queued = sum(1 for j in jobs if broker.get_job(j.id).state == JobState.QUEUED)
    assert running <= 2
    assert running + queued == 5


def test_idle_node_is_reaped_after_the_configured_grace_period():
    broker = make_broker(policy=SchedulingPolicy.BALANCED, idle_reaper_minutes=15)
    job = broker.submit_job(team="a", gpus=1, estimated_minutes=5, priority=Priority.NORMAL)
    broker.advance(30)  # boot + run its full 5 minutes + sit idle past the 15-minute grace period
    assert broker.get_job(job.id).state == JobState.SUCCEEDED

    nodes = broker.list_nodes()
    assert all(n.state == NodeState.TERMINATED for n in nodes)


def test_oversized_gpu_request_fails_immediately_instead_of_queueing_forever():
    """A request for more GPUs than the biggest node type (8) can never be
    placed. This can only happen via a direct caller that bypasses the
    live API's schema -- like replay.py, replaying a trace row that
    happens to ask for 16 GPUs. It must fail fast, not hang the scheduler
    or sit queued forever."""
    broker = make_broker(policy=SchedulingPolicy.BALANCED)
    job = broker.submit_job(team="a", gpus=16, estimated_minutes=10, priority=Priority.NORMAL)
    assert job.state == JobState.FAILED
    assert job.completed_at is not None


def test_latency_first_actually_uses_the_dedicated_node_it_launches():
    """Regression test for a real bug found while building replay.py:
    Latency-first never shares nodes, so a job can only ever be placed on
    the specific node that was launched for it. The scheduler must notice
    when that reserved node becomes READY and place the job there --
    without this, the job would sit queued forever while its own node
    idles, unused."""
    broker = make_broker(policy=SchedulingPolicy.LATENCY_FIRST)
    job = broker.submit_job(team="a", gpus=1, estimated_minutes=5, priority=Priority.NORMAL)
    broker.advance(10)  # longer than the max 8-minute boot time
    assert broker.get_job(job.id).state in (JobState.RUNNING, JobState.SUCCEEDED)
