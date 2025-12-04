class DuplicateResourceError(Exception):
    def __init__(self, resource_name: str, campaign_name: str):
        message = f"Resource '{resource_name}' already exists in the campaign '{campaign_name}'"
        super().__init__(message)
