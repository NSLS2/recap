from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ServerConfig(BaseSettings):
    """Configuration for the recap GraphQL server.

    Values can be set via:
    1. Environment variables prefixed with RECAP_ (e.g. RECAP_PORT=9000)
    2. YAML config file via ServerConfig.from_yaml()
    3. Direct instantiation kwargs
    """

    model_config = SettingsConfigDict(env_prefix="RECAP_")

    db_path: Path
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "info"

    @field_validator("db_path", mode="before")
    @classmethod
    def coerce_db_path(cls, v: object) -> Path:
        return Path(str(v))

    @classmethod
    def from_yaml(cls, path: str | Path) -> ServerConfig:
        """Load ServerConfig from a YAML file.

        YAML must have a top-level 'server:' key. db_path is required.
        CLI/env vars still override YAML values when set.
        Raises FileNotFoundError if the config file does not exist.
        """
        with open(path) as f:
            raw = yaml.safe_load(f)
        section = raw.get("server", {})
        return cls(**section)
