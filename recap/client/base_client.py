from collections.abc import Iterable
from pathlib import Path
from tempfile import gettempdir
from typing import Any, Literal, overload, TYPE_CHECKING
from uuid import UUID, uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from recap.adapter import Backend
from recap.adapter.local import LocalBackend
from recap.client.connection_state import _ConnectionState
from recap.client.permissions import ActorPermissions
from recap.dsl.process_builder import ProcessRunBuilder, ProcessTemplateBuilder
from recap.dsl.query import QueryDSL
from recap.dsl.resource_builder import ResourceBuilder, ResourceTemplateBuilder
from recap.schemas.namespace import NamespaceContext
from recap.schemas.resource import ResourceCopyOptions, ResourceRef, ResourceSchema
from recap.utils.migrations import apply_migrations

if TYPE_CHECKING:
    from recap.client.namespace_client import NamespaceClient

class RecapClient:
    """Primary entry point for interacting with a RECAP provenance database.

    ``RecapClient`` wraps a SQLAlchemy session and exposes factory methods for
    creating and loading the core domain objects: namespaces, resources,
    resource templates, process templates, and process runs.

    Use :meth:`from_sqlite` for local SQLite databases and :meth:`from_url` for
    remote recap servers. These are the canonical initialization methods.

    The client can be used as a context manager, which closes the underlying
    engine on exit:

        with RecapClient.from_sqlite() as client:
            client.create_namespace("projects/my-project")

    """

    def __init__(self, namespace: str | None = None):
        """Initialise common empty client state.

        Use :meth:`from_sqlite` for local clients and :meth:`from_url` for
        remote clients. These are the canonical initialization methods.
        """
        self._namespace_context: NamespaceContext | None = None
        self.namespace_path = self._normalize_namespace(namespace)
        self._connection_state: _ConnectionState | None = None
        self.database_path: Path | None = None
        self.backend: Backend | None = None

    @staticmethod
    def _normalize_namespace(namespace: str | None) -> str:
        return (namespace or "").strip("/")

    def close(self):
        """Close the underlying session and engine to release SQLite locks.

        Safe to call multiple times.  After calling this method the client
        should no longer be used.
        """
        state = getattr(self, "_connection_state", None)
        if state is not None:
            state.release()
            self._connection_state = None
            return
        backend = getattr(self, "backend", None)
        if backend and hasattr(backend, "close"):
            backend.close()
        # Close read_backend separately when it differs from backend (e.g. GraphQLAdapter)
        read_backend = getattr(self, "_read_backend", None)
        if (
            read_backend
            and read_backend is not backend
            and hasattr(read_backend, "close")
        ):
            read_backend.close()
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
    def _from_backends(
        cls,
        read_backend: "Backend",
        write_backend: "Backend",
        *,
        namespace: str | None = None,
        engine: Any = None,
        sessionmaker_: Any = None,
    ) -> "RecapClient":
        """Construct a RecapClient with split read/write backends.

        Internal classmethod used by :meth:`from_url` and :meth:`from_sqlite`.
        The ``backend`` attribute is set to ``write_backend`` for backward
        compatibility with builder methods that reference ``self.backend``.
        ``read_backend`` is stored separately and used by :meth:`query_maker`.
        """
        state = _ConnectionState(
            read_backend=read_backend,
            write_backend=write_backend,
            engine=engine,
            sessionmaker=sessionmaker_,
        )
        instance = cls(namespace=namespace)
        state.acquire()
        instance._connection_state = state
        instance.database_path = None
        instance.backend = write_backend
        instance._read_backend = read_backend
        return instance

    @classmethod
    def from_url(
        cls,
        url: str,
        *,
        api_key: str,
        timeout: float = 30.0,
        namespace: str | None = None,
        unscoped: bool = False,
    ) -> "RecapClient":
        """Connect to a recap webserver.

        Uses :class:`~recap.adapter.graphql.GraphQLAdapter` for reads and
        :class:`~recap.adapter.rest.RESTAdapter` for writes. The client does
        not require access to the server's database filesystem.

        Parameters
        ----------
        url : str
            Base URL of the recap server, e.g. ``"http://localhost:8000"``.
        api_key : str
            API key used to authenticate requests.
        timeout : float, default=30.0
            HTTP request timeout in seconds.
        namespace : str, default=None
            Optional namespace to initialize client with

        Returns
        -------
        RecapClient
            Fully initialized client with GraphQL reads and REST writes.

        Raises
        ------
        RecapConnectionError
            If the server is unreachable or returns an HTTP error response.
        """
        from recap.adapter.graphql import GraphQLAdapter, _RedactedAuthHeaders
        from recap.adapter.rest import RESTAdapter

        if unscoped:
            raise ValueError("Remote clients do not support unscoped=True")

        base = url.rstrip("/")
        header_provider = _RedactedAuthHeaders(api_key)
        read_backend = GraphQLAdapter(
            graphql_url=f"{base}/graphql", _header_provider=header_provider
        )
        write_backend = RESTAdapter(base_url=base, api_key=api_key, timeout=timeout)
        client = cls._from_backends(
            read_backend=read_backend,
            write_backend=write_backend,
            namespace=namespace,
        )
        return client

    def permissions(self, namespace_path: str) -> ActorPermissions:
        """Return typed effective permissions for current remote actor."""
        read_backend = getattr(self, "_read_backend", None)
        if read_backend is None or not hasattr(read_backend, "permissions"):
            raise RuntimeError("Permissions API requires a remote read backend")
        return read_backend.permissions(namespace_path)

    def namespace(self, path: str):
        """Return namespace-scoped facade for ``path`` without persisting it."""
        from recap.client.namespace_client import NamespaceClient

        return NamespaceClient(self, path.strip("/"))

    @classmethod
    def from_sqlite(
        cls,
        path: str | Path | None = None,
        echo: bool = False,
        *,
        namespace: str | None = None,
    ) -> "RecapClient":
        """Create or upgrade a local SQLite database and return a connected client.

        This is the canonical way to create a :class:`RecapClient`.  The
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

        engine = create_engine(db_url, echo=echo)
        sessionmaker_ = sessionmaker(
            bind=engine, expire_on_commit=False, future=True
        )
        backend = LocalBackend(sessionmaker_)
        client = cls._from_backends(
            read_backend=backend,
            write_backend=backend,
            namespace=namespace,
            engine=engine,
            sessionmaker_=sessionmaker_,
        )
        client.engine = engine
        client._sessionmaker = sessionmaker_
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
        on_existing: Literal["silent", "warn", "raise"] = "warn",
        namespace_path: str | None = None,
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
            on_existing: Controls behavior when template already exists:
                ``"warn"`` (default), ``"raise"``, or ``"silent"``.

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
        if self._namespace_context is None or (
            namespace_path is not None
            and self._namespace_context.path != namespace_path
        ):
            raise ValueError("Namespace context is required")

        if process_template_id is not None:
            if args or kwargs:
                raise TypeError(
                    "Pass either an existing process_template_id or name/version, not both"
                )
            return ProcessTemplateBuilder(
                name=None,
                version=None,
                backend=self.backend,
                namespace_id=self._namespace_context.id,
                process_template_id=process_template_id,
                on_existing=on_existing,
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
            namespace_id=self._namespace_context.id,
            on_existing=on_existing,
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
        on_existing: Literal["silent", "warn", "raise"] = "warn",
        namespace_path: str | None = None,
        **kwargs,
    ) -> ProcessRunBuilder:
        """Open a builder for a :class:`~recap.dsl.process_builder.ProcessRunBuilder`.

        Namespace context must be active before calling this method with new
        run arguments.

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
            on_existing: Controls behavior when run already exists:
                ``"warn"`` (default), ``"raise"``, or ``"silent"``.

        Returns:
            A :class:`~recap.dsl.process_builder.ProcessRunBuilder` context
            manager.

        Raises:
            RuntimeError: If the backend has not been initialised.
            ValueError: If no namespace context is set when creating a new run.
            TypeError: On invalid argument combinations.
        """
        if self.backend is None:
            raise RuntimeError("Backend not initialized")
        if namespace_path is not None and (
            self._namespace_context is None
            or self._namespace_context.path != namespace_path
        ):
            raise ValueError("Namespace context is required")

        if process_run_id is not None:
            if args or kwargs:
                raise TypeError(
                    "Pass either an existing process_run_id or name/description/template_name/version, not both"
                )
            return ProcessRunBuilder(
                name=None,
                description=None,
                template_name=None,
                namespace_id=self._namespace_context.id
                if self._namespace_context
                else None,
                backend=self.backend,
                version=None,
                process_run_id=process_run_id,
                on_existing=on_existing,
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

        if self._namespace_context is None:
            raise ValueError("Namespace context is required")

        return ProcessRunBuilder(
            name=name,
            description=description,
            template_name=template_name,
            namespace_id=self._namespace_context.id,
            backend=self.backend,
            version=version,
            on_existing=on_existing,
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
        on_existing: Literal["silent", "warn", "raise"] = "warn",
        namespace_path: str | None = None,
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
            on_existing: Controls behavior when template already exists:
                ``"warn"`` (default), ``"raise"``, or ``"silent"``.

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
        if self._namespace_context is None or (
            namespace_path is not None
            and self._namespace_context.path != namespace_path
        ):
            raise ValueError("Namespace context is required")

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
                namespace_id=self._namespace_context.id,
                resource_template_id=resource_template_id,
                on_existing=on_existing,
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
            namespace_id=self._namespace_context.id,
            on_existing=on_existing,
        )

    @overload
    def build_resource(
        self,
        name: str,
        template_name: str,
        template_version: str = "1.0",
        parent: "ResourceSchema | UUID | None" = None,
    ) -> ResourceBuilder: ...

    @overload
    def build_resource(self, *, resource_id: UUID) -> ResourceBuilder: ...

    def build_resource(
        self,
        *args,
        resource_id: UUID | None = None,
        on_existing: Literal["create", "silent", "warn", "raise"] = "warn",
        parent: "ResourceSchema | UUID | None" = None,
        namespace_path: str | None = None,
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
            on_existing: Controls behavior when resource already exists:
                ``"warn"`` (default), ``"raise"``, ``"silent"``, or
                ``"create"``.
            parent: Optional parent resource for nesting the new resource.
                Accepts a :class:`~recap.schemas.resource.ResourceSchema`
                or a :class:`~uuid.UUID` (which will be resolved to a
                schema via backend query).  Cannot be combined with
                ``resource_id``.

        Returns:
            A :class:`~recap.dsl.resource_builder.ResourceBuilder` context
            manager that commits on clean exit and rolls back on exception.

        Raises:
            RuntimeError: If the backend has not been initialised.
            TypeError: On invalid argument combinations.
        """
        if self.backend is None:
            raise RuntimeError("Backend not initialized")
        if self._namespace_context is None or (
            namespace_path is not None
            and self._namespace_context.path != namespace_path
        ):
            raise ValueError("Namespace context is required")

        if resource_id is not None:
            if args or kwargs:
                raise TypeError(
                    "Pass either an existing resource_id or name/template_name, not both"
                )
            if parent is not None:
                raise TypeError(
                    "Cannot combine resource_id with parent — the resource's "
                    "parent is already determined by the existing resource"
                )
            return ResourceBuilder(
                name=None,
                template_name=None,
                template_version="1.0",
                backend=self.backend,
                namespace_id=self._namespace_context.id,
                resource_id=resource_id,
                on_existing=on_existing,
            )

        resolved_parent = self._resolve_parent(parent)
        name, template_name, template_version = self._parse_resource_args(args, kwargs)

        return ResourceBuilder(
            name=name,
            template_name=template_name,
            template_version=template_version,
            backend=self.backend,
            namespace_id=self._namespace_context.id,
            on_existing=on_existing,
            parent=resolved_parent,
        )

    def _resolve_parent(
        self, parent: "ResourceSchema | UUID | None"
    ) -> "ResourceSchema | None":
        """Resolve a parent argument to a ResourceSchema (or None)."""
        if parent is None:
            return None
        if isinstance(parent, UUID):
            from recap.dsl.query import QuerySpec

            results = self.backend.query(
                ResourceSchema,
                QuerySpec(
                    filters={"id": parent},
                    preloads=["children", "properties"],
                    include_mutable=True,
                ),
                namespace_path=self._namespace_context.path,
            )
            if not results:
                raise ValueError(f"Parent resource with id {parent!r} not found")
            return results[0]
        return parent

    @staticmethod
    def _parse_resource_args(args, kwargs):
        """Extract (name, template_name, template_version) from build_resource args."""
        if args:
            if len(args) != 2:
                raise TypeError("Provide name and template_name")
            name, template_name = args
            return name, template_name, "1.0"
        try:
            name = kwargs.pop("name")
            template_name = kwargs.pop("template_name")
            template_version = kwargs.pop("template_version", "1.0")
        except KeyError as exc:
            raise TypeError("name and template_name are required") from exc
        if kwargs:
            raise TypeError(f"Unexpected keyword arguments: {', '.join(kwargs)}")
        return name, template_name, template_version

    def create_resource(
        self,
        name: str,
        template_name: str,
        template_version: str = "1.0",
        parent: ResourceSchema | None = None,
        on_existing: Literal["create", "silent", "warn", "raise"] = "create",
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
            on_existing: Controls behavior when a resource with the same
                name, parent, and template already exists:

                - ``"create"`` (default): always create a new resource.
                  Resource names are NOT globally unique — multiple resources
                  with the same name can coexist (e.g., for different
                  namespaces).
                - ``"silent"``: reuse the existing resource silently.
                - ``"warn"``: reuse the existing resource and emit a warning.
                - ``"raise"``: raise :class:`ExistingResourceError`.

        Returns:
            A :class:`~recap.schemas.resource.ResourceSchema` representing
            the persisted resource, including any auto-created children.
        """
        if self.backend is None:
            raise RuntimeError("Backend not initialized")
        if self._namespace_context is None:
            raise ValueError("Namespace context is required")
        return ResourceBuilder.create(
            name=name,
            template_name=template_name,
            template_version=template_version,
            backend=self.backend,
            namespace_id=self._namespace_context.id,
            parent=parent,
            on_existing=on_existing,
        )

    def copy_resource(
        self,
        source_resource_id: UUID,
        destination_namespace_id: UUID | None = None,
        options: ResourceCopyOptions | None = None,
        *,
        destination_namespace_path: str | None = None,
    ) -> ResourceSchema:
        """Copy resource across namespaces and commit or roll back atomically.

        Local clients require a destination UUID or active namespace context;
        remote clients use destination path. Returns persisted full schema and
        propagates backend validation or authorization errors.
        """
        if self.backend is None:
            raise RuntimeError("Backend not initialized")
        if destination_namespace_path is not None:
            if (
                self._namespace_context is None
                or self._namespace_context.path != destination_namespace_path
            ):
                raise ValueError("Destination Namespace context is required")
            destination_namespace_id = self._namespace_context.id
        if destination_namespace_id is None:
            raise TypeError(
                "destination_namespace_id or destination_namespace_path is required"
            )
        uow = self.backend.begin()
        try:
            copied = self.backend.copy_resource(
                source_resource_id,
                destination_namespace_id,
                options or ResourceCopyOptions(),
            )
            uow.commit()
            return copied
        except Exception:
            uow.rollback()
            raise

    @overload
    def get_resource(
        self,
        name: str,
        template_name: str,
        template_version: str | None = "1.0",
        *,
        expand: Literal[False] = False,
    ) -> ResourceRef: ...

    @overload
    def get_resource(
        self,
        name: str,
        template_name: str,
        template_version: str | None = "1.0",
        *,
        expand: Literal[True],
    ) -> ResourceSchema: ...

    def get_resource(
        self,
        name: str,
        template_name: str,
        template_version: str | None = "1.0",
        *,
        expand: bool = False,
    ) -> ResourceRef | ResourceSchema:
        """Load a single resource by name and template.

        Looks up the active resource with the given ``name`` whose template
        matches ``template_name`` and ``template_version``. Raises if no such
        resource exists.

        Example::

            plate = client.get_resource("Plate A", "Library Plate", expand=True)
            plate.children["A01"].properties.status.used.value  # False

        Args:
            name: Name of the resource to load.
            template_name: Name of the :class:`ResourceTemplate` the resource
                was instantiated from.
            template_version: Version of the resource template. Defaults to
                ``"1.0"``.
            expand: When ``True``, eagerly hydrate the full resource subtree
                (template, properties, and the entire child hierarchy) and
                return a :class:`~recap.schemas.resource.ResourceSchema`. When
                ``False`` (default), return a lightweight
                :class:`~recap.schemas.resource.ResourceRef`.

        Returns:
            A :class:`~recap.schemas.resource.ResourceRef` when
            ``expand=False``, or a fully hydrated
            :class:`~recap.schemas.resource.ResourceSchema` when
            ``expand=True``.
        """
        if self.backend is None:
            raise RuntimeError("Backend not initialized")
        if self._namespace_context is None:
            raise ValueError("Namespace context is required")
        return self.backend.get_resource(
            self._namespace_context.id,
            name,
            template_name,
            template_version,
            expand=expand,
        )

    def create_namespace(
        self, path: str, metadata: dict[str, Any] | None = None
    ) -> NamespaceContext:
        """Create a namespace and make it active for subsequent writes."""
        if self.backend is None:
            raise RuntimeError("Backend not initialized")
        if self.backend.__class__.__name__ == "RESTAdapter":
            from recap.schemas.namespace import NamespaceSchema

            result = self.backend.create_namespace(path, metadata)
            namespace = NamespaceSchema.model_validate(result.entity)
            self._namespace_context = NamespaceContext(
                id=namespace.id, path=namespace.path
            )
            return self._namespace_context
        uow = self.backend.begin()
        try:
            self._namespace_context = self.backend.create_namespace(path, metadata)
            uow.commit()
        except Exception:
            uow.rollback()
            raise
        return self._namespace_context

    @property
    def namespace_context(self) -> NamespaceContext | None:
        return self._namespace_context

    def set_namespace(self, id: UUID, *, force: bool = False) -> NamespaceContext:
        """Load a namespace by ID and make it active for subsequent writes."""
        if self.backend is None:
            raise RuntimeError("Backend not initialized")
        if not isinstance(id, UUID):
            raise TypeError(f"id should be of type UUID, found {type(id)}")
        if (
            not force
            and self._namespace_context is not None
            and self._namespace_context.id == id
        ):
            return self._namespace_context
        uow = self.backend.begin()
        try:
            self._namespace_context = self.backend.set_namespace(id)
            uow.commit()
        except Exception:
            uow.rollback()
            raise
        return self._namespace_context

    def query_maker(
        self,
        *,
        context: NamespaceContext | None = None,
        namespace: str | None = None,
        on_unloaded: str = "warn",
    ):
        """Return a query DSL scoped to explicit Namespace context."""
        if self.backend is None:
            raise RuntimeError("Backend not initialized")
        if context is None:
            if namespace is None:
                raise TypeError("context or namespace is required")
            context = NamespaceContext(id=UUID(int=0), path=namespace.strip("/"))

        read_backend = getattr(self, "_read_backend", self.backend) or self.backend
        if read_backend is None:
            raise RuntimeError("No read backend available")
        return QueryDSL(
            read_backend,
            context=context,
            on_unloaded=on_unloaded,
        )
