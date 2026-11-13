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
    session_secret: str = ""
    session_ttl_seconds: int = 28800
    model_config = SettingsConfigDict(env_prefix="SOE_", env_file=".env", extra="ignore")


settings = Settings()
