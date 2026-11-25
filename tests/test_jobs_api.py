"""End-to-end tests against the real FastAPI app (the same code path a
real client hits), using a fresh temporary SQLite file per test so tests
never interfere with each other or with a developer's own data/broker.db.
"""

from __future__ import annotations

import sys

import pytest
from fastapi.testclient import TestClient


def _fresh_app(db_path: str):
    """Drop every cached ``app.*`` module and re-import from scratch with
    ``BROKER_DB_PATH`` pointing at a fresh file. Needed because
    app/config.py's ``settings`` and app/db.py's ``engine`` are both
    created once at import time -- a plain re-import would keep reusing
    whatever database the first test in the process happened to create.
    """
    import os

    os.environ["BROKER_DB_PATH"] = db_path
    for name in list(sys.modules):
        if name == "app" or name.startswith("app."):
            del sys.modules[name]
    import app.main as main_module

    return main_module


@pytest.fixture
def client(tmp_path):
    main_module = _fresh_app(str(tmp_path / "test.db"))
    with TestClient(main_module.app) as c:
        yield c


def submit(client, **overrides):
    payload = {"team": "vision", "gpus": 1, "estimated_minutes": 20, "priority": "normal"}
    payload.update(overrides)
    return client.post("/jobs", json=payload)


def advance(client, minutes):
    return client.post("/simulator/advance", json={"minutes": minutes})


def test_submit_is_idempotent_on_client_key(client):
    r1 = submit(client, client_key="abc-123")
    r2 = submit(client, client_key="abc-123")
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["id"] == r2.json()["id"]

    # And the duplicate attempt did not create a second row in the queue.
    r3 = submit(client, client_key="abc-123")
    listing = client.get("/jobs").json()
    matching = [j for j in listing if j["id"] == r1.json()["id"]]
    assert len(matching) == 1
    assert r3.json()["id"] == r1.json()["id"]


def test_happy_path_job_runs_to_completion_automatically(client):
    job = submit(client, estimated_minutes=20).json()

    # Give it enough simulated time to boot (up to 8 min) and then run for
    # its full 20 minutes -- no explicit "complete" call needed.
    advance(client, 40)

    detail = client.get(f"/jobs/{job['id']}").json()
    assert detail["state"] == "succeeded"
    assert detail["started_at"] is not None
    assert detail["completed_at"] is not None
    states_seen = [e["state"] for e in detail["events"]]
    assert states_seen == ["queued", "running", "succeeded"]


def test_cancel_queued_job_before_it_ever_runs(client):
    job = submit(client).json()
    r = client.post(f"/jobs/{job['id']}/cancel")
    assert r.status_code == 200
    assert r.json()["state"] == "cancelled"
    assert r.json()["started_at"] is None


def test_cancel_running_job_releases_its_node(client):
    job = submit(client, estimated_minutes=60).json()
    advance(client, 10)  # long enough to be running, not long enough to finish
    running = client.get(f"/jobs/{job['id']}").json()
    assert running["state"] == "running"

    r = client.post(f"/jobs/{job['id']}/cancel")
    assert r.status_code == 200
    assert r.json()["state"] == "cancelled"

    # The node it was on should now be reap-able -- another job's cost
    # report should not include endless running cost for the cancelled job.
    nodes = client.get("/nodes").json()
    assert any(n["id"] == running["node_id"] for n in nodes)


def test_cancel_already_terminal_job_is_rejected(client):
    job = submit(client, estimated_minutes=5).json()
    advance(client, 20)
    assert client.get(f"/jobs/{job['id']}").json()["state"] == "succeeded"

    r = client.post(f"/jobs/{job['id']}/cancel")
    assert r.status_code == 409


def test_manual_complete_ends_a_running_job_early(client):
    job = submit(client, estimated_minutes=600).json()
    advance(client, 10)
    assert client.get(f"/jobs/{job['id']}").json()["state"] == "running"

    r = client.post(f"/jobs/{job['id']}/complete", json={"outcome": "failed"})
    assert r.status_code == 200
    assert r.json()["state"] == "failed"


def test_get_unknown_job_is_404(client):
    assert client.get("/jobs/job-does-not-exist").status_code == 404


def test_cost_report_has_sensible_shape_after_a_run(client):
    submit(client, team="vision", estimated_minutes=20)
    advance(client, 40)

    report = client.get("/costs").json()
    assert report["total_cost"] > 0
    teams = {t["team"]: t for t in report["teams"]}
    assert "vision" in teams
    assert teams["vision"]["gpu_hours"] > 0
    assert teams["vision"]["total_cost"] == pytest.approx(
        teams["vision"]["running_cost"] + teams["vision"]["idle_cost"]
    )


def test_retrieval_search_returns_ranked_results(client):
    r = client.post(
        "/retrieval/search", json={"text": "loss turned to NaN during training", "top_k": 3}
    )
    assert r.status_code == 200
    results = r.json()["results"]
    assert len(results) == 3
    assert results[0]["score"] >= results[1]["score"] >= results[2]["score"]


def test_list_jobs_defaults_to_queue_only(client):
    done = submit(client, team="vision", estimated_minutes=5).json()
    advance(client, 20)
    assert client.get(f"/jobs/{done['id']}").json()["state"] == "succeeded"

    # Nothing is queued anymore -- the default (no filters) listing should
    # be empty, confirming it still only reflects the live queue, not every
    # job ever submitted.
    assert client.get("/jobs").json() == []


def test_list_jobs_filters_by_state_across_all_jobs(client):
    done = submit(client, estimated_minutes=5).json()
    advance(client, 20)
    cancelled = submit(client, estimated_minutes=5).json()
    client.post(f"/jobs/{cancelled['id']}/cancel")

    succeeded = client.get("/jobs", params={"state": "succeeded"}).json()
    assert [j["id"] for j in succeeded] == [done["id"]]

    multi = client.get("/jobs", params={"state": ["succeeded", "cancelled"]}).json()
    assert {j["id"] for j in multi} == {done["id"], cancelled["id"]}


def test_list_jobs_filters_by_team(client):
    vision_job = submit(client, team="vision", estimated_minutes=5).json()
    submit(client, team="nlp", estimated_minutes=5)

    result = client.get("/jobs", params={"team": "vision", "state": "queued"}).json()
    assert [j["id"] for j in result] == [vision_job["id"]]


def test_gpu_count_outside_the_supported_sizes_is_rejected_at_the_door(client):
    """The schema only accepts the node catalog's sizes (1/2/4/8); anything
    else -- including a request bigger than the biggest node type -- is
    rejected with a 422 before it ever reaches the scheduler. See
    test_scheduler_fairness.py for what happens if an oversized request
    reaches the broker directly (e.g. via trace replay, which bypasses
    this schema)."""
    r = submit(client, gpus=16)
    assert r.status_code == 422
