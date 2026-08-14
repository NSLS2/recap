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


class RecapError(Exception):
    code = "recap_error"

    def __init__(
        self,
        message: str,
        *,
        url: str | None = None,
        status_code: int | None = None,
        request_id: str | None = None,
    ) -> None:
        self.message = message
        self.url = url
        self.status_code = status_code
        self.request_id = request_id
        details = [message]
        if status_code is not None:
            details.append(f"HTTP {status_code}")
        if request_id is not None:
            details.append(f"request_id={request_id}")
        super().__init__("; ".join(details))


class RecapConnectionError(RecapError):
    code = "connection_error"


class RecapProtocolError(RecapError):
    code = "protocol_error"


class RecapRequestError(RecapError):
    code = "request_error"


class RecapAuthenticationError(RecapRequestError):
    code = "authentication_required"


class RecapPermissionDeniedError(RecapRequestError):
    code = "permission_denied"


class RecapNotFoundError(RecapRequestError):
    code = "not_found"


class RecapValidationError(RecapRequestError):
    code = "validation_error"


class RecapConflictError(RecapRequestError):
    code = "conflict"


class RecapServiceUnavailableError(RecapRequestError):
    code = "service_unavailable"


class RecapInternalError(RecapRequestError):
    code = "internal_error"


ERROR_TYPES = {
    "authentication_required": RecapAuthenticationError,
    "permission_denied": RecapPermissionDeniedError,
    "not_found": RecapNotFoundError,
    "validation_error": RecapValidationError,
    "conflict": RecapConflictError,
    "service_unavailable": RecapServiceUnavailableError,
    "internal_error": RecapInternalError,
    "request_error": RecapRequestError,
}


def error_from_code(
    code: str,
    message: str,
    *,
    url: str | None = None,
    status_code: int | None = None,
    request_id: str | None = None,
) -> RecapRequestError:
    error_type = ERROR_TYPES.get(code, RecapRequestError)
    error = error_type(
        message,
        url=url,
        status_code=status_code,
        request_id=request_id,
    )
    return error
