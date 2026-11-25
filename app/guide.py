"""The `/guide` page: a single, self-contained HTML document served by the
running app itself (no build step, no external assets -- it works offline,
same as everything else in this service).

This exists so there is exactly one place that is both (a) the narrative
"read this first" walkthrough with diagrams and runnable examples, and
(b) guaranteed to never drift from the real running app, because its two
"data" endpoints (``/guide/config`` and ``/guide/design-note``) read
straight from ``app/config.py`` / ``app/models.py`` and from
``docs/DESIGN.md`` on disk, respectively -- nothing here is a hand-copied
duplicate of either. See README.md for why this isn't counted as a third
hand-maintained doc to keep in sync.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, PlainTextResponse

from app.config import (
    CAPACITY_FAILURE_PROBABILITY_DURING_INCIDENT,
    CAPACITY_FAILURE_PROBABILITY_NORMAL,
    CAPACITY_INCIDENT_MAX_DURATION_MINUTES,
    CAPACITY_INCIDENT_MIN_DURATION_MINUTES,
    CAPACITY_INCIDENT_START_PROBABILITY_PER_MINUTE,
    INTERRUPTIBLE_PRICE_FACTOR,
    INTERRUPTIBLE_RECLAIM_NOTICE_MINUTES,
    INTERRUPTIBLE_RECLAIM_TARGET_FRACTION,
    MINIMUM_BILLED_SECONDS,
    NEVER_READY_PROBABILITY,
    NEVER_READY_REPORT_AFTER_MINUTES,
    NODE_CATALOG,
    NODE_READY_MAX_MINUTES,
    NODE_READY_MIN_MINUTES,
    SchedulingPolicy,
    settings,
)
from app.models import JobState, NodeState, Priority

router = APIRouter(tags=["0. Guide"])

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DESIGN_NOTE_PATH = _REPO_ROOT / "docs" / "DESIGN.md"


@router.get("/guide/config", include_in_schema=False)
def guide_config() -> dict:
    """Live snapshot of every tunable/allowed-value the guide page's
    "Rules we follow" tables render -- read directly from ``app.config``
    and ``app.models``, so these tables can never silently drift from what
    the running service actually enforces."""
    return {
        "node_catalog": {
            node_type.value: {
                "gpus": spec.gpus,
                "on_demand_price_per_hour": spec.price_per_hour,
                "interruptible_price_per_hour": round(
                    spec.price_per_hour * INTERRUPTIBLE_PRICE_FACTOR, 4
                ),
            }
            for node_type, spec in NODE_CATALOG.items()
        },
        "interruptible_price_factor": INTERRUPTIBLE_PRICE_FACTOR,
        "minimum_billed_seconds": MINIMUM_BILLED_SECONDS,
        "node_ready_minutes": {
            "min": NODE_READY_MIN_MINUTES,
            "max": NODE_READY_MAX_MINUTES,
        },
        "never_ready_probability": NEVER_READY_PROBABILITY,
        "never_ready_report_after_minutes": NEVER_READY_REPORT_AFTER_MINUTES,
        "interruptible_reclaim": {
            "target_fraction_of_node_hours": INTERRUPTIBLE_RECLAIM_TARGET_FRACTION,
            "notice_minutes": INTERRUPTIBLE_RECLAIM_NOTICE_MINUTES,
        },
        "capacity_incidents": {
            "start_probability_per_minute": CAPACITY_INCIDENT_START_PROBABILITY_PER_MINUTE,
            "min_duration_minutes": CAPACITY_INCIDENT_MIN_DURATION_MINUTES,
            "max_duration_minutes": CAPACITY_INCIDENT_MAX_DURATION_MINUTES,
            "failure_probability_normal": CAPACITY_FAILURE_PROBABILITY_NORMAL,
            "failure_probability_during_incident": CAPACITY_FAILURE_PROBABILITY_DURING_INCIDENT,
        },
        "idle_reaper_minutes": settings.idle_reaper_minutes,
        "max_nodes": settings.max_nodes,
        "scheduling_policy": settings.scheduling_policy.value,
        "scheduling_policies_available": [p.value for p in SchedulingPolicy],
        "isolate_teams_per_node": settings.isolate_teams_per_node,
        "allowed_gpus": [1, 2, 4, 8],
        "allowed_priorities": [p.value for p in Priority],
        "job_states": [s.value for s in JobState],
        "node_states": [s.value for s in NodeState],
    }


@router.get("/guide/design-note", include_in_schema=False)
def guide_design_note() -> PlainTextResponse:
    """The literal contents of docs/DESIGN.md, served as-is so the guide
    page's "Design decisions" section can render the *real* document
    instead of a paraphrased, driftable copy of it."""
    text = _DESIGN_NOTE_PATH.read_text(encoding="utf-8")
    return PlainTextResponse(text, media_type="text/markdown")


@router.get("/guide", include_in_schema=False, response_class=HTMLResponse)
def guide_page() -> str:
    return _PAGE_HTML


_PAGE_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>GPU Job Broker &mdash; Guide</title>
<style>
:root {
  --ks-orange: #FF6B00;
  --ks-orange-dark: #CC5500;
  --ks-orange-tint: #FFF1E6;
  --ks-navy: #171B26;
  --ks-navy-soft: #262B3A;
  --ks-grey-bg: #F7F8FA;
  --ks-grey-line: #E3E5EA;
  --ks-text: #2B2E36;
  --ks-text-soft: #6B6F7B;
  --ks-white: #FFFFFF;
  --ks-green: #1F8A4C;
  --ks-red: #C4291C;
  --mono: "SFMono-Regular", Consolas, "Liberation Mono", Menlo, monospace;
  --sans: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
}
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; }
body {
  font-family: var(--sans);
  color: var(--ks-text);
  background: var(--ks-grey-bg);
  line-height: 1.55;
}
a { color: var(--ks-orange-dark); }
code, pre { font-family: var(--mono); }
code {
  background: var(--ks-orange-tint);
  padding: 0.1em 0.4em;
  border-radius: 4px;
  font-size: 0.92em;
  white-space: nowrap;
}
pre {
  background: var(--ks-navy);
  color: #E8EAF0;
  padding: 14px 16px;
  border-radius: 8px;
  overflow-x: auto;
  font-size: 0.86em;
}
pre code { background: none; padding: 0; color: inherit; white-space: pre; }
 
/* ---- Top bar ---- */
#topbar {
  position: sticky; top: 0; z-index: 20;
  display: flex; align-items: center; gap: 12px;
  background: var(--ks-navy);
  color: var(--ks-white);
  padding: 12px 20px;
  border-bottom: 3px solid var(--ks-orange);
}
#topbar .brand { font-weight: 700; font-size: 1.05rem; letter-spacing: 0.02em; }
#topbar .brand .k { color: var(--ks-orange); }
#topbar .tag { color: #B9BECB; font-size: 0.85rem; }
#topbar .spacer { flex: 1; }
#topbar a.pill {
  color: var(--ks-white);
  background: var(--ks-navy-soft);
  border: 1px solid #3A4053;
  padding: 6px 12px;
  border-radius: 999px;
  text-decoration: none;
  font-size: 0.85rem;
  white-space: nowrap;
}
#topbar a.pill:hover { background: var(--ks-orange); border-color: var(--ks-orange); }
 
/* ---- Layout ---- */
#layout { display: flex; align-items: flex-start; min-height: calc(100vh - 54px); }
nav#sidenav {
  position: sticky; top: 54px;
  width: 250px; flex: 0 0 250px;
  height: calc(100vh - 54px);
  overflow-y: auto;
  background: var(--ks-white);
  border-right: 1px solid var(--ks-grey-line);
  padding: 18px 0 40px;
}
nav#sidenav .group-label {
  font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.08em;
  color: var(--ks-text-soft); padding: 14px 20px 6px;
}
nav#sidenav a {
  display: block;
  padding: 7px 20px;
  color: var(--ks-text);
  text-decoration: none;
  font-size: 0.9rem;
  border-left: 3px solid transparent;
}
nav#sidenav a:hover { background: var(--ks-orange-tint); }
nav#sidenav a.active {
  border-left-color: var(--ks-orange);
  color: var(--ks-orange-dark);
  font-weight: 600;
  background: var(--ks-orange-tint);
}
main { flex: 1; min-width: 0; padding: 32px 40px 100px; max-width: 980px; }
 
section.doc-section { margin-bottom: 56px; scroll-margin-top: 70px; }
section.doc-section h2 {
  font-size: 1.5rem; margin: 0 0 6px;
  border-bottom: 2px solid var(--ks-orange); padding-bottom: 8px;
}
section.doc-section h3 { font-size: 1.1rem; margin-top: 28px; }
.step-badge {
  display: inline-block; background: var(--ks-orange); color: white;
  font-size: 0.72rem; font-weight: 700; padding: 2px 9px; border-radius: 999px;
  margin-right: 8px; vertical-align: middle;
}
.lede { color: var(--ks-text-soft); font-size: 1.02rem; }
 
table.rules {
  width: 100%; border-collapse: collapse; margin: 14px 0; font-size: 0.9rem;
}
table.rules th, table.rules td {
  border: 1px solid var(--ks-grey-line); padding: 8px 10px; text-align: left;
}
table.rules th { background: var(--ks-navy); color: white; font-weight: 600; }
table.rules tr:nth-child(even) td { background: #FBFBFC; }
table.rules code { white-space: nowrap; }
 
.card {
  background: var(--ks-white);
  border: 1px solid var(--ks-grey-line);
  border-radius: 10px;
  padding: 18px 20px;
  margin: 16px 0;
}
.card.rule { border-left: 4px solid var(--ks-orange); }
.card.warn { border-left: 4px solid var(--ks-red); background: #FFF6F5; }
 
.tryit {
  background: var(--ks-white);
  border: 1px solid var(--ks-grey-line);
  border-left: 4px solid var(--ks-orange);
  border-radius: 10px;
  padding: 18px 20px;
  margin: 18px 0;
}
.tryit h4 {
  margin: 0 0 4px; font-size: 0.95rem;
  display: flex; align-items: center; gap: 8px;
}
.tryit h4 .verb {
  font-family: var(--mono); font-size: 0.72rem; font-weight: 700;
  background: var(--ks-navy); color: white; padding: 2px 8px; border-radius: 4px;
}
.tryit p.desc { color: var(--ks-text-soft); margin: 4px 0 12px; font-size: 0.88rem; }
.tryit .row { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 10px; align-items: flex-end; }
.tryit label { display: flex; flex-direction: column; font-size: 0.75rem; color: var(--ks-text-soft); gap: 4px; }
.tryit input, .tryit select, .tryit textarea {
  font-family: var(--sans); font-size: 0.88rem; padding: 6px 8px;
  border: 1px solid var(--ks-grey-line); border-radius: 6px; min-width: 120px;
}
.tryit textarea { min-width: 320px; min-height: 54px; font-family: var(--mono); }
.tryit button {
  background: var(--ks-orange); color: white; border: none;
  padding: 8px 16px; border-radius: 6px; font-weight: 600; cursor: pointer;
  font-size: 0.88rem;
}
.tryit button:hover { background: var(--ks-orange-dark); }
.tryit .out {
  margin-top: 10px; display: none;
  background: var(--ks-navy); color: #E8EAF0;
  padding: 10px 12px; border-radius: 6px;
  font-family: var(--mono); font-size: 0.82rem;
  white-space: pre-wrap; word-break: break-word;
  max-height: 320px; overflow-y: auto;
}
.tryit .out.show { display: block; }
.tryit .out.err { border-left: 4px solid var(--ks-red); }
.tryit .out.ok { border-left: 4px solid var(--ks-green); }
 
.diagram { background: var(--ks-white); border: 1px solid var(--ks-grey-line); border-radius: 10px; padding: 16px; margin: 16px 0; overflow-x: auto; }
.diagram svg { display: block; margin: 0 auto; }
.diagram .caption { text-align: center; color: var(--ks-text-soft); font-size: 0.82rem; margin-top: 8px; }
 
.badge-live { background: var(--ks-red); color: white; font-size: 0.7rem; padding: 2px 8px; border-radius: 999px; font-weight: 700; white-space: nowrap; display: inline-block; }
 
footer {
  color: var(--ks-text-soft); font-size: 0.8rem;
  padding: 30px 40px 60px; border-top: 1px solid var(--ks-grey-line); margin-top: 30px;
}
 
#design-note h1, #design-note h2 { border: none; padding: 0; }
#design-note h2 { font-size: 1.15rem; margin-top: 26px; border-bottom: 1px solid var(--ks-grey-line); padding-bottom: 6px; }
#design-note .table-wrap { overflow-x: auto; margin: 10px 0; }
#design-note table { border-collapse: collapse; font-size: 0.88rem; margin: 0; }
#design-note table th, #design-note table td { border: 1px solid var(--ks-grey-line); padding: 6px 10px; }
#design-note table th { background: var(--ks-navy); color: white; }
#design-note ul, #design-note ol { padding-left: 22px; }
 
@media (max-width: 860px) {
  nav#sidenav { display: none; }
  main { padding: 24px; }
}
</style>
</head>
<body>
 
<div id="topbar">
  <div class="brand"><span class="k">&#9632;</span> GPU Job Broker</div>
  <div class="tag">Guide &mdash; start here</div>
  <div class="spacer"></div>
  <a class="pill" href="/docs">Swagger / API reference</a>
  <a class="pill" href="/redoc">ReDoc</a>
</div>
 
<div id="layout">
  <nav id="sidenav">
    <div class="group-label">Start here</div>
    <a href="#welcome">Welcome</a>
    <a href="#setup">Prerequisites &amp; setup</a>
    <a href="#rules">Rules we follow</a>
    <a href="#architecture">Architecture</a>
    <div class="group-label">The story</div>
    <a href="#job-lifecycle">1. Job lifecycle</a>
    <a href="#node-scheduling">2. Nodes &amp; scheduling</a>
    <a href="#fairness">3. Fairness policy</a>
    <a href="#billing">4. Billing &amp; cost</a>
    <a href="#retrieval">5. Retrieval / AI</a>
    <a href="#restart">6. Restart safety</a>
    <div class="group-label">Reference</div>
    <a href="#api-reference">API quick reference</a>
    <a href="#design-note">Full design rationale</a>
  </nav>
 
  <main>
 
    <section id="welcome" class="doc-section">
      <h2>Welcome</h2>
      <p class="lede">
        This is a shared GPU job broker for a small AI research org: teams submit jobs, the
        broker places them on simulated cloud nodes, controls cost (idle reaping, interruptible
        nodes), survives restarts, and reports spend per team.
      </p>
      <p>
        Everything below is a <strong>story</strong> &mdash; each section builds on the one
        before it, the same way you'd actually use the service. Every <span class="badge-live">TRY IT</span>
        box makes a real HTTP call to <em>this running server</em> and shows you the real
        response, right here, no terminal required.
      </p>
      <div class="card rule">
        <strong>One clock, nothing happens on its own.</strong> This service runs on a
        <em>simulated</em> clock &mdash; nothing sleeps, nothing ticks in the background. Time only
        moves forward when you call <code>POST /simulator/advance</code>. That single idea
        explains almost everything you'll see below: a node "takes 5 minutes to boot" only in
        simulated time, so a whole week of activity can play out in a couple of real seconds.
      </div>
    </section>
 
    <section id="setup" class="doc-section">
      <h2>Prerequisites &amp; setup</h2>
      <p class="lede">
        You're reading this because a server is already running somewhere &mdash; here's exactly
        how to get to this same point yourself (or restart it), and how to run the test suite.
        Full detail in <code>README.md</code> at the repo root.
      </p>
      <div class="card rule">
        <strong>Prerequisites:</strong> Python 3.10+ and
        <a href="https://docs.astral.sh/uv/">uv</a>. Nothing else &mdash; no database server, no
        external services, no API keys, no model downloads. The database is a single SQLite
        file at <code>data/broker.db</code>, created automatically on first run.
      </div>
      <h3>Clone, install, run</h3>
      <pre><code>git clone &lt;this repo&gt;
cd gpu-job-broker
uv sync                              # installs everything into .venv, including dev/test deps
uv run uvicorn app.main:app --reload # starts the API at http://127.0.0.1:8000</code></pre>
      <p>Then open <code>http://127.0.0.1:8000/</code> &mdash; it redirects right back here.</p>
      <h3>Run the test suite</h3>
      <pre><code>uv run pytest -v</code></pre>
      <p>36 tests across 7 files. Run just one file with e.g.
      <code>uv run pytest tests/test_billing.py -v</code>.</p>
      <h3>Run the trace replay (policy comparison)</h3>
      <pre><code>uv run python replay.py data/01a_trace.csv</code></pre>
      <p>See <a href="#fairness">Fairness policy</a> below for what it prints and why.</p>
    </section>
 
    <section id="rules" class="doc-section">
      <h2>Rules we follow</h2>
      <p class="lede">
        These values are <strong>not</strong> typed into this page by hand &mdash; they're pulled
        live from <code>app/config.py</code> via <code>GET /guide/config</code>, the moment you
        load this page. If a tunable changes, this table changes with it automatically.
      </p>
      <div id="rules-tables">Loading live config&hellip;</div>
    </section>
 
    <section id="architecture" class="doc-section">
      <h2>Architecture at a glance</h2>
      <p>One process, one SQLite file, no background workers, no network calls other than the
      HTTP request you just made.</p>
      <div class="diagram">
        <svg viewBox="0 0 860 240" width="100%" height="240" xmlns="http://www.w3.org/2000/svg">
          <defs>
            <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M0,0 L10,5 L0,10 z" fill="#171B26"/>
            </marker>
          </defs>
          <g font-family="sans-serif" font-size="13">
            <rect x="20" y="90" width="120" height="56" rx="8" fill="#FFF1E6" stroke="#FF6B00" stroke-width="2"/>
            <text x="80" y="113" text-anchor="middle" font-weight="700">You</text>
            <text x="80" y="131" text-anchor="middle" font-size="11" fill="#6B6F7B">curl / Swagger / this page</text>
 
            <line x1="140" y1="118" x2="205" y2="118" stroke="#171B26" stroke-width="2" marker-end="url(#arrow)"/>
 
            <rect x="210" y="70" width="150" height="96" rx="8" fill="#FFFFFF" stroke="#E3E5EA" stroke-width="2"/>
            <text x="285" y="95" text-anchor="middle" font-weight="700">FastAPI routers</text>
            <text x="285" y="113" text-anchor="middle" font-size="11" fill="#6B6F7B">jobs / nodes / costs /</text>
            <text x="285" y="128" text-anchor="middle" font-size="11" fill="#6B6F7B">simulator / retrieval</text>
            <text x="285" y="150" text-anchor="middle" font-size="11" fill="#6B6F7B">(app/api/*.py)</text>
 
            <line x1="360" y1="118" x2="425" y2="118" stroke="#171B26" stroke-width="2" marker-end="url(#arrow)"/>
 
            <rect x="430" y="60" width="150" height="116" rx="8" fill="#171B26" stroke="#171B26" stroke-width="2"/>
            <text x="505" y="90" text-anchor="middle" font-weight="700" fill="white">Broker</text>
            <text x="505" y="108" text-anchor="middle" font-size="11" fill="#B9BECB">orchestrator</text>
            <text x="505" y="124" text-anchor="middle" font-size="11" fill="#B9BECB">(app/broker.py)</text>
            <text x="505" y="145" text-anchor="middle" font-size="11" fill="#FF6B00">driven by the</text>
            <text x="505" y="160" text-anchor="middle" font-size="11" fill="#FF6B00">Simulated Clock</text>
 
            <line x1="580" y1="90" x2="640" y2="55" stroke="#171B26" stroke-width="2" marker-end="url(#arrow)"/>
            <line x1="580" y1="118" x2="640" y2="118" stroke="#171B26" stroke-width="2" marker-end="url(#arrow)"/>
            <line x1="580" y1="146" x2="640" y2="181" stroke="#171B26" stroke-width="2" marker-end="url(#arrow)"/>
 
            <rect x="645" y="20" width="130" height="50" rx="8" fill="#FFFFFF" stroke="#E3E5EA" stroke-width="2"/>
            <text x="710" y="50" text-anchor="middle" font-size="12">Scheduler</text>
            <rect x="645" y="93" width="130" height="50" rx="8" fill="#FFFFFF" stroke="#E3E5EA" stroke-width="2"/>
            <text x="710" y="123" text-anchor="middle" font-size="12">Fake Provider</text>
            <rect x="645" y="166" width="130" height="50" rx="8" fill="#FFFFFF" stroke="#E3E5EA" stroke-width="2"/>
            <text x="710" y="196" text-anchor="middle" font-size="12">Billing</text>
 
            <line x1="505" y1="176" x2="505" y2="210" stroke="#171B26" stroke-width="2" marker-end="url(#arrow)"/>
            <rect x="420" y="210" width="170" height="26" rx="6" fill="#FFF1E6" stroke="#FF6B00" stroke-width="1.5"/>
            <text x="505" y="227" text-anchor="middle" font-size="12">SQLite (data/broker.db)</text>
          </g>
        </svg>
        <div class="caption">Every request is synchronous, in-process, and hits the same SQLite file &mdash; no queues, no background threads.</div>
      </div>
    </section>
 
    <section id="job-lifecycle" class="doc-section">
      <h2><span class="step-badge">STEP 1</span>Job lifecycle</h2>
      <p>A job moves through a small set of states. The only ways it changes state are: you
      advancing the clock, or you explicitly cancelling/completing it.</p>
      <div class="diagram">
        <svg viewBox="0 0 760 220" width="100%" height="220" xmlns="http://www.w3.org/2000/svg">
          <defs>
            <marker id="arrow2" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M0,0 L10,5 L0,10 z" fill="#171B26"/>
            </marker>
          </defs>
          <g font-family="sans-serif" font-size="13">
            <rect x="20" y="85" width="120" height="50" rx="25" fill="#FFF1E6" stroke="#FF6B00" stroke-width="2"/>
            <text x="80" y="115" text-anchor="middle" font-weight="700">queued</text>
 
            <line x1="140" y1="110" x2="265" y2="110" stroke="#171B26" stroke-width="2" marker-end="url(#arrow2)"/>
            <text x="200" y="100" text-anchor="middle" font-size="11" fill="#6B6F7B">node ready</text>
 
            <rect x="270" y="85" width="120" height="50" rx="25" fill="#171B26" stroke="#171B26" stroke-width="2"/>
            <text x="330" y="115" text-anchor="middle" font-weight="700" fill="white">running</text>
 
            <path d="M330,85 C 300,20 160,20 100,82" fill="none" stroke="#C4291C" stroke-width="2" marker-end="url(#arrow2)"/>
            <text x="210" y="35" text-anchor="middle" font-size="11" fill="#C4291C">interruptible reclaim (requeued)</text>
 
            <line x1="390" y1="100" x2="520" y2="55" stroke="#1F8A4C" stroke-width="2" marker-end="url(#arrow2)"/>
            <rect x="525" y="30" width="130" height="46" rx="23" fill="#1F8A4C"/>
            <text x="590" y="58" text-anchor="middle" fill="white" font-weight="700">succeeded</text>
 
            <line x1="390" y1="118" x2="520" y2="118" stroke="#C4291C" stroke-width="2" marker-end="url(#arrow2)"/>
            <rect x="525" y="93" width="130" height="46" rx="23" fill="#C4291C"/>
            <text x="590" y="121" text-anchor="middle" fill="white" font-weight="700">failed</text>
 
            <line x1="390" y1="135" x2="520" y2="178" stroke="#6B6F7B" stroke-width="2" marker-end="url(#arrow2)"/>
            <line x1="80" y1="135" x2="80" y2="178" stroke="#6B6F7B" stroke-width="2"/>
            <line x1="80" y1="178" x2="520" y2="178" stroke="#6B6F7B" stroke-width="2" marker-end="url(#arrow2)"/>
            <rect x="525" y="155" width="130" height="46" rx="23" fill="#6B6F7B"/>
            <text x="590" y="183" text-anchor="middle" fill="white" font-weight="700">cancelled</text>
            <text x="300" y="200" text-anchor="middle" font-size="11" fill="#6B6F7B">POST /jobs/{id}/cancel &mdash; from queued or running</text>
          </g>
        </svg>
      </div>
 
      <div class="tryit">
        <h4><span class="verb">POST</span> /jobs &mdash; submit a job</h4>
        <p class="desc">Starts out <code>queued</code>. It won't actually run until you move the clock forward below.</p>
        <div class="row">
          <label>team <input id="j-team" value="vision"></label>
          <label>gpus
            <select id="j-gpus"><option>1</option><option>2</option><option>4</option><option>8</option></select>
          </label>
          <label>estimated_minutes <input id="j-min" type="number" value="20"></label>
          <label>priority
            <select id="j-prio"><option value="normal" selected>normal</option><option value="low">low</option><option value="high">high</option></select>
          </label>
          <button onclick="submitJob()">Run</button>
        </div>
        <div id="out-submit" class="out"></div>
      </div>
 
      <div class="tryit">
        <h4><span class="verb">POST</span> /simulator/advance &mdash; move simulated time forward</h4>
        <p class="desc">This is how anything happens: nodes boot, jobs start, jobs finish, idle nodes get reaped. Try 10, then check the job below, then try a bigger jump.</p>
        <div class="row">
          <label>minutes <input id="adv-min" type="number" value="10"></label>
          <button onclick="advanceClock()">Run</button>
        </div>
        <div id="out-advance" class="out"></div>
      </div>
 
      <div class="tryit">
        <h4><span class="verb">GET</span> /jobs/{id} &mdash; check a job's state &amp; history</h4>
        <p class="desc">Auto-filled with the job you just submitted &mdash; edit it to check any other job id.</p>
        <div class="row">
          <label>job id <input id="j-id-check" class="job-id-field" placeholder="job-xxxx"></label>
          <button onclick="checkJob()">Run</button>
        </div>
        <div id="out-check" class="out"></div>
      </div>
 
      <div class="tryit">
        <h4><span class="verb">POST</span> /jobs/{id}/cancel or /complete</h4>
        <p class="desc">Cancel a queued/running job, or force a running job to end right now with a chosen outcome.</p>
        <div class="row">
          <label>job id <input id="j-id-act" class="job-id-field" placeholder="job-xxxx"></label>
          <button onclick="cancelJob()">Cancel</button>
          <label>outcome
            <select id="j-outcome"><option value="succeeded">succeeded</option><option value="failed">failed</option></select>
          </label>
          <button onclick="completeJob()">Complete</button>
        </div>
        <div id="out-act" class="out"></div>
      </div>
    </section>
 
    <section id="node-scheduling" class="doc-section">
      <h2><span class="step-badge">STEP 2</span>Nodes &amp; scheduling</h2>
      <p>A job doesn't get its own node unless it has to. The scheduler prefers sharing a node
      with free GPUs over launching a new one, and never launches past the node cap.</p>
 
      <div class="diagram">
        <svg viewBox="0 0 760 170" width="100%" height="170" xmlns="http://www.w3.org/2000/svg">
          <defs>
            <marker id="arrow3" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
              <path d="M0,0 L10,5 L0,10 z" fill="#171B26"/>
            </marker>
          </defs>
          <g font-family="sans-serif" font-size="13">
            <rect x="20" y="60" width="120" height="50" rx="8" fill="#FFF1E6" stroke="#FF6B00" stroke-width="2"/>
            <text x="80" y="90" text-anchor="middle" font-weight="700">booting</text>
            <line x1="140" y1="85" x2="260" y2="85" stroke="#171B26" stroke-width="2" marker-end="url(#arrow3)"/>
            <text x="200" y="75" text-anchor="middle" font-size="11" fill="#6B6F7B">4&ndash;8 min</text>
            <rect x="265" y="60" width="120" height="50" rx="8" fill="#1F8A4C"/>
            <text x="325" y="90" text-anchor="middle" fill="white" font-weight="700">ready</text>
 
            <line x1="80" y1="60" x2="80" y2="25" stroke="#C4291C" stroke-width="2"/>
            <line x1="80" y1="25" x2="200" y2="25" stroke="#C4291C" stroke-width="2" marker-end="url(#arrow3)"/>
            <rect x="205" y="2" width="120" height="46" rx="8" fill="#C4291C"/>
            <text x="265" y="30" text-anchor="middle" fill="white" font-weight="700">failed</text>
            <text x="150" y="16" text-anchor="middle" font-size="11" fill="#C4291C">~3% never-ready (15 min)</text>
 
            <line x1="385" y1="78" x2="500" y2="45" stroke="#6B6F7B" stroke-width="2" marker-end="url(#arrow3)"/>
            <rect x="505" y="20" width="150" height="46" rx="8" fill="#6B6F7B"/>
            <text x="580" y="48" text-anchor="middle" fill="white" font-weight="700">reclaiming</text>
            <text x="500" y="90" text-anchor="middle" font-size="11" fill="#6B6F7B">interruptible only, ~5% of node-hrs</text>
 
            <line x1="385" y1="95" x2="500" y2="128" stroke="#171B26" stroke-width="2" marker-end="url(#arrow3)"/>
            <line x1="580" y1="66" x2="580" y2="110" stroke="#171B26" stroke-width="2" marker-end="url(#arrow3)"/>
            <rect x="505" y="110" width="150" height="46" rx="8" fill="#171B26"/>
            <text x="580" y="138" text-anchor="middle" fill="white" font-weight="700">terminated</text>
            <text x="500" y="166" text-anchor="middle" font-size="11" fill="#6B6F7B">idle &gt; idle_reaper_minutes, or reclaimed</text>
          </g>
        </svg>
        <div class="caption">Node lifecycle. Billing never stops accruing until <code>terminated</code>/<code>failed</code> &mdash; see Billing below.</div>
      </div>
 
      <div class="tryit">
        <h4><span class="verb">GET</span> /nodes &mdash; list every node the broker has launched</h4>
        <div class="row"><button onclick="listNodes()">Run</button></div>
        <div id="out-nodes" class="out"></div>
      </div>
      <div class="tryit">
        <h4><span class="verb">GET</span> /jobs &mdash; list the current queue, in scheduling order</h4>
        <p class="desc">Priority + aging + (under Balanced) a per-team fairness tiebreaker, applied live.</p>
        <div class="row"><button onclick="listQueue()">Run</button></div>
        <div id="out-queue" class="out"></div>
      </div>
    </section>
 
    <section id="fairness" class="doc-section">
      <h2><span class="step-badge">STEP 3</span>Fairness policy</h2>
      <p>Two concrete scheduling policies are implemented and compared against the real
      494-row trace in <code>replay.py</code>:</p>
      <ul>
        <li><strong>Balanced</strong> (the default): priority + aging (a job's effective priority
        rises the longer it waits) + a per-team fairness tiebreaker, shares existing nodes, uses
        interruptible nodes for everything except <code>high</code> priority.</li>
        <li><strong>Latency&ndash;first</strong>: plain priority + FIFO, always launches a dedicated
        on-demand node immediately, never shares, never uses interruptible nodes.</li>
      </ul>
      <div class="card">
        Run it yourself from a terminal: <code>uv run python replay.py data/01a_trace.csv</code>
        &mdash; it prints the full comparison (total cost, platform overhead, and p50/p90/p99
        wait time per team) for both policies. The scheduling policy is a server-wide config
        value (<code>BROKER_SCHEDULING_POLICY</code>), not a per-request parameter, so there's no
        "try it" button here &mdash; see <a href="#design-note">the full design rationale</a> for
        the actual numbers and why Balanced is the recommended default.
      </div>
    </section>
 
    <section id="billing" class="doc-section">
      <h2><span class="step-badge">STEP 4</span>Billing &amp; cost</h2>
      <p>Billing starts the instant a launch is accepted (not when the node becomes ready), is
      per-second with a 10-minute floor per node, and every billed second is attributed to
      exactly one bucket: a team's <code>running_cost</code>, a team's <code>idle_cost</code>
      (the node is up but empty, billed to whoever ran there most recently), or
      <code>platform_overhead</code> (nobody's fault &mdash; e.g. a node that never became ready).</p>
      <div class="tryit">
        <h4><span class="verb">GET</span> /costs &mdash; GPU-hours and $ cost, per team</h4>
        <div class="row"><button onclick="getCosts()">Run</button></div>
        <div id="out-costs" class="out"></div>
      </div>
    </section>
 
    <section id="retrieval" class="doc-section">
      <h2><span class="step-badge">STEP 5</span>Retrieval / AI track</h2>
      <p>Paste a failed job's raw log, or just ask in plain English &mdash; both find the same
      cluster of past failures. Runs fully offline (TF-IDF + synonyms + latent-semantic hybrid
      over <code>data/01b_failure_logs.jsonl</code>), no model downloads, no network calls.</p>
      <div class="tryit">
        <h4><span class="verb">POST</span> /retrieval/search</h4>
        <div class="row">
          <label style="flex:1">text
            <textarea id="r-text">my job keeps running out of GPU RAM</textarea>
          </label>
          <label>top_k <input id="r-topk" type="number" value="3" style="width:60px"></label>
          <button onclick="retrievalSearch()">Run</button>
        </div>
        <div id="out-retrieval" class="out"></div>
      </div>
    </section>
 
    <section id="restart" class="doc-section">
      <h2><span class="step-badge">STEP 6</span>Restart safety</h2>
      <p>Everything &mdash; every job, node, usage segment, and the simulated clock's current
      time &mdash; is a row in the single SQLite file at <code>data/broker.db</code>. There's no
      in-memory state anywhere that matters. To see this for yourself (can't be done from a
      browser button, since it requires actually restarting the process):</p>
      <ol>
        <li>Submit a job and note its id and the current <code>/costs</code> output.</li>
        <li>Stop the server (Ctrl+C) and start it again with the exact same command.</li>
        <li><code>GET /jobs/{id}</code> and <code>GET /costs</code> return the exact same
        results &mdash; and the simulated clock resumes from where it left off, not from zero.</li>
      </ol>
    </section>
 
    <section id="api-reference" class="doc-section">
      <h2>API quick reference</h2>
      <p>Full interactive docs, with every field's description and example, live at
      <a href="/docs">/docs</a> (Swagger) and <a href="/redoc">/redoc</a>. This table is just the map:</p>
      <table class="rules">
        <tr><th>Endpoint</th><th>What it does</th></tr>
        <tr><td><code>POST /jobs</code></td><td>Submit a job (idempotent via optional <code>client_key</code>)</td></tr>
        <tr><td><code>GET /jobs</code></td><td>List the current queue, in scheduling order</td></tr>
        <tr><td><code>GET /jobs/{id}</code></td><td>Job state + full event history</td></tr>
        <tr><td><code>POST /jobs/{id}/cancel</code></td><td>Cancel a queued or running job</td></tr>
        <tr><td><code>POST /jobs/{id}/complete</code></td><td>Manually end a running job (succeeded/failed)</td></tr>
        <tr><td><code>GET /nodes</code></td><td>Every node ever launched and its current state</td></tr>
        <tr><td><code>POST /simulator/advance</code></td><td>Move simulated time forward by N minutes</td></tr>
        <tr><td><code>GET /costs</code></td><td>GPU-hours and $ cost per team, with idle/overhead broken out</td></tr>
        <tr><td><code>POST /retrieval/search</code></td><td>Find similar past job failures from free text</td></tr>
      </table>
    </section>
 
    <section id="design-note" class="doc-section">
      <h2>Full design rationale</h2>
      <p class="lede">
        Rendered live, below, from the real <code>docs/DESIGN.md</code> in this repository (via
        <code>GET /guide/design-note</code>) &mdash; this is not a summary or a copy, so it can
        never go out of sync with the actual written document.
      </p>
      <div id="design-note-body">Loading&hellip;</div>
    </section>
 
  </main>
</div>
 
<footer>
  GPU Job Broker &mdash; this page is served by the running app itself (no build step, no CDN
  assets, works fully offline).
</footer>
 
<script>
const BASE = '';
 
function setOut(id, text, ok) {
  const el = document.getElementById(id);
  el.textContent = text;
  el.className = 'out show ' + (ok === undefined ? '' : (ok ? 'ok' : 'err'));
}
 
async function callApi(outId, method, path, body) {
  setOut(outId, 'Running ' + method + ' ' + path + ' ...');
  try {
    const opts = { method };
    if (body !== undefined) {
      opts.headers = { 'Content-Type': 'application/json' };
      opts.body = JSON.stringify(body);
    }
    const res = await fetch(BASE + path, opts);
    let data;
    try { data = await res.json(); } catch (e) { data = { detail: await res.text() }; }
    setOut(outId, JSON.stringify(data, null, 2), res.ok);
    return { ok: res.ok, data };
  } catch (e) {
    setOut(outId, 'Network error: ' + e, false);
    return { ok: false, data: null };
  }
}
 
function fillJobIdFields(id) {
  document.querySelectorAll('.job-id-field').forEach(function (el) { el.value = id; });
}
 
async function submitJob() {
  const body = {
    team: document.getElementById('j-team').value || 'vision',
    gpus: parseInt(document.getElementById('j-gpus').value, 10),
    estimated_minutes: parseFloat(document.getElementById('j-min').value || '20'),
    priority: document.getElementById('j-prio').value,
  };
  const { ok, data } = await callApi('out-submit', 'POST', '/jobs', body);
  if (ok && data && data.id) fillJobIdFields(data.id);
}
 
async function advanceClock() {
  const minutes = parseFloat(document.getElementById('adv-min').value || '10');
  await callApi('out-advance', 'POST', '/simulator/advance', { minutes });
}
 
async function checkJob() {
  const id = document.getElementById('j-id-check').value.trim();
  if (!id) { setOut('out-check', 'Submit a job above first, or type an id.', false); return; }
  await callApi('out-check', 'GET', '/jobs/' + encodeURIComponent(id));
}
 
async function cancelJob() {
  const id = document.getElementById('j-id-act').value.trim();
  if (!id) { setOut('out-act', 'Enter a job id first.', false); return; }
  await callApi('out-act', 'POST', '/jobs/' + encodeURIComponent(id) + '/cancel');
}
 
async function completeJob() {
  const id = document.getElementById('j-id-act').value.trim();
  if (!id) { setOut('out-act', 'Enter a job id first.', false); return; }
  const outcome = document.getElementById('j-outcome').value;
  await callApi('out-act', 'POST', '/jobs/' + encodeURIComponent(id) + '/complete', { outcome });
}
 
async function listNodes() { await callApi('out-nodes', 'GET', '/nodes'); }
async function listQueue() { await callApi('out-queue', 'GET', '/jobs'); }
async function getCosts() { await callApi('out-costs', 'GET', '/costs'); }
 
async function retrievalSearch() {
  const text = document.getElementById('r-text').value;
  const top_k = parseInt(document.getElementById('r-topk').value || '3', 10);
  await callApi('out-retrieval', 'POST', '/retrieval/search', { text, top_k });
}
 
/* ---- Live "Rules we follow" tables, from /guide/config ---- */
function esc(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;'); }
 
function renderRules(cfg) {
  let html = '';
  html += '<h3>Node catalog</h3><table class="rules"><tr><th>Type</th><th>GPUs</th><th>On-demand $/hr</th><th>Interruptible $/hr</th></tr>';
  for (const [type, spec] of Object.entries(cfg.node_catalog)) {
    html += '<tr><td><code>' + esc(type) + '</code></td><td>' + spec.gpus + '</td><td>$' + spec.on_demand_price_per_hour.toFixed(2) + '</td><td>$' + spec.interruptible_price_per_hour.toFixed(2) + '</td></tr>';
  }
  html += '</table>';
 
  html += '<h3>Allowed job parameters</h3><table class="rules">';
  html += '<tr><th>Field</th><th>Allowed values</th><th>Meaning</th></tr>';
  html += '<tr><td><code>gpus</code></td><td>' + cfg.allowed_gpus.join(', ') + '</td><td>Fresh launches use the cheapest node type that fits (1&rarr;gpu-s, 2 or 4&rarr;gpu-m, 8&rarr;gpu-l); can also share any existing node with enough free GPUs</td></tr>';
  html += '<tr><td><code>priority</code></td><td>' + cfg.allowed_priorities.join(', ') + '</td><td><code>high</code> jumps the queue and always gets a reliable (non-interruptible) node</td></tr>';
  html += '<tr><td><code>job state</code></td><td>' + cfg.job_states.join(', ') + '</td><td>Read-only, reported back by the API</td></tr>';
  html += '<tr><td><code>node state</code></td><td>' + cfg.node_states.join(', ') + '</td><td>Read-only, reported back by the API</td></tr>';
  html += '</table>';
 
  html += '<h3>Timers &amp; limits</h3><table class="rules">';
  html += '<tr><th>Rule</th><th>Current value</th></tr>';
  html += '<tr><td>Minimum billed time per node</td><td>' + (cfg.minimum_billed_seconds / 60) + ' minutes</td></tr>';
  html += '<tr><td>Node boot time</td><td>' + cfg.node_ready_minutes.min + '&ndash;' + cfg.node_ready_minutes.max + ' minutes</td></tr>';
  html += '<tr><td>Chance a launch never becomes ready</td><td>' + (cfg.never_ready_probability * 100).toFixed(0) + '% (reported failed after ' + cfg.never_ready_report_after_minutes + ' min)</td></tr>';
  html += '<tr><td>Interruptible reclaim rate</td><td>~' + (cfg.interruptible_reclaim.target_fraction_of_node_hours * 100).toFixed(0) + '% of node-hours, ' + cfg.interruptible_reclaim.notice_minutes + ' min notice</td></tr>';
  html += '<tr><td>Idle reaper grace period</td><td>' + cfg.idle_reaper_minutes + ' minutes</td></tr>';
  html += '<tr><td>Max nodes alive at once</td><td>' + cfg.max_nodes + '</td></tr>';
  html += '<tr><td>Active scheduling policy</td><td><code>' + esc(cfg.scheduling_policy) + '</code> (available: ' + cfg.scheduling_policies_available.join(', ') + ')</td></tr>';
  html += '<tr><td>Node sharing across teams</td><td>' + (cfg.isolate_teams_per_node ? 'Isolated (one team per node)' : 'Allowed (default)') + '</td></tr>';
  html += '</table>';
 
  document.getElementById('rules-tables').innerHTML = html;
}
 
fetch('/guide/config').then(function (r) { return r.json(); }).then(renderRules).catch(function (e) {
  document.getElementById('rules-tables').innerHTML = '<p style="color:#C4291C">Could not load live config: ' + e + '</p>';
});
 
/* ---- Minimal markdown -> HTML renderer for the design note ---- */
function renderMarkdown(src) {
  const lines = src.replace(/\r\n/g, '\n').split('\n');
  let html = '';
  let i = 0;
  let inList = null; // 'ul' | 'ol'
  function closeList() { if (inList) { html += '</' + inList + '>'; inList = null; } }
  function inline(s) {
    s = esc(s);
    s = s.replace(/`([^`]+)`/g, '<code>$1</code>');
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2">$1</a>');
    return s;
  }
  while (i < lines.length) {
    const line = lines[i];
 
    if (/^\s*$/.test(line)) { closeList(); i++; continue; }
 
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h) {
      closeList();
      const level = h[1].length;
      html += '<h' + level + '>' + inline(h[2]) + '</h' + level + '>';
      i++; continue;
    }
 
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
      closeList();
      const header = line.split('|').slice(1, -1).map(function (c) { return c.trim(); });
      html += '<div class="table-wrap"><table><tr>' + header.map(function (c) { return '<th>' + inline(c) + '</th>'; }).join('') + '</tr>';
      i += 2;
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
        const cells = lines[i].split('|').slice(1, -1).map(function (c) { return c.trim(); });
        html += '<tr>' + cells.map(function (c) { return '<td>' + inline(c) + '</td>'; }).join('') + '</tr>';
        i++;
      }
      html += '</table></div>';
      continue;
    }
 
    const ol = line.match(/^\s*\d+\.\s+(.*)$/);
    if (ol) {
      if (inList !== 'ol') { closeList(); html += '<ol>'; inList = 'ol'; }
      html += '<li>' + inline(ol[1]) + '</li>';
      i++; continue;
    }
 
    const ul = line.match(/^\s*[-*]\s+(.*)$/);
    if (ul) {
      if (inList !== 'ul') { closeList(); html += '<ul>'; inList = 'ul'; }
      html += '<li>' + inline(ul[1]) + '</li>';
      i++; continue;
    }
 
    if (/^-{3,}\s*$/.test(line)) { closeList(); html += '<hr>'; i++; continue; }
 
    closeList();
    html += '<p>' + inline(line) + '</p>';
    i++;
  }
  closeList();
  return html;
}
 
fetch('/guide/design-note').then(function (r) { return r.text(); }).then(function (text) {
  document.getElementById('design-note-body').innerHTML = renderMarkdown(text);
}).catch(function (e) {
  document.getElementById('design-note-body').innerHTML = '<p style="color:#C4291C">Could not load docs/DESIGN.md: ' + e + '</p>';
});
 
/* ---- Scroll-spy nav highlighting ---- */
(function () {
  const links = Array.prototype.slice.call(document.querySelectorAll('#sidenav a'));
  const sections = links.map(function (a) { return document.querySelector(a.getAttribute('href')); }).filter(Boolean);
  function onScroll() {
    let current = sections[0];
    for (const s of sections) { if (s.getBoundingClientRect().top - 90 <= 0) current = s; }
    links.forEach(function (a) {
      a.classList.toggle('active', current && a.getAttribute('href') === '#' + current.id);
    });
  }
  document.addEventListener('scroll', onScroll);
  onScroll();
})();
</script>
</body>
</html>
"""
