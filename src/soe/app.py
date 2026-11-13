from __future__ import annotations

from uuid import uuid4

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

app = FastAPI(title="SOE")

_contents: dict[str, str] = {}


class IngestRequest(BaseModel):
    content: str = Field(min_length=1)


class ClipRequest(BaseModel):
    content_id: str
    max_clips: int = Field(default=3, ge=1, le=10)


class PublishRequest(BaseModel):
    content_id: str
    destination: str = Field(min_length=1)


class MonetizeRequest(BaseModel):
    content_id: str
    cpm: float = Field(default=10.0, gt=0)
    impressions: int = Field(default=1000, ge=0)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/ingest")
def ingest(payload: IngestRequest) -> dict[str, str]:
    content_id = str(uuid4())
    _contents[content_id] = payload.content.strip()
    return {"content_id": content_id, "status": "ingested"}


@app.post("/clip")
def clip(payload: ClipRequest) -> dict[str, object]:
    content = _contents.get(payload.content_id)
    if content is None:
        raise HTTPException(status_code=404, detail="content_id not found")

    lines = [line.strip() for line in content.splitlines() if line.strip()]
    if not lines:
        lines = [content]

    clips = lines[: payload.max_clips]
    return {"content_id": payload.content_id, "clips": clips, "count": len(clips)}


@app.post("/publish")
def publish(payload: PublishRequest) -> dict[str, str]:
    if payload.content_id not in _contents:
        raise HTTPException(status_code=404, detail="content_id not found")
    return {
        "content_id": payload.content_id,
        "destination": payload.destination,
        "status": "published",
    }


@app.post("/monetize")
def monetize(payload: MonetizeRequest) -> dict[str, float | str]:
    if payload.content_id not in _contents:
        raise HTTPException(status_code=404, detail="content_id not found")
    revenue = (payload.impressions / 1000) * payload.cpm
    return {
        "content_id": payload.content_id,
        "estimated_revenue": round(revenue, 2),
        "currency": "USD",
    }
