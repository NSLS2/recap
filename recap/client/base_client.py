from contextlib import contextmanager
from typing import Any
from uuid import UUID

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from recap.dsl.process_builder import ProcessRunBuilder, ProcessTemplateBuilder
from recap.dsl.resource_builder import ResourceTemplateBuilder
from recap.models.campaign import Campaign


class RecapClient:
    def __init__(
        self, url: str | None = None, echo: bool = False, session: Session | None = None
    ):
        self._session: Session | None = None
        self._campaign: Campaign | None = None
        if url is not None:
            self.engine = create_engine(url, echo=echo)
            self._sessionmaker = sessionmaker(
                bind=self.engine, expire_on_commit=False, future=True
            )
        if session is not None:
            self._session = session

    @contextmanager
    def session(self):
        if self._session is not None:
            yield self._session
            return
        s = self._sessionmaker()
        try:
            yield s
        finally:
            s.close()

    def process_template(self, name: str, version: str) -> ProcessTemplateBuilder:
        with self.session() as session:
            return ProcessTemplateBuilder(session=session, name=name, version=version)

    def process_run(self, name: str, template_name: str, version: str):
        if self._campaign is None:
            raise ValueError(
                "Campaign not set, cannot create process run. Use create_campaign() or set_campaign() first"
            )
        with self.session() as session:
            return ProcessRunBuilder(
                session=session,
                name=name,
                template_name=template_name,
                campaign=self._campaign,
                version=version,
            )

    def resource_template(self, name: str, type_names: list[str]):
        with self.session() as session:
            return ResourceTemplateBuilder(
                session=session, name=name, type_names=type_names
            )

    def create_campaign(
        self,
        name: str,
        proposal: str,
        saf: str | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        with self.session() as session:
            self._campaign = Campaign(
                name=name,
                proposal=str(proposal),
                saf=saf,
                metadata=metadata,
            )
            session.add(self._campaign)
            session.flush()
            return self._campaign

    def set_campaign(self, id: UUID):
        statement = select(Campaign).filter_by(id=id)
        with self.session() as session:
            self._campaign = session.execute(statement).scalar_one_or_none()
        if self._campaign is None:
            raise ValueError(f"Campaign with ID {id} not found")
