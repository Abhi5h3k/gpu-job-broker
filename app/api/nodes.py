"""Step 2 of the story: see what nodes the broker has launched to serve
your jobs, and their current state (booting / ready / reclaiming /
terminated / failed)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app.broker import Broker
from app.deps import get_broker, get_db_session
from app.schemas import NodeOut

router = APIRouter(prefix="/nodes", tags=["2. Nodes"])


def _broker(request: Request, session: Session = Depends(get_db_session)) -> Broker:
    return get_broker(request, session)


@router.get(
    "",
    response_model=list[NodeOut],
    summary="List all nodes",
    description=(
        "Every node the broker has ever launched, oldest first. Watch `gpus_free` and `state` "
        "change as you call `POST /simulator/advance` -- a node goes `booting` -> `ready` -> "
        "(eventually) `terminated`, or `failed` if it's one of the ~3% that never comes up, or "
        "`reclaiming` if it's an interruptible node the provider is taking back."
    ),
)
def list_nodes(broker: Broker = Depends(_broker)) -> list[NodeOut]:
    return [NodeOut.model_validate(n) for n in broker.list_nodes()]
