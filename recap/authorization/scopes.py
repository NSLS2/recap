from enum import Enum

from pydantic import BaseModel, ConfigDict


class Scope(str, Enum):
    NAMESPACE_READ = "namespace:read"
    NAMESPACE_WRITE = "namespace:write"
    PROCESS_TEMPLATE_READ = "process-template:read"
    PROCESS_TEMPLATE_WRITE = "process-template:write"
    RESOURCE_TEMPLATE_READ = "resource-template:read"
    RESOURCE_TEMPLATE_WRITE = "resource-template:write"
    RESOURCE_READ = "resource:read"
    RESOURCE_WRITE = "resource:write"
    PROCESS_RUN_READ = "process-run:read"
    PROCESS_RUN_WRITE = "process-run:write"


class ScopeRegistry(BaseModel):
    model_config = ConfigDict(frozen=True)

    scopes: frozenset[Scope]

    def allows(self, granted: frozenset[Scope], required: Scope) -> bool:
        return required in granted


DEFAULT_SCOPE_REGISTRY = ScopeRegistry(scopes=frozenset(Scope))
