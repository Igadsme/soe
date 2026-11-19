import os
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = Field(default="sqlite:///./soe.db", repr=False)
    env: str = "production"
    demo_mode: bool = False
    dev_auth_bypass: bool = False
    session_idle_seconds: int = 1800
    ffmpeg_timeout_seconds: float = 300
    processing_deadline_floor_seconds: float = Field(default=300, ge=1)
    processing_deadline_duration_multiplier: float = Field(default=5, ge=1)
    max_source_seconds: float = 3600
    whisper_cache_dir: str = "/app/.cache/whisper"
    media_root: str = "./media"
    max_upload_bytes: int = 2_000_000_000
    max_clip_seconds: int = 180
    youtube_api_key: SecretStr = SecretStr("")
    twitch_client_id: str = ""
    twitch_client_secret: SecretStr = SecretStr("")
    publishing_enabled: bool = False
    publish_youtube_enabled: bool = False
    publish_tiktok_enabled: bool = False
    publish_instagram_enabled: bool = False
    publishing_dry_run: bool = False
    token_encryption_keys: SecretStr = SecretStr("")
    google_client_id: SecretStr = SecretStr("")
    google_client_secret: SecretStr = SecretStr("")
    tiktok_client_key: SecretStr = SecretStr("")
    tiktok_client_secret: SecretStr = SecretStr("")
    meta_app_id: SecretStr = SecretStr("")
    meta_app_secret: SecretStr = SecretStr("")
    oauth_redirect_base: SecretStr = SecretStr("")
    meta_graph_version: str = Field(default="v26.0", pattern=r"^v[0-9]{2}\.0$")
    tiktok_app_audited: bool = False
    youtube_upload_chunk_bytes: int = Field(default=8 * 1024**2, ge=256 * 1024, le=64 * 1024**2, multiple_of=256 * 1024)
    publishing_max_attempts: int = Field(default=5, ge=1, le=10)
    publishing_deadline_seconds: int = Field(default=900, ge=1)
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

    def oauth_credentials(self, platform: str) -> tuple[str, str]:
        attributes = {
            "youtube": ("google_client_id", "google_client_secret"),
            "tiktok": ("tiktok_client_key", "tiktok_client_secret"),
            "instagram": ("meta_app_id", "meta_app_secret"),
        }
        identifier, secret = attributes[platform]
        return secret_value(getattr(self, identifier)).strip(), secret_value(getattr(self, secret)).strip()

    def oauth_origin(self) -> str:
        value = secret_value(self.oauth_redirect_base).strip().rstrip("/")
        try:
            parts = urlsplit(value)
            if parts.username is not None or not parts.hostname or parts.path or parts.query or parts.fragment:
                return ""
            if parts.scheme == "https" or (
                self.env in {"development", "test"} and parts.scheme == "http" and parts.hostname in {"localhost", "127.0.0.1"}
            ):
                return value
        except ValueError:
            return ""
        return ""

    def publishing_allowed(self, platform: str) -> bool:
        return self.publishing_enabled and getattr(self, f"publish_{platform}_enabled", False)

    def validate_production(self) -> None:
        """Reject unsafe production settings without including their values in errors."""
        if self.env not in {"production", "staging"}:
            return
        errors = []
        if secret_value(self.admin_password):
            errors.append("SOE_ADMIN_PASSWORD is forbidden; use SOE_ADMIN_PASSWORD_HASH")
        if self.metrics_enabled and not secret_value(self.metrics_token).strip():
            errors.append("SOE_METRICS_TOKEN is required when metrics are enabled")
        clients = [self.oauth_credentials(platform) for platform in ("youtube", "tiktok", "instagram")]
        if self.publishing_enabled and not (self.oauth_origin() and any(all(client) for client in clients)):
            errors.append("SOE_PUBLISHING_ENABLED requires a configured OAuth client and HTTPS SOE_OAUTH_REDIRECT_BASE")
        if self.publishing_enabled or any(any(client) for client in clients) or self.token_encryption_keys:
            from .token_vault import keyring
            try:
                keyring(secret_value(self.token_encryption_keys))
            except ValueError:
                errors.append("SOE_TOKEN_ENCRYPTION_KEYS must contain valid Fernet keys")
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

    model_config = SettingsConfigDict(env_prefix="SOE_", env_file=os.environ.get("SOE_ENV_FILE", ".env"), extra="ignore", env_ignore_empty=True)


settings = Settings()


def secret_value(value: SecretStr | str) -> str:
    return value.get_secret_value() if isinstance(value, SecretStr) else value
