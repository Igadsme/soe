from datetime import datetime, timezone

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)


class SourceCreate(BaseModel):
    platform: str = Field(pattern="^(youtube|twitch|upload)$")
    external_id: str = Field(min_length=1, max_length=255)
    title: str = Field(min_length=1, max_length=500)
    url: HttpUrl
    media_path: str | None = None


    @field_validator("media_path")
    @classmethod
    def validate_media_path(cls, value):
        return safe_media_path(value) if value else value


def safe_media_path(value: str) -> str:
    from pathlib import Path

    from .config import settings
    resolved = Path(value).resolve()
    if not resolved.is_relative_to(Path(settings.media_root).resolve()):
        raise ValueError("media_path must be inside SOE_MEDIA_ROOT")
    return str(resolved)


class RightsCreate(BaseModel):
    authorization_type: str = Field(pattern="^(owned|licensed|explicit_creator_authorization)$")
    commercial_use: bool
    publishing_allowed: bool
    evidence: str = Field(min_length=20)
    expires_at: datetime | None = None
    allowed_platforms: list[str] = Field(min_length=1)

    @field_validator("evidence")
    @classmethod
    def evidence_content(cls, value):
        if len("".join(value.split())) < 20:
            raise ValueError("evidence requires 20 non-whitespace characters")
        return value

    @field_validator("allowed_platforms")
    @classmethod
    def platforms(cls, value):
        if not set(value) <= {"youtube", "twitch", "tiktok", "instagram", "upload"}:
            raise ValueError("unknown platform")
        return value

    @field_validator("expires_at")
    @classmethod
    def utc_expiry(cls, value):
        return value.replace(tzinfo=value.tzinfo or timezone.utc) if value else value


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
    created_at: datetime | None = None
    output_path: str | None = None
    subtitle_path: str | None = None
    model_config = ConfigDict(from_attributes=True)

    @model_validator(mode="after")
    def validate_interval(self) -> "Highlight":
        if self.end_seconds <= self.start_seconds:
            raise ValueError("end_seconds must be greater than start_seconds")
        return self


class SourceRead(BaseModel):
    platform: str
    external_id: str
    title: str
    url: str
    has_media: bool
    id: int
    status: str
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)
