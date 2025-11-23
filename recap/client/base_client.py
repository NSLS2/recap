from collections.abc import Iterable
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from recap.adapter import Backend
from recap.adapter.local import LocalBackend
from recap.dsl.process_builder import ProcessRunBuilder, ProcessTemplateBuilder
from recap.dsl.query import QueryDSL
from recap.dsl.resource_builder import ResourceBuilder, ResourceTemplateBuilder
from recap.schemas.process import CampaignSchema


class RecapClient:
    def __init__(
        self,
        url: str | None = None,
        echo: bool = False,
    ):
        self._campaign: CampaignSchema | None = None
        self.backend: Backend
        if url is not None:
            parsed = urlparse(url)
            if parsed.scheme in ("http", "https"):
                pass
            elif "sqlite" in parsed.scheme:
                self.engine = create_engine(url, echo=echo)
                self._sessionmaker = sessionmaker(
                    bind=self.engine, expire_on_commit=False, future=True
                )
                self.backend = LocalBackend(self._sessionmaker)

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
            create=create_new,
        )

    def create_resource(self, name: str, template_name: str):
        return ResourceBuilder.create(
            name=name,
            template_name=template_name,
            backend=self.backend,
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
        # if self._session:
        #     SessionLocal = sessionmaker(bind=self._session.get_bind())
        #     return QueryDSL(lambda: SessionLocal())
        # else:
        # return QueryDSL(self._sessionmaker)
        return QueryDSL(self.backend)
