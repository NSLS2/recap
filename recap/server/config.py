from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import SecretStr, field_validator, model_validator
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
    authentication_mode: Literal["single-user", "multi-user"] = "single-user"
    api_key: SecretStr | None = None
    entitlement_snapshot_path: Path | None = None
    entitlement_snapshot_max_age_seconds: int = 300
    audit_log_path: Path = Path("recap-audit.jsonl")

    @field_validator(
        "db_path", "entitlement_snapshot_path", "audit_log_path", mode="before"
    )
    @classmethod
    def coerce_path(cls, v: object) -> Path | None:
        if v is None:
            return None
        return Path(str(v))

    @model_validator(mode="after")
    def validate_authentication(self) -> ServerConfig:
        if self.authentication_mode == "single-user":
            if self.api_key is None or not self.api_key.get_secret_value():
                raise ValueError("API key is required for single-user authentication")
        elif self.entitlement_snapshot_path is None:
            raise ValueError(
                "Entitlement snapshot path is required for multi-user authentication"
            )
        if self.entitlement_snapshot_max_age_seconds <= 0:
            raise ValueError("Entitlement snapshot maximum age must be positive")
        return self

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
