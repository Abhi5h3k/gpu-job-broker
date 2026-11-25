"""All tunable settings for the broker, in one place.

Nothing in this file is a secret -- it is pure configuration (node prices,
timers, feature flags). Values can be overridden via environment variables
or a local ``.env`` file (see ``.env.example``); nothing here is required
to be set for the service to run with sensible defaults.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.models import NodeType  # the one canonical definition; see models.py


class NodeSpec(BaseModel):
    """Static facts about one node type: how many GPUs it has and what it
    costs per hour, on-demand."""

    gpus: int
    price_per_hour: float


# The provider's price sheet, taken directly from the exercise doc.
NODE_CATALOG: dict[NodeType, NodeSpec] = {
    NodeType.GPU_S: NodeSpec(gpus=1, price_per_hour=1.00),
    NodeType.GPU_M: NodeSpec(gpus=4, price_per_hour=4.50),
    NodeType.GPU_L: NodeSpec(gpus=8, price_per_hour=8.50),
}

# Interruptible nodes cost this fraction of the on-demand price (doc: "35%
# of the price").
INTERRUPTIBLE_PRICE_FACTOR = 0.35

# Per-node billing floor: even a node alive for 1 second is billed for this
# many seconds. Doc: "Billing is per second with a 10-minute minimum per
# node." Applies to every node lifecycle, successful or not -- see
# docs/DESIGN.md for the worked example of why this matters.
MINIMUM_BILLED_SECONDS = 10 * 60

# Node readiness: doc says "4 to 8 minutes to become ready after launch."
NODE_READY_MIN_MINUTES = 4.0
NODE_READY_MAX_MINUTES = 8.0

# Doc: "About 3% of launches never become ready (the provider reports
# failure after 15 minutes)."
NEVER_READY_PROBABILITY = 0.03
NEVER_READY_REPORT_AFTER_MINUTES = 15.0

# Doc: interruptible nodes "can be taken back with 2 minutes' notice, on
# average about 5% of node-hours." We model this as a constant per-minute
# hazard rate calibrated so the long-run fraction of node-minutes reclaimed
# converges to this target. See app/provider.py for the derivation.
INTERRUPTIBLE_RECLAIM_TARGET_FRACTION = 0.05
INTERRUPTIBLE_RECLAIM_NOTICE_MINUTES = 2.0

# Bursty CapacityUnavailable: while an "incident" is active, launches fail
# at high probability; otherwise failures are rare. These are intentionally
# simple, named constants (not a full Markov-chain library) -- see
# app/provider.py.
CAPACITY_INCIDENT_START_PROBABILITY_PER_MINUTE = 0.01
CAPACITY_INCIDENT_MIN_DURATION_MINUTES = 5.0
CAPACITY_INCIDENT_MAX_DURATION_MINUTES = 20.0
CAPACITY_FAILURE_PROBABILITY_DURING_INCIDENT = 0.85
CAPACITY_FAILURE_PROBABILITY_NORMAL = 0.01


class SchedulingPolicy(str, Enum):
    """The two scheduling policies compared in the trace replay
    (requirement 8). See docs/DESIGN.md for the full justification."""

    BALANCED = "balanced"
    LATENCY_FIRST = "latency_first"


class Settings(BaseSettings):
    """Runtime-tunable settings, loaded from environment variables prefixed
    with ``BROKER_`` (or a ``.env`` file). Every field has a safe default
    so the service runs out of the box with zero configuration."""

    model_config = SettingsConfigDict(
        env_prefix="BROKER_", env_file=".env", extra="ignore"
    )

    db_path: str = "data/broker.db"

    # Doc: "a node with no running jobs for more than N minutes
    # (configurable) is terminated."
    idle_reaper_minutes: float = 15.0

    # Doc: "Maximum 10 nodes alive at once."
    max_nodes: int = 10

    scheduling_policy: SchedulingPolicy = SchedulingPolicy.BALANCED

    # See docs/DESIGN.md for the Security-vs-utilization discussion this
    # flag resolves. Default False matches the literal requirement that a
    # node may run jobs from different teams if GPUs are free.
    isolate_teams_per_node: bool = False

    # Random seed for the fake provider's dice rolls (capacity incidents,
    # never-ready draws, reclaim hazard). Fixed by default so test runs and
    # demos are reproducible; override for genuine randomness.
    random_seed: int = 1337


settings = Settings()
