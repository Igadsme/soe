from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, HttpUrl


class SourceCreate(BaseModel):
    platform: str = Field(pattern="^(youtube|twitch)$")
    external_id: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=500)
    url: HttpUrl


class RightsCreate(BaseModel):
    authorization_type: str = Field(pattern="^(owned|licensed|explicit_creator_authorization)$")
    commercial_use: bool
    publishing_allowed: bool
    evidence: str = Field(min_length=5)
    expires_at: datetime | None = None


class Highlight(BaseModel):
    start_seconds: float = Field(ge=0)
    end_seconds: float
    description: str
    hook: str
    score: float = Field(ge=0, le=100)
    evidence: dict


class ClipRead(Highlight):
    id: int
    source_id: int
    status: str
    model_config = ConfigDict(from_attributes=True)


class SourceRead(SourceCreate):
    id: int
    status: str
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)
