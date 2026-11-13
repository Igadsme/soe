from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "sqlite:///./soe.db"
    env: str = "development"
    demo_mode: bool = True
    model_config = SettingsConfigDict(env_prefix="SOE_", env_file=".env", extra="ignore")


settings = Settings()
