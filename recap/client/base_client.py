from contextlib import contextmanager
from typing import Any
from uuid import UUID

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from recap.dsl.process_builder import ProcessRunBuilder, ProcessTemplateBuilder
from recap.dsl.resource_builder import ResourceTemplateBuilder
from recap.models.campaign import Campaign


class RecapClient:
    def __init__(self, url: str | None = None, echo: bool = False, session=None):
        if url is not None:
            self.engine = create_engine(url, echo=echo)
            self.Session = sessionmaker(
                bind=self.engine, expire_on_commit=False, future=True
            )
        if session is not None:
            self._session = session
        self._campaign = None

    @contextmanager
    def session(self):
        """Yield a Session with transaction boundaries."""
        with self.Session() as session:
            try:
                with session.begin():
                    yield session
            finally:
                # Session closed by context exit
                ...

    def process_template(self, name: str, version: str) -> ProcessTemplateBuilder:
        session = self._session
        return ProcessTemplateBuilder(session=session, name=name, version=version)

    def process_run(self, name: str, template_name: str, version: str):
        if self._campaign is not None:
            return ProcessRunBuilder(
                session=self._session,
                name=name,
                template_name=template_name,
                version=version,
            )
        else:
            raise ValueError(
                "Campaign not set, cannot create process run. Use create_campaign() or set_campaign() first"
            )

    def resource_template(self, name: str, type_names: list[str]):
        return ResourceTemplateBuilder(
            session=self._session, name=name, type_names=type_names
        )

    def create_campaign(
        self,
        name: str,
        proposal: str,
        saf: str | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        self._campaign = Campaign(
            name=name,
            proposal=str(proposal),
            saf=saf,
            metadata=metadata,
        )
        self._session.add(self._campaign)
        self._session.flush()
        return self._campaign

    def set_campaign(self, id: UUID):
        statement = select(Campaign).filter_by(id=id)
        self._campaign = self.session.execute(statement).scalar_one_or_none()
        if self._campaign is None:
            raise ValueError(f"Campaign with ID {id} not found")
