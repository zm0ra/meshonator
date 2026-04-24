from functools import lru_cache
from datetime import datetime, UTC
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Meshonator"
    build_version: str = "dev"
    build_date: str = datetime.now(UTC).isoformat()
    app_env: Literal["dev", "test", "prod"] = "dev"
    app_host: str = "0.0.0.0"
    app_port: int = 8080
    secret_key: str = "change-me-in-prod"

    database_url: str = "sqlite+pysqlite:///./meshonator.db"
    scheduler_enabled: bool = True
    scheduler_sync_cron: str = "*/5 * * * *"
    job_executor_mode: Literal["local", "external"] = "local"

    default_provider: str = "meshtastic"
    meshtastic_default_tcp_port: int = 4403
    discovery_connect_timeout_s: float = 1.5
    provider_timeout_s: float = 10.0

    cli_fallback_enabled: bool = True
    meshcore_enabled: bool = False

    bootstrap_admin_username: str = "admin"
    bootstrap_admin_password: str = "admin"
    bootstrap_admin_role: Literal["viewer", "operator", "admin"] = "admin"

    map_default_lat: float = 52.2297
    map_default_lon: float = 21.0122
    map_default_zoom: int = 5

    cors_allow_origins: list[str] = Field(default_factory=lambda: ["*"])


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
