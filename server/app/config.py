from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Process-level settings, read from the environment (.env in compose)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://multichat:multichat@localhost:5432/multichat"
    multichat_api_token: str = ""
    multichat_secret_key: str = ""
    data_dir: Path = Path("/data")
    config_file: Path = Path("/config/config.yaml")

    # Agent defaults; config.yaml `defaults:` overrides these at runtime.
    max_steps: int = 40
    command_timeout: int = 600
    tool_output_limit: int = 30_000
    anthropic_max_tokens: int = 8192


@lru_cache
def get_settings() -> Settings:
    return Settings()
