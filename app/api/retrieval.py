"""The Retrieval/AI track endpoint: paste a failed job's log, or just ask
a plain-English question, and get back the most similar past failures and
how they were fixed."""

from __future__ import annotations

from fastapi import APIRouter

from app.retrieval import get_default_index
from app.schemas import (
    RetrievalResultOut,
    RetrievalSearchRequest,
    RetrievalSearchResponse,
)

router = APIRouter(prefix="/retrieval", tags=["5. Retrieval / AI"])


@router.post(
    "/search",
    response_model=RetrievalSearchResponse,
    summary="Find similar past failures",
    description=(
        "Paste a failed job's raw log, or ask a free-text question in your own words (technical "
        "or everyday phrasing both work, e.g. 'ran out of memory' or 'OOM' find the same "
        "failures). Returns the most similar past failures from data/01b_failure_logs.jsonl, "
        "each with its recorded resolution (empty if none was ever recorded) and a similarity "
        "score. Runs entirely offline -- no external calls, no model downloads; see "
        "docs/DESIGN.md for how the ranking works and why."
    ),
)
def search(payload: RetrievalSearchRequest) -> RetrievalSearchResponse:
    index = get_default_index()
    results = index.rank(payload.text, top_k=payload.top_k)
    return RetrievalSearchResponse(
        results=[
            RetrievalResultOut(
                id=log.id, team=log.team, resolution=log.resolution, score=score
            )
            for log, score in results
        ]
    )
