# GPU Job Broker
 
A shared GPU job broker for a small AI research org: teams submit jobs, the
broker schedules them onto simulated cloud nodes, controls cost (idle
reaping, interruptible nodes), survives restarts, and reports spend per
team. It also replays a real week of submission history to compare two
scheduling policies, and answers free-text questions about past job
failures.
 
Everything runs on a **simulated clock** -- nothing sleeps, nothing ticks
in the background. You move time forward by calling an endpoint (or
running the replay script), and the broker processes every event that
happens in between, instantly. This is what lets a whole simulated week
run in a couple of seconds of real time.
 
> **Start here:** once the server is running (see Quickstart below), open
> **http://127.0.0.1:8000/** (it redirects to `/guide`) *before* `/docs`.
> It's a narrated, single-page walkthrough with rules, architecture
> diagrams, and "try it" boxes that call the real running service and show
> you the real response -- no terminal required. `/docs` (Swagger) is the
> detailed field-by-field reference for after.
 
See [`docs/DESIGN.md`](docs/DESIGN.md) for the written reasoning behind
every design decision (fairness, billing attribution, the retrieval
approach, the clock model, and the real bugs found while building this) --
`/guide`'s own "Full design rationale" section renders this exact document
live, so there's nothing to keep in sync between them.
 
## Quickstart
 
Prerequisites: Python 3.10+ and [`uv`](https://docs.astral.sh/uv/).
 
```bash
git clone https://github.com/Abhi5h3k/gpu-job-broker.git
cd gpu-job-broker
uv sync                              # installs everything into .venv, including dev/test deps
uv run uvicorn app.main:app --reload # starts the API at http://127.0.0.1:8000
```
 
Open **http://127.0.0.1:8000/docs** -- the Swagger page's top description is
written as a guided walkthrough of the whole flow, and every endpoint has
its own explanation of what it does and why you'd call it. You don't need
to read any code to use the service from there.
 
There is nothing else to install or configure -- no database server, no
external services, no API keys, no model downloads. The database is a
single SQLite file at `data/broker.db`, created automatically on first run
(and gitignored, so your local runs don't pollute the repo).
 
## Try these scenarios yourself
 
The full interactive version of every scenario below -- submit a job, hit
the 10-node cap, query retrieval, pull a cost report, with real responses
rendered right on the page -- lives at **`/guide`** once the server is
running. The one example here is just so a terminal-only skim of this repo
still shows the shape of the API without starting the server:
 
```bash
curl -s -X POST localhost:8000/jobs -H "Content-Type: application/json" \
  -d '{"team":"vision","gpus":1,"estimated_minutes":20,"priority":"normal"}'
# -> {"id": "job-xxxx", "state": "queued", ...}
 
curl -s -X POST localhost:8000/simulator/advance -H "Content-Type: application/json" -d '{"minutes": 30}'
curl -s localhost:8000/jobs/job-xxxx   # state: succeeded -- no "complete" call needed
```
 
For restart safety specifically (note a job id and `/costs` output, Ctrl+C
the server, start it again with the same command, confirm both are
unchanged and the clock resumed where it left off instead of from zero),
see `/guide`'s "Restart safety" section -- it can't be a clickable button
since it requires actually restarting the process.
 
## Running the tests
 
```bash
uv run pytest -v
```
 
36 tests across 7 files, each proving one of the scenarios called out in
the exercise:
 
| File | What it proves |
|---|---|
| `test_jobs_api.py` | End-to-end through the real FastAPI app: idempotent submission, the full automatic happy path, cancel (queued and running), the explicit manual-complete override, 404/409 error handling, the cost report's shape, and the oversized-GPU request rejected at the schema |
| `test_scheduler_fairness.py` | Aging prevents starvation, high priority both jumps the queue and gets a reliable (non-interruptible) node, the 10-node cap is enforced, the idle reaper fires, and two real bugs found during development stay fixed (oversized-GPU direct-to-broker guard, Latency-first actually using its own launched node) |
| `test_billing.py` | The 10-minute billing floor (hand-verified against the exact expected dollar amount), idle time attributed to the right team, a never-ready node billing only `platform_overhead`, and GPU-hours matching the doc's literal definition |
| `test_provider_failures.py` | A `CapacityUnavailable` burst leaves a job queued and bills nothing, a never-ready node requeues the job without ever marking it `failed`, and an interruptible reclaim freezes/resumes `remaining_run_minutes` without double-counting |
| `test_restart_safety.py` | A job mid-flight and the simulated clock both survive a full restart (fresh engine/session/clock/provider reading the same SQLite file) |
| `test_retrieval_precision.py` | Precision@3/@5 against all 24 labelled queries (correctly excluding the 6 with empty `expected_ids` from the average), plus 6 hand-written paraphrases *not* in the labelled set, to check generalization rather than memorization |
| `test_replay_smoke.py` | `replay.py` runs both policies on a slice of the real trace end-to-end without crashing, and the full 494-row trace parses and sorts correctly |
 
Run just one file with e.g. `uv run pytest tests/test_billing.py -v`.
 
## Running the trace replay (requirement 8: policy comparison)
 
```bash
uv run python replay.py data/01a_trace.csv
```
 
Feeds all 494 real job submissions from the trace into the broker, in
timestamp order, using the exact same scheduling code the live API uses --
no duplicated logic, no real sleeping. It does this twice: once under the
default **Balanced** policy, once under the alternative **Latency-first**
policy (always launch a dedicated node immediately, never share, never use
interruptible nodes -- literally what an ML engineer who only cares about
their own job's start time would ask for). For each run it prints wait-time
percentiles (p50/p90/p99) per team and total cost, so you can see the
cost/latency trade-off directly. Takes roughly a minute and a half of real
time for both full-week runs combined (replaying 494 jobs' worth of
scheduling decisions in Python is the dominant cost here -- the *simulated*
time itself, a full week twice over, is instant).
 
See [`docs/DESIGN.md`](docs/DESIGN.md#10-fairness-policy-balanced-vs-latency-first)
for the full result and why Balanced is the recommended default.
 
## Project layout
 
```
app/
  main.py        FastAPI app: lifespan startup, routers, the Swagger story
  config.py      All tunables in one place (node prices, timers, policy flags)
  clock.py       The simulated clock -- see docs/DESIGN.md section 4
  db.py          SQLAlchemy engine/session setup
  models.py      ORM schema: Job, Node, UsageSegment, JobEvent, ClockState
  schemas.py     Pydantic request/response models (drive the Swagger docs)
  provider.py    The fake cloud provider: launch delays, failures, reclaim
  billing.py     Per-second cost, the 10-minute floor, idle/overhead attribution
  scheduler.py   Placement, fairness/aging, the idle reaper -- pure functions, no DB writes
  broker.py      The orchestrator: ties the clock, scheduler, provider, and billing together
  deps.py        FastAPI dependency-injection glue
  retrieval.py   The TF-IDF + synonyms + LSA hybrid ranker
  guide.py       The /guide page -- served live, reads app/config.py and docs/DESIGN.md directly
  api/           One router file per resource: jobs, nodes, costs, simulator, retrieval
replay.py        CLI: trace replay + policy comparison
tests/           36 tests, one file per scenario group (see table above)
data/            The exercise's input files (trace, failure logs, labelled queries)
docs/DESIGN.md   Every design decision, written down with reasoning
```
 