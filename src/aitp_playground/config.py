"""Service configuration loaded from environment."""
from __future__ import annotations

from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    port: int = 8000
    host: str = "0.0.0.0"
    scenarios_dir: str = "./scenarios"
    registry_cache_ttl_ms: int = 0
    agent_base_port: int = 8100
    agent_python: str = "python3"
    playground_base_url: str = "http://localhost:8000"
    cp_base_url: str = ""
    cp_api_key: str = ""
    cp_timeout_ms: int = 5000
    anthropic_api_key: str = ""
    openai_api_key: str = ""
    log_level: str = "INFO"

    @property
    def scenarios_path(self) -> Path:
        return Path(self.scenarios_dir).resolve()


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
