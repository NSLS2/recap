from collections.abc import Mapping
from importlib import resources
from pathlib import Path
from uuid import UUID

from alembic import command
from alembic.config import Config


def get_migration_path() -> Path:
    """Return the filesystem path to the bundled Alembic migrations."""
    return Path(resources.files("recap.db").joinpath("migrations"))


def build_alembic_config(db_url: str, script_location: Path | None = None) -> Config:
    """Create an Alembic Config pointing at the packaged migrations and a DB URL."""
    config = Config()
    location = script_location or get_migration_path()
    config.set_main_option("script_location", str(location))
    config.set_main_option("sqlalchemy.url", db_url)
    return config


def apply_migrations(
    db_url: str,
    revision: str = "head",
    *,
    campaign_namespace_paths: Mapping[UUID | str, str] | None = None,
    base_namespace_path: str = "",
) -> None:
    """Apply migrations, using the canonical root and campaign path defaults.

    An empty ``base_namespace_path`` represents the canonical root namespace.
    Campaign paths default to ``campaign/<uuid>`` when no explicit mapping is
    provided.
    """
    config = build_alembic_config(db_url)
    config.attributes["campaign_namespace_paths"] = {
        str(campaign_id): path
        for campaign_id, path in (campaign_namespace_paths or {}).items()
    }
    config.attributes["base_namespace_path"] = base_namespace_path
    command.upgrade(config, revision)


def downgrade_migrations(db_url: str, revision: str = "base") -> None:
    """Downgrade migrations to the requested revision for the given database."""
    config = build_alembic_config(db_url)
    command.downgrade(config, revision)
