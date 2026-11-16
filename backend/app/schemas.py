from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class SourceCreate(BaseModel):
    platform: str = Field(pattern="^(youtube|twitch|upload)$")
    external_id: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=500)
    url: HttpUrl
    media_path: str | None = None


class RightsCreate(BaseModel):
    authorization_type: str = Field(pattern="^(owned|licensed|explicit_creator_authorization)$")
    commercial_use: bool
    publishing_allowed: bool
    evidence: str = Field(min_length=5)
    expires_at: datetime | None = None
    allowed_platforms: list[str] = []


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
    output_path: str | None = None
    subtitle_path: str | None = None
    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode="after")
    def validate_interval(self) -> "Highlight":
        if self.end_seconds <= self.start_seconds:
            raise ValueError("end_seconds must be greater than start_seconds")
        return self


class SourceRead(SourceCreate):
    id: int
    status: str
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)
