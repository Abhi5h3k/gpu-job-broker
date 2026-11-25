"""Pydantic request/response models for the HTTP API.

These double as the source of the Swagger (``/docs``) examples and field
descriptions -- see each ``Field(..., description=...)`` below. Keeping
request/response shapes here, separate from the ORM models in models.py,
means a change to how we store something internally never accidentally
changes the public API contract, and vice versa.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from app.models import JobSource, JobState, NodeState, NodeType, Priority

GpuCount = Literal[1, 2, 4, 8]


class SubmitJobRequest(BaseModel):
    team: str = Field(
        ...,
        examples=["vision"],
        description="The team this job belongs to -- any string; used for cost attribution and the fairness tiebreaker. No registration needed.",
    )
    gpus: GpuCount = Field(
        ...,
        description=(
            "How many GPUs this job needs -- must be exactly 1, 2, 4, or 8 (no in-between "
            "sizes). May be placed on any existing node with enough free GPUs regardless of "
            "type; if a fresh node has to be launched, the cheapest type that fits is chosen: "
            "1 GPU -> gpu-s, 2 or 4 GPUs -> gpu-m, 8 GPUs -> gpu-l. See `/guide`'s "
            "'Rules we follow' section for live node prices."
        ),
    )
    estimated_minutes: float = Field(
        ...,
        gt=0,
        examples=[30],
        description="Your best guess at how long the job will run.",
    )
    priority: Priority = Field(
        default=Priority.NORMAL,
        description=(
            "low / normal / high. High priority jobs are ordered first and are placed on "
            "reliable (non-interruptible) nodes -- see docs/DESIGN.md for why."
        ),
    )
    client_key: str | None = Field(
        default=None,
        description=(
            "Optional idempotency key. Submitting the same client_key twice returns the "
            "original job instead of creating a duplicate."
        ),
    )


class CompleteJobRequest(BaseModel):
    outcome: Literal["succeeded", "failed"] = Field(
        ..., description="Manually end a running job early with this outcome."
    )


class JobEventOut(BaseModel):
    state: JobState
    at: datetime
    note: str | None = None

    model_config = {"from_attributes": True}


class JobOut(BaseModel):
    id: str
    team: str
    gpus_requested: int
    priority: Priority
    estimated_minutes: float
    state: JobState
    source: JobSource
    submitted_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    remaining_run_minutes: float
    total_run_minutes_accrued: float
    node_id: str | None
    requeue_count: int

    model_config = {"from_attributes": True}


class JobDetailOut(JobOut):
    events: list[JobEventOut]


class NodeOut(BaseModel):
    id: str
    node_type: NodeType
    is_interruptible: bool
    gpus_total: int
    gpus_free: int
    state: NodeState
    launched_at: datetime
    ready_at: datetime | None
    terminated_at: datetime | None
    owner_team: str | None

    model_config = {"from_attributes": True}


class AdvanceRequest(BaseModel):
    minutes: float = Field(
        ...,
        gt=0,
        examples=[10],
        description="How many simulated minutes to jump forward.",
    )


class AdvanceResponse(BaseModel):
    now: datetime = Field(..., description="The simulated clock's new current time.")
    nodes_ready: list[str] = Field(default_factory=list)
    nodes_failed_never_ready: list[str] = Field(default_factory=list)
    nodes_reclaimed: list[str] = Field(default_factory=list)
    nodes_reaped_idle: list[str] = Field(default_factory=list)
    jobs_completed: list[str] = Field(default_factory=list)
    jobs_requeued_after_reclaim: list[str] = Field(default_factory=list)
    capacity_unavailable_events: int = 0


class TeamCostOut(BaseModel):
    team: str
    gpu_hours: float
    running_cost: float
    idle_cost: float
    total_cost: float


class CostReportOut(BaseModel):
    window_start: datetime
    window_end: datetime
    teams: list[TeamCostOut]
    platform_overhead_cost: float = Field(
        ...,
        description="Cost of nodes that never ran a job for anyone (e.g. launches that never became ready).",
    )
    total_cost: float


class RetrievalSearchRequest(BaseModel):
    text: str = Field(
        ...,
        examples=["loss turned to NaN after a few epochs"],
        description="A failed job's log text, or a free-text question -- technical or everyday phrasing both work.",
    )
    top_k: int = Field(default=5, ge=1, le=20)


class RetrievalResultOut(BaseModel):
    id: str
    team: str
    resolution: str
    score: float


class RetrievalSearchResponse(BaseModel):
    results: list[RetrievalResultOut]
