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
