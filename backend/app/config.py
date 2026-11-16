from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = Field(default="sqlite:///./soe.db", repr=False)
    env: str = "development"
    demo_mode: bool = True
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
    session_ttl_seconds: int = 28800
    cors_origins: str = "http://localhost:3000"
    rate_limit_per_minute: int = 120
    trusted_proxies: str = ""
    redis_url: str = Field(default="", repr=False)

    @field_validator("env")
    @classmethod
    def normalize_environment(cls, value: str) -> str:
        return value.strip().lower()

    def validate_production(self) -> None:
        """Reject unsafe production settings without including their values in errors."""
        if self.env.strip().lower() != "production":
            return
        errors = []
        if len(secret_value(self.session_secret).strip()) < 32:
            errors.append("SOE_SESSION_SECRET must contain at least 32 characters")
        if not (secret_value(self.admin_password).strip() or secret_value(self.admin_password_hash).strip()):
            errors.append("SOE_ADMIN_PASSWORD or SOE_ADMIN_PASSWORD_HASH is required")
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

    model_config = SettingsConfigDict(env_prefix="SOE_", env_file=".env", extra="ignore")


settings = Settings()


def secret_value(value: SecretStr | str) -> str:
    return value.get_secret_value() if isinstance(value, SecretStr) else value
