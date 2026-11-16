from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "sqlite:///./soe.db"
    env: str = "development"
    demo_mode: bool = True
    media_root: str = "./media"
    max_upload_bytes: int = 2_000_000_000
    max_clip_seconds: int = 180
    youtube_api_key: str = ""
    twitch_client_id: str = ""
    twitch_client_secret: str = ""
    publishing_enabled: bool = False
    whisper_model_size: str = "tiny.en"
    admin_username: str = "operator"
    admin_password: str = ""
    admin_password_hash: str = ""
    session_secret: str = ""
    session_ttl_seconds: int = 28800
    cors_origins: str = "http://localhost:3000"
    rate_limit_per_minute: int = 120
    def validate_production(self) -> None:
        """Reject unsafe production settings without including their values in errors."""
        if self.env.strip().lower() != "production":
            return
        errors = []
        if len(self.session_secret.strip()) < 32:
            errors.append("SOE_SESSION_SECRET must contain at least 32 characters")
        if not (self.admin_password.strip() or self.admin_password_hash.strip()):
            errors.append("SOE_ADMIN_PASSWORD or SOE_ADMIN_PASSWORD_HASH is required")
        if self.demo_mode:
            errors.append("SOE_DEMO_MODE must be false")
        origins = [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]
        if not origins or any("*" in origin for origin in origins):
            errors.append("SOE_CORS_ORIGINS must contain explicit origins without wildcards")
        if self.database_url.strip().lower().startswith("sqlite"):
            errors.append("SOE_DATABASE_URL must use a production database, not SQLite")
        if errors:
            raise RuntimeError("Invalid production configuration: " + "; ".join(errors))

    model_config = SettingsConfigDict(env_prefix="SOE_", env_file=".env", extra="ignore")


settings = Settings()
