"""Memory routes: read, search, list by layer."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from core.types.identifiers import AutomatonId, MemoryId
from core.types.automaton import MemoryLayer

router = APIRouter()


class MemoryWriteRequest(BaseModel):
    automaton_id: AutomatonId
    layer: MemoryLayer
    content: str = Field(min_length=1, max_length=1_000_000)
    importance: float = Field(ge=0.0, le=1.0, default=0.5)
    tags: list[str] = Field(default_factory=list)


class MemoryResponse(BaseModel):
    id: MemoryId
    layer: MemoryLayer
    content: str
    importance: float
    tags: list[str]
    created_at: str
    updated_at: str


def get_registry(request: Request):
    return request.app.state.registry


# In-memory store keyed by automaton. In production this is the memory service.
_MEM: dict[str, dict[str, MemoryResponse]] = {}


@router.post("", response_model=MemoryResponse, status_code=201)
def write(req: MemoryWriteRequest) -> MemoryResponse:
    from datetime import datetime, timezone
    import uuid

    mid = MemoryId(f"mem_{uuid.uuid4().hex}")
    now = datetime.now(tz=timezone.utc)
    resp = MemoryResponse(
        id=mid,
        layer=req.layer,
        content=req.content,
        importance=req.importance,
        tags=req.tags,
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
    )
    _MEM.setdefault(str(req.automaton_id), {})[str(mid)] = resp
    return resp


@router.get("/{aid}", response_model=list[MemoryResponse])
def list_memory(aid: str, layer: MemoryLayer | None = None) -> list[MemoryResponse]:
    items = _MEM.get(aid, {}).values()
    if layer is not None:
        items = [m for m in items if m.layer == layer]
    return list(items)


@router.get("/{aid}/search")
def search(aid: str, query: str = "", k: int = 5) -> list[MemoryResponse]:
    items = _MEM.get(aid, {}).values()
    if not query:
        return list(items)[:k]
    q = query.lower().split()
    return [m for m in items if any(tok in m.content.lower() for tok in q)][:k]
