from collections.abc import Iterable
from pathlib import Path
from tempfile import gettempdir
from typing import Any, overload
from urllib.parse import urlparse
from uuid import UUID, uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from recap.adapter import Backend
from recap.adapter.local import LocalBackend
from recap.dsl.process_builder import ProcessRunBuilder, ProcessTemplateBuilder
from recap.dsl.query import QueryDSL
from recap.dsl.resource_builder import ResourceBuilder, ResourceTemplateBuilder
from recap.schemas.process import (
    CampaignSchema,
    ProcessRunSchema,
    ProcessTemplateRef,
    ProcessTemplateSchema,
)
from recap.schemas.resource import (
    ResourceSchema,
    ResourceTemplateRef,
    ResourceTemplateSchema,
)
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

    @overload
    def build_process_template(
        self, name: str, version: str
    ) -> ProcessTemplateBuilder: ...

    @overload
    def build_process_template(
        self, *, process_template: ProcessTemplateRef | ProcessTemplateSchema
    ) -> ProcessTemplateBuilder: ...

    def build_process_template(
        self,
        *args,
        process_template: ProcessTemplateRef | ProcessTemplateSchema | None = None,
        **kwargs,
    ) -> ProcessTemplateBuilder:
        if self.backend is None:
            raise RuntimeError("Backend not initialized")

        if process_template is not None:
            if args or kwargs:
                raise TypeError(
                    "Pass either an existing process_template or name/version, not both"
                )
            return ProcessTemplateBuilder(
                name=process_template.name,
                version=process_template.version,
                backend=self.backend,
                process_template=process_template,
            )

        if args:
            if len(args) != 2:
                raise TypeError("Provide name and version")
            name, version = args
        else:
            try:
                name = kwargs.pop("name")
                version = kwargs.pop("version")
            except KeyError as exc:
                raise TypeError("name and version are required") from exc
            if kwargs:
                raise TypeError(f"Unexpected keyword arguments: {', '.join(kwargs)}")

        return ProcessTemplateBuilder(name=name, version=version, backend=self.backend)

    @overload
    def build_process_run(
        self, name: str, description: str, template_name: str, version: str
    ) -> ProcessRunBuilder: ...

    @overload
    def build_process_run(
        self, *, process_run: ProcessRunSchema
    ) -> ProcessRunBuilder: ...

    def build_process_run(
        self,
        *args,
        process_run: ProcessRunSchema | None = None,
        **kwargs,
    ) -> ProcessRunBuilder:
        if self.backend is None:
            raise RuntimeError("Backend not initialized")

        if process_run is not None:
            if args or kwargs:
                raise TypeError(
                    "Pass either an existing process_run or name/description/template_name/version, not both"
                )
            template = process_run.template
            return ProcessRunBuilder(
                name=process_run.name,
                description=process_run.description,
                template_name=template.name,
                campaign=self._campaign,
                backend=self.backend,
                version=template.version,
                process_run=process_run,
            )

        if args:
            if len(args) != 4:
                raise TypeError(
                    "Provide exactly four positional arguments: name, description, template_name, version"
                )
            name, description, template_name, version = args
        else:
            try:
                name = kwargs.pop("name")
                description = kwargs.pop("description")
                template_name = kwargs.pop("template_name")
                version = kwargs.pop("version")
            except KeyError as exc:
                raise TypeError(
                    "name, description, template_name, and version are required"
                ) from exc
            if kwargs:
                raise TypeError(f"Unexpected keyword arguments: {', '.join(kwargs)}")

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

    @overload
    def build_resource_template(
        self, *, name: str, type_names: list[str], version: str = "1.0"
    ) -> ResourceTemplateBuilder: ...

    @overload
    def build_resource_template(
        self,
        *,
        resource_template: ResourceTemplateRef | ResourceTemplateSchema,
    ) -> ResourceTemplateBuilder: ...

    def build_resource_template(
        self,
        *,
        name: str | None = None,
        type_names: list[str] | None = None,
        version: str = "1.0",
        resource_template: ResourceTemplateRef | ResourceTemplateSchema | None = None,
    ):
        if self.backend is None:
            raise RuntimeError("Backend not initialized")

        if resource_template is not None:
            if name is not None or type_names is not None:
                raise TypeError(
                    "Pass either an existing resource_template or name/type_names, not both"
                )
            return ResourceTemplateBuilder(
                name=resource_template.name,
                type_names=[rt.name for rt in resource_template.types],
                version=resource_template.version,
                backend=self.backend,
                resource_template=resource_template,
            )

        if name is None or type_names is None:
            raise TypeError("name and type_names are required")

        if isinstance(type_names, str) or not isinstance(type_names, Iterable):
            raise TypeError("type_names must be a collection, not a string")
        if not all(isinstance(item, str) for item in type_names):
            raise TypeError("type_names must only contain strings")
        return ResourceTemplateBuilder(
            name=name, type_names=type_names, version=version, backend=self.backend
        )

    @overload
    def build_resource(
        self, name: str, template_name: str, template_version: str = "1.0"
    ) -> ResourceBuilder: ...

    @overload
    def build_resource(self, *, resource: ResourceSchema) -> ResourceBuilder: ...

    def build_resource(
        self,
        *args,
        resource: ResourceSchema | None = None,
        **kwargs,
    ):
        if self.backend is None:
            raise RuntimeError("Backend not initialized")

        if resource is not None:
            if args or kwargs:
                raise TypeError(
                    "Pass either an existing resource or name/template_name, not both"
                )
            return ResourceBuilder(
                name=resource.name,
                template_name=resource.template.name,
                template_version=resource.template.version,
                backend=self.backend,
                resource=resource,
            )

        if args:
            if len(args) != 2:
                raise TypeError("Provide name and template_name")
            name, template_name = args
            template_version = "1.0"
        else:
            try:
                name = kwargs.pop("name")
                template_name = kwargs.pop("template_name")
                template_version = kwargs.pop("template_version", "1.0")
            except KeyError as exc:
                raise TypeError("name and template_name are required") from exc
            if kwargs:
                raise TypeError(f"Unexpected keyword arguments: {', '.join(kwargs)}")

        return ResourceBuilder(
            name=name,
            template_name=template_name,
            template_version=template_version,
            backend=self.backend,
        )

    def create_resource(
        self,
        name: str,
        template_name: str,
        template_version: str = "1.0",
        parent: ResourceSchema | None = None,
    ):
        return ResourceBuilder.create(
            name=name,
            template_name=template_name,
            template_version=template_version,
            backend=self.backend,
            parent=parent,
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
