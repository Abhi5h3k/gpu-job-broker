"""POST /admin/reset: wipes all data and starts the simulated world over,
without needing to restart the process or delete the database file."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.test_jobs_api import _fresh_app, advance, submit


@pytest.fixture
def client(tmp_path):
    main_module = _fresh_app(str(tmp_path / "test.db"))
    with TestClient(main_module.app) as c:
        yield c


def test_reset_clears_jobs_and_nodes(client):
    job = submit(client, estimated_minutes=20).json()
    advance(client, 40)
    assert client.get(f"/jobs/{job['id']}").json()["state"] == "succeeded"
    assert client.get("/nodes").json()

    r = client.post("/admin/reset")
    assert r.status_code == 200
    assert "reset_at" in r.json()

    assert client.get(f"/jobs/{job['id']}").status_code == 404
    assert client.get("/jobs").json() == []
    assert client.get("/nodes").json() == []


def test_reset_puts_clock_back_near_now(client):
    submit(client, estimated_minutes=20)
    advance(client, 10_000)  # jump the simulated clock far into the future

    r = client.post("/admin/reset")
    reset_at = r.json()["reset_at"]

    report = client.get("/costs").json()
    # window_end defaults to the (now-reset) clock's current time -- it
    # should be back near wall-clock "now", not still far in the future.
    assert report["window_end"][:10] == reset_at[:10]


def test_can_submit_and_run_jobs_again_after_reset(client):
    submit(client)
    client.post("/admin/reset")

    job = submit(client, team="vision", estimated_minutes=5).json()
    advance(client, 20)
    assert client.get(f"/jobs/{job['id']}").json()["state"] == "succeeded"
