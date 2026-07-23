from pathlib import Path

import pytest
import yaml


def test_server_config_defaults():
    from recap.server.config import ServerConfig

    cfg = ServerConfig(db_path="/tmp/test.db")
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 8000
    assert cfg.log_level == "info"
    assert cfg.db_path == Path("/tmp/test.db")


def test_server_config_from_yaml(tmp_path):
    from recap.server.config import ServerConfig

    config_file = tmp_path / "recap-server.yaml"
    config_file.write_text(
        yaml.dump({"server": {"db_path": str(tmp_path / "recap.db"), "port": 9000}})
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
    cfg = ServerConfig()
    assert cfg.port == 7777
    assert cfg.db_path == tmp_path / "env.db"
