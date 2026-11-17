import os

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = Field(default="sqlite:///./soe.db", repr=False)
    env: str = "production"
    demo_mode: bool = False
    dev_auth_bypass: bool = False
    session_idle_seconds: int = 1800
    ffmpeg_timeout_seconds: float = 300
    max_source_seconds: float = 3600
    whisper_cache_dir: str = "/app/.cache/whisper"
    media_root: str = "./media"
    max_upload_bytes: int = 2_000_000_000
    max_clip_seconds: int = 180
    youtube_api_key: SecretStr = SecretStr("")
    twitch_client_id: str = ""
    twitch_client_secret: SecretStr = SecretStr("")
    publishing_enabled: bool = False
    whisper_model_size: str = "tiny.en"
    admin_username: str = "operator"
    admin_password: SecretStr = SecretStr("")
    admin_password_hash: SecretStr = SecretStr("")
    session_secret: SecretStr = SecretStr("")
    session_retention_seconds: int = Field(default=604800, ge=0)
    session_ttl_seconds: int = 28800
    cors_origins: str = "http://localhost:3000"
    rate_limit_per_minute: int = 120
    trusted_proxies: str = ""
    metrics_enabled: bool = False
    metrics_token: SecretStr = SecretStr("")
    sentry_dsn: SecretStr = SecretStr("")
    sentry_traces_sample_rate: float = Field(default=0, ge=0, le=1)
    redis_url: str = Field(default="", repr=False)

    @field_validator("env")
    @classmethod
    def normalize_environment(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in {"development", "test", "staging", "production"}:
            raise ValueError("unknown SOE_ENV")
        return value

    def validate_production(self) -> None:
        """Reject unsafe production settings without including their values in errors."""
        if self.env not in {"production", "staging"}:
            return
        errors = []
        if secret_value(self.admin_password):
            errors.append("SOE_ADMIN_PASSWORD is forbidden; use SOE_ADMIN_PASSWORD_HASH")
        if self.metrics_enabled and not secret_value(self.metrics_token).strip():
            errors.append("SOE_METRICS_TOKEN is required when metrics are enabled")
        if self.publishing_enabled:
            errors.append("SOE_PUBLISHING_ENABLED must be false until media publishing is implemented")
        if len(secret_value(self.session_secret).strip()) < 32:
            errors.append("SOE_SESSION_SECRET must contain at least 32 characters")
        if not secret_value(self.admin_password_hash).strip():
            errors.append("SOE_ADMIN_PASSWORD_HASH is required")
        if self.demo_mode:
            errors.append("SOE_DEMO_MODE must be false")
        origins = [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]
        if not origins or any("*" in origin for origin in origins):
            errors.append("SOE_CORS_ORIGINS must contain explicit origins without wildcards")
        if self.database_url.strip().lower().startswith("sqlite"):
            errors.append("SOE_DATABASE_URL must use a production database, not SQLite")
        if not self.redis_url.strip():
            errors.append("SOE_REDIS_URL is required")
        if errors:
            raise RuntimeError("Invalid production configuration: " + "; ".join(errors))

    model_config = SettingsConfigDict(env_prefix="SOE_", env_file=os.environ.get("SOE_ENV_FILE", ".env"), extra="ignore")


settings = Settings()


def secret_value(value: SecretStr | str) -> str:
    return value.get_secret_value() if isinstance(value, SecretStr) else value
