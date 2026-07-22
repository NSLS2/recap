class DuplicateResourceError(Exception):
    def __init__(
        self,
        resource_name: str,
        campaign_name: str,
        process_run_name: str,
        step_name: str | None,
    ):
        message = f"Resource '{resource_name}' already exists in process run {process_run_name} under campaign '{campaign_name}'"
        if step_name:
            message = f"Resource '{resource_name}' already exists in process run '{process_run_name}', step '{step_name}' under campaign '{campaign_name}'"
        super().__init__(message)


class ExistingEntityWarning(UserWarning):
    """Base warning for idempotent builder reuse of existing entities."""


class ExistingResourceTemplateWarning(ExistingEntityWarning):
    pass


class ExistingProcessTemplateWarning(ExistingEntityWarning):
    pass


class ExistingResourceWarning(ExistingEntityWarning):
    pass


class ExistingProcessRunWarning(ExistingEntityWarning):
    pass


class ExistingEntityError(Exception):
    """Base error for builders configured with on_existing='raise'."""


class ExistingResourceTemplateError(ExistingEntityError):
    pass


class ExistingProcessTemplateError(ExistingEntityError):
    pass


class ExistingResourceError(ExistingEntityError):
    pass


class ExistingProcessRunError(ExistingEntityError):
    pass


class UnloadedFieldWarning(UserWarning):
    """Raised when accessing relationship fields that were not preloaded."""


class UnloadedFieldError(RuntimeError):
    """Raised when unloaded relationship access policy is set to 'raise'."""


class RecapConnectionError(Exception):
    """Raised when RecapClient cannot connect to a recap server."""

    def __init__(self, url: str, status_code: int | None = None, message: str | None = None):
        self.url = url
        self.status_code = status_code
        detail = f" (HTTP {status_code})" if status_code else ""
        extra = f": {message}" if message else ""
        super().__init__(f"Cannot connect to recap server at {url}{detail}{extra}")
