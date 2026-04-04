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
from recap.schemas.process import CampaignSchema
from recap.schemas.resource import ResourceSchema
from recap.utils.migrations import apply_migrations


class RecapClient:
    """Primary entry point for interacting with a RECAP provenance database.

    ``RecapClient`` wraps a SQLAlchemy session and exposes factory methods for
    creating and loading the core domain objects — campaigns, resources,
    resource templates, process templates, and process runs.

    Prefer the :meth:`from_sqlite` class method over constructing an instance
    directly; it handles database creation and schema migrations automatically.

    The client can be used as a context manager, which closes the underlying
    engine on exit::

        with RecapClient.from_sqlite() as client:
            client.create_campaign("my campaign", "proposal-42")

    Attributes:
        database_path: Filesystem path to the SQLite database file, or
            ``None`` when a non-file URL is used.
        backend: The storage backend used to persist domain objects.
    """

    def __init__(
        self,
        url: str | None = None,
        echo: bool = False,
    ):
        """Initialise a client from a database URL.

        In most cases you should use :meth:`from_sqlite` instead, which also
        creates the database file and runs pending migrations.

        Args:
            url: A SQLAlchemy-compatible connection string.  Only
                ``sqlite:///`` URLs are currently supported.  Pass ``None``
                to create an uninitialised client (useful for testing).
            echo: When ``True`` the SQLAlchemy engine will log every SQL
                statement it executes.  Defaults to ``False``.

        Raises:
            NotImplementedError: If an ``http://`` or ``https://`` URL is
                supplied (REST backend is not yet implemented).
            ValueError: If the URL scheme is not recognised.
        """
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
        """Close the underlying session and engine to release SQLite locks.

        Safe to call multiple times.  After calling this method the client
        should no longer be used.
        """
        backend = getattr(self, "backend", None)
        if backend and hasattr(backend, "close"):
            backend.close()
        engine = getattr(self, "engine", None)
        if engine:
            engine.dispose()

    def __enter__(self):
        """Return the client itself when used as a context manager."""
        return self

    def __exit__(self, exc_type, exc, tb):
        """Close the client when leaving the ``with`` block."""
        self.close()

    @classmethod
    def from_sqlite(
        cls, path: str | Path | None = None, echo: bool = False
    ) -> "RecapClient":
        """Create or upgrade a local SQLite database and return a connected client.

        This is the recommended way to create a :class:`RecapClient`.  The
        method creates the database file (and any missing parent directories)
        if it does not already exist, then runs any pending Alembic migrations
        so the schema is always up to date.

        Example::

            # Temporary database (auto-generated filename in the system temp dir)
            client = RecapClient.from_sqlite()

            # Persistent database at a specific path
            client = RecapClient.from_sqlite("/data/my_experiment.db")

        Args:
            path: Filesystem path for the SQLite database.  Accepts a
                ``str`` or :class:`pathlib.Path`.  When omitted a new file
                named ``recap-<uuid>.db`` is created in the system temp
                directory.
            echo: Forward all SQL statements to the Python ``logging``
                infrastructure.  Useful for debugging.  Defaults to
                ``False``.

        Returns:
            A fully initialised :class:`RecapClient` connected to *path*.

        Raises:
            ValueError: If *path* points to an existing directory rather
                than a file.
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
        self, *, process_template_id: UUID
    ) -> ProcessTemplateBuilder: ...

    def build_process_template(
        self,
        *args,
        process_template_id: UUID | None = None,
        strict_checking: bool = False,
        **kwargs,
    ) -> ProcessTemplateBuilder:
        """Open a builder for a :class:`~recap.dsl.process_builder.ProcessTemplateBuilder`.

        Call this method in two mutually exclusive ways:

        **Create or update by name and version** — pass positional arguments
        ``name`` and ``version``::

            with client.build_process_template("MX Data Collection", "1.0") as pt:
                pt.add_step("Mount", order=1)

        **Load an existing template by ID** — pass the keyword argument
        ``process_template_id``::

            with client.build_process_template(
                process_template_id=uuid
            ) as pt:
                ...

        Args:
            name: Human-readable name of the process template (positional).
            version: Version string, e.g. ``"1.0"`` (positional).
            process_template_id: UUID of an existing template to load.  When
                supplied, *name* and *version* must not be provided.
            strict_checking: When ``True``, the builder raises on any
                inconsistency between the submitted model and the database
                record.  Defaults to ``False``.

        Returns:
            A :class:`~recap.dsl.process_builder.ProcessTemplateBuilder`
            context manager that commits on clean exit and rolls back on
            exception.

        Raises:
            RuntimeError: If the backend has not been initialised.
            TypeError: On invalid argument combinations.
        """
        if self.backend is None:
            raise RuntimeError("Backend not initialized")

        if process_template_id is not None:
            if args or kwargs:
                raise TypeError(
                    "Pass either an existing process_template_id or name/version, not both"
                )
            return ProcessTemplateBuilder(
                name=None,
                version=None,
                backend=self.backend,
                process_template_id=process_template_id,
                strict_checking=strict_checking,
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

        return ProcessTemplateBuilder(
            name=name,
            version=version,
            backend=self.backend,
            strict_checking=strict_checking,
        )

    @overload
    def build_process_run(
        self, name: str, description: str, template_name: str, version: str
    ) -> ProcessRunBuilder: ...

    @overload
    def build_process_run(self, *, process_run_id: UUID) -> ProcessRunBuilder: ...

    def build_process_run(
        self,
        *args,
        process_run_id: UUID | None = None,
        strict_checking: bool = False,
        **kwargs,
    ) -> ProcessRunBuilder:
        """Open a builder for a :class:`~recap.dsl.process_builder.ProcessRunBuilder`.

        A :class:`~recap.schemas.process.CampaignSchema` must be active (set
        via :meth:`create_campaign` or :meth:`set_campaign`) before calling
        this method with new run arguments.

        Call this method in two mutually exclusive ways:

        **Create a new run** — pass all four positional arguments::

            with client.build_process_run(
                "Run 001", "First run", "MX Data Collection", "1.0"
            ) as run:
                run.assign_resource(plate, "crystal_plate")

        **Load an existing run by ID**::

            with client.build_process_run(process_run_id=uuid) as run:
                ...

        Args:
            name: Display name for this run (positional).
            description: Free-text description of this run (positional).
            template_name: Name of the :class:`ProcessTemplate` to
                instantiate (positional).
            version: Version of the template (positional).
            process_run_id: UUID of an existing run to load.  When supplied,
                positional arguments must not be provided.
            strict_checking: Raise on model/database inconsistencies.
                Defaults to ``False``.

        Returns:
            A :class:`~recap.dsl.process_builder.ProcessRunBuilder` context
            manager.

        Raises:
            RuntimeError: If the backend has not been initialised.
            ValueError: If no campaign is set when creating a new run.
            TypeError: On invalid argument combinations.
        """
        if self.backend is None:
            raise RuntimeError("Backend not initialized")

        if process_run_id is not None:
            if args or kwargs:
                raise TypeError(
                    "Pass either an existing process_run_id or name/description/template_name/version, not both"
                )
            return ProcessRunBuilder(
                name=None,
                description=None,
                template_name=None,
                campaign=self._campaign,
                backend=self.backend,
                version=None,
                process_run_id=process_run_id,
                strict_checking=strict_checking,
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
            strict_checking=strict_checking,
        )

    @overload
    def build_resource_template(
        self, *, name: str, type_names: list[str], version: str = "1.0"
    ) -> ResourceTemplateBuilder: ...

    @overload
    def build_resource_template(
        self, *, resource_template_id: UUID
    ) -> ResourceTemplateBuilder: ...

    def build_resource_template(
        self,
        *,
        name: str | None = None,
        type_names: list[str] | None = None,
        version: str = "1.0",
        resource_template_id: UUID | None = None,
        strict_checking: bool = False,
    ):
        """Open a builder for a :class:`~recap.dsl.resource_builder.ResourceTemplateBuilder`.

        A :class:`~recap.schemas.resource.ResourceTemplateSchema` is the
        blueprint for a :class:`~recap.schemas.resource.ResourceSchema`.
        This method supports two mutually exclusive call patterns:

        **Create or update a template by name** — the most common usage::

            with client.build_resource_template(
                name="Library Plate",
                type_names=["container", "plate", "library_plate"],
            ) as tb:
                tb.add_properties({"dimensions": [{"name": "rows", "type": "int", "default": 8}]})

        **Load an existing template by ID**::

            with client.build_resource_template(resource_template_id=uuid) as tb:
                ...

        Args:
            name: Unique human-readable name of the template.  Required when
                not supplying *resource_template_id*.
            type_names: List of type tag strings (e.g.
                ``["container", "plate"]``).  Required when not supplying
                *resource_template_id*.
            version: Schema version string.  Defaults to ``"1.0"``.
            resource_template_id: UUID of an existing template to load.
                When supplied, *name* and *type_names* must not be provided.
            strict_checking: Raise on model/database inconsistencies.
                Defaults to ``False``.

        Returns:
            A :class:`~recap.dsl.resource_builder.ResourceTemplateBuilder`
            context manager.

        Raises:
            RuntimeError: If the backend has not been initialised.
            TypeError: If *type_names* is a string, contains non-string
                items, or if conflicting arguments are provided.
        """
        if self.backend is None:
            raise RuntimeError("Backend not initialized")

        if resource_template_id is not None:
            if name is not None or type_names is not None:
                raise TypeError(
                    "Pass either an existing resource_template_id or name/type_names, not both"
                )
            return ResourceTemplateBuilder(
                name=None,
                type_names=None,
                version=version,
                backend=self.backend,
                resource_template_id=resource_template_id,
                strict_checking=strict_checking,
            )

        if name is None or type_names is None:
            raise TypeError("name and type_names are required")

        if isinstance(type_names, str) or not isinstance(type_names, Iterable):
            raise TypeError("type_names must be a collection, not a string")
        if not all(isinstance(item, str) for item in type_names):
            raise TypeError("type_names must only contain strings")
        return ResourceTemplateBuilder(
            name=name,
            type_names=type_names,
            version=version,
            backend=self.backend,
            strict_checking=strict_checking,
        )

    @overload
    def build_resource(
        self, name: str, template_name: str, template_version: str = "1.0"
    ) -> ResourceBuilder: ...

    @overload
    def build_resource(self, *, resource_id: UUID) -> ResourceBuilder: ...

    def build_resource(
        self,
        *args,
        resource_id: UUID | None = None,
        strict_checking: bool = False,
        **kwargs,
    ):
        """Open a builder for a :class:`~recap.dsl.resource_builder.ResourceBuilder`.

        Use this when you need to inspect or modify a resource's property
        values before (or after) persisting them.  For simple creation with
        default values prefer :meth:`create_resource`.

        Call this method in two mutually exclusive ways:

        **Create or update by name and template** — pass positional arguments
        ``name`` and ``template_name``::

            with client.build_resource("Plate A", "Library Plate") as rb:
                model = rb.get_model()
                model.children["A01"].properties.status.used = True
                rb.set_model(model)

        **Load an existing resource by ID**::

            with client.build_resource(resource_id=uuid) as rb:
                ...

        Args:
            name: Display name for the resource (positional).
            template_name: Name of the :class:`ResourceTemplate` to
                instantiate from (positional).
            template_version: Version of the resource template.  Defaults
                to ``"1.0"`` (keyword only).
            resource_id: UUID of an existing resource to load.  When
                supplied, positional arguments must not be provided.
            strict_checking: Raise on model/database inconsistencies.
                Defaults to ``False``.

        Returns:
            A :class:`~recap.dsl.resource_builder.ResourceBuilder` context
            manager that commits on clean exit and rolls back on exception.

        Raises:
            RuntimeError: If the backend has not been initialised.
            TypeError: On invalid argument combinations.
        """
        if self.backend is None:
            raise RuntimeError("Backend not initialized")

        if resource_id is not None:
            if args or kwargs:
                raise TypeError(
                    "Pass either an existing resource_id or name/template_name, not both"
                )
            return ResourceBuilder(
                name=None,
                template_name=None,
                template_version="1.0",
                backend=self.backend,
                resource_id=resource_id,
                strict_checking=strict_checking,
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
            strict_checking=strict_checking,
        )

    def create_resource(
        self,
        name: str,
        template_name: str,
        template_version: str = "1.0",
        parent: ResourceSchema | None = None,
    ):
        """Create a resource instance from a template with default values.

        This is the convenience shortcut when you do not need to override any
        property values before saving.  Child resources defined by the template
        are created automatically and all properties are populated with their
        declared defaults.

        For more control over property values before persisting, use
        :meth:`build_resource` instead.

        Example::

            plate = client.create_resource("Plate A", "Library Plate")
            plate.children["A01"].properties.status.used.value  # False

        Args:
            name: Display name for the new resource.
            template_name: Name of the :class:`ResourceTemplate` to
                instantiate.
            template_version: Version of the resource template.  Defaults
                to ``"1.0"``.
            parent: Optional parent :class:`~recap.schemas.resource.ResourceSchema`
                when the new resource should be nested inside an existing one.

        Returns:
            A :class:`~recap.schemas.resource.ResourceSchema` representing
            the persisted resource, including any auto-created children.
        """
        if self.backend is None:
            raise RuntimeError("Backend not initialized")
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
        """Create a new campaign and make it the active campaign for this client.

        A :class:`~recap.schemas.process.CampaignSchema` is a top-level
        grouping object that all :class:`ProcessRun` instances belong to.  You
        must create or set a campaign before calling :meth:`build_process_run`
        with new run arguments.

        Example::

            client.create_campaign(
                name="MX Beamtime April 2026",
                proposal="MX-2026-001",
                saf="SAF-42",
            )

        Args:
            name: Human-readable name for the campaign.
            proposal: Proposal or project identifier associated with this
                campaign.
            saf: Safety Approval Form (SAF) or equivalent authorization
                reference.  Optional.
            metadata: Arbitrary JSON-serialisable key/value pairs to store
                alongside the campaign record.  Optional.

        Returns:
            ``None``.  The created campaign is stored as the client's active
            campaign (accessible via ``client._campaign``).
        """
        if self.backend is None:
            raise RuntimeError("Backend not initialized")
        uow = self.backend.begin()
        try:
            self._campaign = self.backend.create_campaign(name, proposal, saf, metadata)
            uow.commit()
        except Exception:
            uow.rollback()
            raise

    def set_campaign(self, id: UUID):
        """Load an existing campaign by ID and make it the active campaign.

        Use this to resume work against a campaign that was created in a
        previous session or by another client instance.

        Example::

            client.set_campaign(existing_campaign_id)

        Args:
            id: The UUID of the campaign to activate.

        Returns:
            ``None``.  The loaded campaign is stored as the client's active
            campaign.
        """
        if self.backend is None:
            raise RuntimeError("Backend not initialized")
        uow = self.backend.begin()
        try:
            self._campaign = self.backend.set_campaign(id)
            uow.commit()
        except Exception:
            uow.rollback()
            raise

    def query_maker(self, *, campaign=None):
        """Return a :class:`~recap.dsl.query.QueryDSL` scoped to a campaign.

        The returned object exposes a fluent query API for retrieving
        resources, process runs, and their relationships from the database.

        If *campaign* is omitted the client's currently active campaign is
        used.  Pass an explicit campaign (or its UUID) to query a different
        one without changing the client's active campaign.

        Example::

            qm = client.query_maker()
            resources = qm.resources().of_type("library_plate").all()

        Args:
            campaign: A :class:`~recap.schemas.process.CampaignSchema` instance
                or its UUID to scope the query.  When ``None`` the active
                campaign is used (if one is set).

        Returns:
            A :class:`~recap.dsl.query.QueryDSL` instance.

        Raises:
            RuntimeError: If the backend has not been initialised.
        """
        if self.backend is None:
            raise RuntimeError("Backend not initialized")

        campaign_id = None
        if campaign is not None:
            campaign_id = getattr(campaign, "id", campaign)
        elif self._campaign is not None:
            campaign_id = self._campaign.id

        return QueryDSL(self.backend, campaign_id=campaign_id)
