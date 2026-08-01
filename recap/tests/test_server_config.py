from pathlib import Path

import pytest
import yaml
from pydantic import SecretStr, ValidationError


def test_server_config_defaults():
    from recap.server.config import ServerConfig

    cfg = ServerConfig(db_path="/tmp/test.db", api_key="test-secret")
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 8000
    assert cfg.log_level == "info"
    assert cfg.db_path == Path("/tmp/test.db")
    assert cfg.authentication_mode == "single-user"
    assert cfg.api_key == SecretStr("test-secret")
    assert cfg.entitlement_snapshot_path is None
    assert cfg.entitlement_snapshot_max_age_seconds == 300
    assert cfg.audit_log_path == Path("recap-audit.jsonl")


def test_server_config_from_yaml(tmp_path):
    from recap.server.config import ServerConfig

    config_file = tmp_path / "recap-server.yaml"
    config_file.write_text(
        yaml.dump(
            {
                "server": {
                    "db_path": str(tmp_path / "recap.db"),
                    "port": 9000,
                    "api_key": "yaml-secret",
                }
            }
        )
    )
    cfg = ServerConfig.from_yaml(config_file)
    assert cfg.port == 9000
    assert cfg.db_path == tmp_path / "recap.db"


def test_server_config_from_yaml_missing_db_path(tmp_path):
    from pydantic import ValidationError

    from recap.server.config import ServerConfig

    config_file = tmp_path / "recap-server.yaml"
    config_file.write_text(yaml.dump({"server": {"port": 9000}}))
    with pytest.raises(ValidationError):
        ServerConfig.from_yaml(config_file)


def test_server_config_env_override(tmp_path, monkeypatch):
    from recap.server.config import ServerConfig

    monkeypatch.setenv("RECAP_PORT", "7777")
    monkeypatch.setenv("RECAP_DB_PATH", str(tmp_path / "env.db"))
    monkeypatch.setenv("RECAP_API_KEY", "env-secret")
    cfg = ServerConfig()
    assert cfg.port == 7777
    assert cfg.db_path == tmp_path / "env.db"


def test_single_user_mode_requires_api_key():
    from recap.server.config import ServerConfig

    with pytest.raises(ValidationError, match="API key is required"):
        ServerConfig(db_path="/tmp/test.db")


def test_multi_user_mode_requires_entitlement_snapshot():
    from recap.server.config import ServerConfig

    with pytest.raises(ValidationError, match="snapshot path is required"):
        ServerConfig(db_path="/tmp/test.db", authentication_mode="multi-user")


def test_multi_user_mode_accepts_snapshot_without_api_key(tmp_path):
    from recap.server.config import ServerConfig

    cfg = ServerConfig(
        db_path=tmp_path / "test.db",
        authentication_mode="multi-user",
        entitlement_snapshot_path=tmp_path / "entitlements.json",
    )

    assert cfg.api_key is None


def test_server_config_repr_redacts_api_key():
    from recap.server.config import ServerConfig

    cfg = ServerConfig(db_path="/tmp/test.db", api_key="never-print-this")

    assert "never-print-this" not in repr(cfg)


def test_entitlement_snapshot_max_age_must_be_positive():
    from recap.server.config import ServerConfig

    with pytest.raises(ValidationError, match="maximum age must be positive"):
        ServerConfig(
            db_path="/tmp/test.db",
            api_key="test-secret",
            entitlement_snapshot_max_age_seconds=0,
        )
