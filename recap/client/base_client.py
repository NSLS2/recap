from collections.abc import Iterable, Mapping
from pathlib import Path
from tempfile import gettempdir
from typing import Any, Literal, overload
from uuid import UUID, uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from recap.client.backend import ClientBackend
from recap.client.connection_state import ConnectionState
from recap.client.permissions import ActorPermissions
from recap.commands.context import build_local_command_context
from recap.commands.errors import CommandValidationError
from recap.commands.models import (
    CopyProcessRun,
    CopyResource,
    CreateNamespace,
    UpdateNamespace,
)
from recap.dsl.process_builder import ProcessRunBuilder, ProcessTemplateBuilder
from recap.dsl.query import QueryDSL
from recap.dsl.resource_builder import ResourceBuilder, ResourceTemplateBuilder
from recap.exceptions import RecapNotFoundError
from recap.lifecycle import LifecycleStatus
from recap.schemas.namespace import NamespaceContext
from recap.schemas.process import ProcessRunCopyOptions, ProcessRunSchema
from recap.schemas.resource import ResourceCopyOptions, ResourceRef, ResourceSchema
from recap.utils.migrations import apply_migrations


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
            client.create_namespace("projects")
            client.create_namespace("projects/my-project")

    """

    @overload
    def __init__(self, connection_state: ConnectionState, *, namespace_path: str): ...

    @overload
    def __init__(
        self, connection_state: ConnectionState, *, namespace_context: NamespaceContext
    ): ...

    def __init__(
        self,
        connection_state: ConnectionState,
        *,
        namespace_path: str | None = None,
        namespace_context: NamespaceContext | None = None,
    ):
        """Initialise common empty client state.

        Use :meth:`from_sqlite` for local clients and :meth:`from_url` for
        remote clients. These are the canonical initialization methods.
        """
        namespace_path = namespace_path if namespace_path is not None else ""
        self.connection_state: ConnectionState = connection_state
        self.connection_state.acquire()
        self._namespace_context: NamespaceContext = (
            namespace_context
            if namespace_context is not None
            else self._resolve_namespace_context(namespace_path)
        )
        self._closed = False

    def __repr__(self) -> str:
        return f"RecapClient({self.namespace_path=})"

    @staticmethod
    def _normalize_namespace(namespace: str | None) -> str:
        return (namespace or "").strip("/")

    @property
    def namespace_path(self):
        return self._namespace_context.path

    def close(self):
        """Close the underlying session and engine to release SQLite locks.

        Safe to call multiple times.  After calling this method the client
        should no longer be used.
        """
        if self._closed:
            return
        self.connection_state.release()
        self._closed = True

    def __enter__(self):
        """Return the client itself when used as a context manager."""
        return self

    def __exit__(self, exc_type, exc, tb):
        """Close the client when leaving the ``with`` block."""
        self.close()

    @classmethod
    def _from_backends(
        cls,
        backend: ClientBackend,
        *,
        namespace: str,
        engine: Any = None,
        sessionmaker_: Any = None,
        database_path: Path | None = None,
    ) -> "RecapClient":
        """Construct a RecapClient with composed backend capabilities."""
        state = ConnectionState(
            backend=backend,
            engine=engine,
            sessionmaker=sessionmaker_,
            database_path=database_path,
        )
        instance = cls(connection_state=state, namespace_path=namespace)
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

        Uses :class:`~recap.adapter.rest.RESTAdapter` for reads and writes. The client does
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
            Fully initialized client with REST reads and writes.

        Raises
        ------
        RecapConnectionError
            If the server is unreachable.
        RecapProtocolError
            If the server returns a malformed response.
        RecapRequestError
            If the server returns an API error response.
        """
        from recap.adapter.http_transport import HTTPTransport
        from recap.adapter.rest import RESTAdapter

        if unscoped:
            raise ValueError("Remote clients do not support unscoped=True")

        base = url.rstrip("/")
        transport = HTTPTransport(api_key, timeout=timeout)
        rest = RESTAdapter(base_url=base, _transport=transport)
        client = cls._from_backends(
            backend=ClientBackend(
                reader=rest,
                writer=rest,
                namespaces=rest,
                namespace_writer=rest,
                context_resolver=rest,
                permissions=rest,
            ),
            namespace=namespace,
        )
        return client

    def permissions(self) -> ActorPermissions:
        """Return typed effective permissions for this client's namespace."""
        if self.connection_state.backend.permissions is None:
            raise RuntimeError("Permissions API requires a remote read backend")
        return self.connection_state.backend.permissions.permissions(
            self.namespace_path
        )

    def namespace(self, path: str) -> "RecapClient":
        """Return a view scoped to an additive namespace path."""
        if not isinstance(path, str):
            raise TypeError("namespace path must be a string")
        child_path = self._normalize_namespace(path)
        namespace_path = "/".join(
            part for part in (self.namespace_path, child_path) if part
        )
        child_namespace_context = self._resolve_namespace_context(namespace_path)
        view = self.__class__(
            connection_state=self.connection_state,
            namespace_context=child_namespace_context,
        )
        return view

    def __getitem__(self, namespace: str) -> "RecapClient":
        return self.namespace(namespace)

    def _resolve_namespace_context(
        self, namespace_path: str | None
    ) -> NamespaceContext:
        namespace_path = (
            namespace_path if namespace_path is not None else self.namespace_path
        )
        try:
            return self.connection_state.backend.context_resolver.get_namespace_context(
                namespace_path
            )
        except LookupError:
            raise

    @staticmethod
    def _command_context():
        return build_local_command_context()

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
        sessionmaker_ = sessionmaker(bind=engine, expire_on_commit=False, future=True)
        from recap.adapter.local import LocalBackend

        backend = LocalBackend(sessionmaker_)
        client = cls._from_backends(
            backend=ClientBackend(
                reader=backend,
                writer=backend,
                namespaces=backend,
                namespace_writer=backend,
                context_resolver=backend,
            ),
            namespace=namespace,
            engine=engine,
            sessionmaker_=sessionmaker_,
            database_path=target_path,
        )
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
        **kwargs,
    ) -> ProcessTemplateBuilder:
        """Open a builder for a
        :class:`~recap.dsl.process_builder.ProcessTemplateBuilder`.

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
        if process_template_id is not None:
            if args or kwargs:
                raise TypeError(
                    "Pass either an existing process_template_id or "
                    "name/version, not both"
                )
            return ProcessTemplateBuilder(
                name=None,
                version=None,
                backend=self.connection_state.backend,
                command_context=self._command_context(),
                namespace_context=self.namespace_context,
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
            backend=self.connection_state.backend,
            command_context=self._command_context(),
            namespace_context=self.namespace_context,
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
        if process_run_id is not None:
            if args or kwargs:
                raise TypeError(
                    "Pass either an existing process_run_id or "
                    "name/description/template_name/version, not both"
                )
            return ProcessRunBuilder(
                name=None,
                description=None,
                template_name=None,
                backend=self.connection_state.backend,
                namespace_context=self.namespace_context,
                command_context=self._command_context(),
                version=None,
                process_run_id=process_run_id,
                on_existing=on_existing,
            )

        if args:
            if len(args) != 4:
                raise TypeError(
                    "Provide exactly four positional arguments: name, description, "
                    "template_name, version"
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

        return ProcessRunBuilder(
            name=name,
            description=description,
            template_name=template_name,
            namespace_context=self.namespace_context,
            backend=self.connection_state.backend,
            command_context=self._command_context(),
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
    ):
        """Open a builder for a
        :class:`~recap.dsl.resource_builder.ResourceTemplateBuilder`.

        A :class:`~recap.schemas.resource.ResourceTemplateSchema` is the
        blueprint for a :class:`~recap.schemas.resource.ResourceSchema`.
        This method supports two mutually exclusive call patterns:

        **Create or update a template by name** — the most common usage::

            with client.build_resource_template(
                name="Library Plate",
                type_names=["container", "plate", "library_plate"],
            ) as tb:
                tb.add_properties(
                    {"dimensions": [{"name": "rows", "type": "int", "default": 8}]}
                )

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
        if resource_template_id is not None:
            if name is not None or type_names is not None:
                raise TypeError(
                    "Pass either an existing resource_template_id or "
                    "name/type_names, not both"
                )
            return ResourceTemplateBuilder(
                name=None,
                type_names=None,
                version=version,
                backend=self.connection_state.backend,
                command_context=self._command_context(),
                namespace_context=self.namespace_context,
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
            backend=self.connection_state.backend,
            command_context=self._command_context(),
            namespace_context=self.namespace_context,
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
        if resource_id is not None:
            if args or kwargs:
                raise TypeError(
                    "Pass either an existing resource_id or "
                    "name/template_name, not both"
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
                backend=self.connection_state.backend,
                command_context=self._command_context(),
                namespace_context=self.namespace_context,
                resource_id=resource_id,
                on_existing=on_existing,
            )

        resolved_parent = self._resolve_parent(parent, self.namespace_context)
        name, template_name, template_version = self._parse_resource_args(args, kwargs)

        return ResourceBuilder(
            name=name,
            template_name=template_name,
            template_version=template_version,
            backend=self.connection_state.backend,
            command_context=self._command_context(),
            namespace_context=self.namespace_context,
            on_existing=on_existing,
            parent=resolved_parent,
        )

    def _resolve_parent(
        self,
        parent: "ResourceSchema | UUID | None",
        namespace_context: NamespaceContext | None = None,
    ) -> "ResourceSchema | None":
        """Resolve a parent argument to a ResourceSchema (or None)."""
        if parent is None:
            return None
        if isinstance(parent, UUID):
            from recap.dsl.query import QuerySpec

            results = self.connection_state.backend.query(
                ResourceSchema,
                QuerySpec(
                    filters={"id": parent},
                    preloads=["children", "properties"],
                    include_mutable=True,
                ),
                namespace_path=(namespace_context or self._namespace_context).path,
            )
            if not results:
                raise RecapNotFoundError(
                    f"Parent resource with id {parent!r} not found"
                )
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
        return ResourceBuilder.create(
            name=name,
            template_name=template_name,
            template_version=template_version,
            backend=self.connection_state.backend,
            namespace_context=self.namespace_context,
            command_context=self._command_context(),
            parent=parent,
            on_existing=on_existing,
        )

    def copy_resource(
        self,
        source_resource_id: UUID,
        options: ResourceCopyOptions | None = None,
    ) -> ResourceSchema:
        """Copy resource across namespaces and commit or roll back atomically.

        Destination namespace comes from this client's scope. Returns persisted
        full schema and propagates backend validation or authorization errors.
        """
        copy_options = options or ResourceCopyOptions()
        try:
            return self.connection_state.backend._execute(
                CopyResource(
                    source_resource_id=source_resource_id,
                    destination_namespace_path=self.namespace_context.path,
                    options=copy_options,
                ),
                self._command_context(),
            )
        except CommandValidationError as error:
            raise ValueError(str(error)) from error

    def copy_process_run(
        self,
        source_process_run_id: UUID,
        options: ProcessRunCopyOptions | None = None,
    ) -> ProcessRunSchema:
        """Copy process run into current namespace with fresh aggregate identity."""
        if self.connection_state.backend is None:
            raise RuntimeError("Backend not initialized")
        namespace_context = self._resolve_namespace_context()
        return self.connection_state.backend._execute(
            CopyProcessRun(
                source_process_run_id=source_process_run_id,
                destination_namespace_path=namespace_context.path,
                options=options or ProcessRunCopyOptions(),
            ),
            self._command_context(),
        )

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
        from recap.dsl.query import QuerySpec

        schema = ResourceSchema if expand else ResourceRef
        results = self.connection_state.backend.query(
            schema,
            QuerySpec(
                filters={
                    "name": name,
                    "template__name": template_name,
                    "template__version": template_version,
                },
                preloads=("template", "children", "properties") if expand else (),
                load_mode="eager" if expand else None,
                include_mutable=True,
            ),
            namespace_path=self.namespace_context.path,
        )
        if not results:
            raise RecapNotFoundError(f"Resource {name!r} not found")
        if len(results) > 1:
            raise ValueError(
                f"Multiple resources named {name!r} matched the requested template"
            )
        return results[0]

    def create_namespace(
        self, path: str, metadata: dict[str, Any] | None = None, as_current=False
    ) -> NamespaceContext:
        """Create a namespace and make it active for subsequent writes."""
        result = self.connection_state.backend._execute(
            CreateNamespace(path=path, metadata=metadata), self._command_context()
        )
        if as_current:
            self._namespace_context = self._as_namespace_context(result)
        return self._namespace_context

    def get_namespace(self, path: str) -> NamespaceContext:
        """Get existing namespace information"""
        return self._resolve_namespace_context(path)

    def update_namespace(
        self,
        namespace_id: UUID | None = None,
        *,
        expected_revision: int | None = None,
        metadata: Mapping[str, Any] | None = None,
        status: LifecycleStatus | None = None,
    ) -> NamespaceContext:
        """Apply namespace metadata/status update and make result active."""
        if self.connection_state.backend is None:
            raise RuntimeError("Backend not initialized")
        context = self._namespace_context
        if namespace_id is None:
            if context is None:
                raise ValueError("An active namespace context is required")
            namespace_id = context.id
        if expected_revision is None:
            if context is None or context.revision is None:
                raise ValueError("Expected namespace revision is required")
            expected_revision = context.revision

        result = self.connection_state.backend._execute(
            UpdateNamespace(
                namespace_id=namespace_id,
                expected_revision=expected_revision,
                metadata=None if metadata is None else dict(metadata),
                status=status,
            ),
            self._command_context(),
            etag_override=None if context is None else context.etag,
        )
        self._namespace_context = self._as_namespace_context(result)
        return self._namespace_context

    @staticmethod
    def _as_namespace_context(result) -> NamespaceContext:
        if isinstance(result, NamespaceContext):
            return result
        return NamespaceContext.model_validate(result)

    @property
    def namespace_context(self) -> NamespaceContext:
        return self._namespace_context

    def list_namespaces(self) -> list[str]:
        """Return relative names of direct child namespaces."""
        return self.connection_state.backend.list_child_namespaces(self.namespace_path)

    def query_maker(
        self,
        *,
        on_unloaded: str = "warn",
    ):
        """Return a query DSL scoped to this client's namespace."""

        return QueryDSL(
            self.connection_state.backend,
            context=self.namespace_context,
            on_unloaded=on_unloaded,
        )
