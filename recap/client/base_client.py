from collections.abc import Iterable
from pathlib import Path
from tempfile import gettempdir
from typing import Any
from urllib.parse import urlparse
from uuid import UUID, uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from recap.adapter import Backend
from recap.adapter.local import LocalBackend
from recap.dsl.process_builder import ProcessRunBuilder, ProcessTemplateBuilder
from recap.dsl.query import QueryDSL
from recap.dsl.resource_builder import ResourceBuilder, ResourceTemplateBuilder
from recap.schemas.process import CampaignSchema
from recap.schemas.resource import ResourceSchema
from recap.utils.migrations import apply_migrations


class RecapClient:
    def __init__(
        self,
        url: str | None = None,
        echo: bool = False,
    ):
        self._campaign: CampaignSchema | None = None
        self.database_path: Path | None = None
        self.backend: Backend | None = None
        if url is not None:
            parsed = urlparse(url)
            if parsed.scheme in ("http", "https"):
                raise NotImplementedError("Rest api via HTTP(S) is not yet implemented")
            elif "sqlite" in parsed.scheme:
                if parsed.path and parsed.path != "/:memory:":
                    self.database_path = Path(parsed.path)
                self.engine = create_engine(url, echo=echo)
                self._sessionmaker = sessionmaker(
                    bind=self.engine, expire_on_commit=False, future=True
                )
                self.backend = LocalBackend(self._sessionmaker)
            else:
                raise ValueError(f"Unknown scheme: {parsed.scheme}")

    def close(self):
        """Close the underlying session/engine to release SQLite locks."""
        backend = getattr(self, "backend", None)
        if backend and hasattr(backend, "close"):
            backend.close()
        engine = getattr(self, "engine", None)
        if engine:
            engine.dispose()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    @classmethod
    def from_sqlite(
        cls, path: str | Path | None = None, echo: bool = False
    ) -> "RecapClient":
        """
        Create or upgrade a local SQLite database and return a connected client.

        If no path is provided a new database file is created under the system
        temp directory. When the file already exists, migrations are applied to
        bring it up to date.
        """
        target_path = (
            Path(path)
            if path is not None
            else Path(gettempdir()) / f"recap-{uuid4().hex}.db"
        )
        if target_path.is_dir():
            raise ValueError("Path must point to a database file, not a directory")
        target_path.parent.mkdir(parents=True, exist_ok=True)

        db_url = f"sqlite:///{target_path}"
        apply_migrations(db_url)

        client = cls(url=db_url, echo=echo)
        client.database_path = target_path
        return client

    def build_process_template(self, name: str, version: str) -> ProcessTemplateBuilder:
        return ProcessTemplateBuilder(name=name, version=version, backend=self.backend)

    def build_process_run(
        self, name: str, description: str, template_name: str, version: str
    ):
        if self._campaign is None:
            raise ValueError(
                "Campaign not set, cannot create process run. Use create_campaign() or set_campaign() first"
            )
        return ProcessRunBuilder(
            name=name,
            description=description,
            template_name=template_name,
            campaign=self._campaign,
            backend=self.backend,
            version=version,
        )

    def build_resource_template(self, name: str, type_names: list[str]):
        if isinstance(type_names, str) or not isinstance(type_names, Iterable):
            raise TypeError("type_names must be a collection, not a string")
        if not all(isinstance(item, str) for item in type_names):
            raise TypeError("type_names must only contain strings")
        return ResourceTemplateBuilder(
            name=name, type_names=type_names, backend=self.backend
        )

    def build_resource(self, name: str, template_name: str, create_new=True):
        return ResourceBuilder(
            name=name,
            template_name=template_name,
            backend=self.backend,
            create_new=create_new,
        )

    def create_resource(
        self, name: str, template_name: str, parent: ResourceSchema | None = None
    ):
        return ResourceBuilder.create(
            name=name, template_name=template_name, backend=self.backend, parent=parent
        )

    def create_campaign(
        self,
        name: str,
        proposal: str,
        saf: str | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        uow = self.backend.begin()
        try:
            self._campaign = self.backend.create_campaign(name, proposal, saf, metadata)
            uow.commit()
        except Exception:
            uow.rollback()
            raise

    def set_campaign(self, id: UUID):
        uow = self.backend.begin()
        try:
            self._campaign = self.backend.set_campaign(id)
            uow.commit()
        except Exception:
            uow.rollback()
            raise

    def query_maker(self):
        return QueryDSL(self.backend)
