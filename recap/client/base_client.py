from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from recap.dsl.process_builder import ProcessRunBuilder, ProcessTemplateBuilder
from recap.dsl.resource_builder import ResourceTemplateBuilder


class RecapClient:
    def __init__(self, url: str | None = None, echo: bool = False, session=None):
        if url is not None:
            self.engine = create_engine(url, echo=echo)
            self.Session = sessionmaker(
                bind=self.engine, expire_on_commit=False, future=True
            )
        if session is not None:
            self._session = session

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
        return ProcessRunBuilder(
            session=self._session,
            name=name,
            template_name=template_name,
            version=version,
        )

    def resource_template(self, name: str, type_names: list[str]):
        return ResourceTemplateBuilder(
            session=self._session, name=name, type_names=type_names
        )
