from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from recap.authentication.models import ProviderIdentity
from recap.authorization.scopes import Scope
from recap.utils.namespace import canonicalize_namespace_path


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(
    loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ValueError(f"Duplicate YAML key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


class _SourceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RoleConfig(_SourceModel):
    scopes: tuple[Scope, ...]

    @field_validator("scopes")
    @classmethod
    def validate_scopes(cls, scopes: tuple[Scope, ...]) -> tuple[Scope, ...]:
        if not scopes:
            raise ValueError("Role scopes must not be empty")
        if len(scopes) != len(set(scopes)):
            raise ValueError("Role scopes must not contain duplicates")
        return scopes


class IdentityGroupConfig(_SourceModel):
    identities: tuple[ProviderIdentity, ...]

    @model_validator(mode="before")
    @classmethod
    def validate_identity_input(cls, value: Any) -> Any:
        if not isinstance(value, Mapping):
            return value
        identities = value.get("identities")
        if not isinstance(identities, (list, tuple)):
            return value
        for identity in identities:
            if not isinstance(identity, Mapping):
                continue
            extra = set(identity) - {"provider", "subject"}
            if extra:
                raise ValueError(f"Unknown identity fields: {sorted(extra)!r}")
            for field in ("provider", "subject"):
                if not isinstance(identity.get(field), str) or not identity[field]:
                    raise ValueError(f"Identity {field} must be a non-empty string")
        return value

    @field_validator("identities")
    @classmethod
    def validate_identities(
        cls, identities: tuple[ProviderIdentity, ...]
    ) -> tuple[ProviderIdentity, ...]:
        if not identities:
            raise ValueError("Identity group must not be empty")
        if len(identities) != len(set(identities)):
            raise ValueError("Duplicate identity in group")
        return identities


class NamespaceGroupBinding(_SourceModel):
    name: str
    role: str

    @field_validator("name", "role")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not value:
            raise ValueError("Binding name and role must not be empty")
        return value


class NamespaceBindingConfig(_SourceModel):
    groups: tuple[NamespaceGroupBinding, ...]

    @field_validator("groups")
    @classmethod
    def validate_groups(
        cls, groups: tuple[NamespaceGroupBinding, ...]
    ) -> tuple[NamespaceGroupBinding, ...]:
        if not groups:
            raise ValueError("Namespace groups must not be empty")
        bindings = {(binding.name, binding.role) for binding in groups}
        if len(bindings) != len(groups):
            raise ValueError("Duplicate binding")
        return groups


class AuthorizationSourceConfig(_SourceModel):
    source_revision: str
    roles: dict[str, RoleConfig]
    groups: dict[str, IdentityGroupConfig]
    namespaces: dict[str, NamespaceBindingConfig]

    @field_validator("source_revision")
    @classmethod
    def validate_source_revision(cls, value: str) -> str:
        if not value:
            raise ValueError("source_revision must not be empty")
        return value

    @field_validator("roles", "groups", "namespaces")
    @classmethod
    def validate_mapping_names(cls, value: dict[str, Any]) -> dict[str, Any]:
        if any(not isinstance(name, str) or not name for name in value):
            raise ValueError("Configuration names must be non-empty strings")
        return value

    @field_validator("namespaces")
    @classmethod
    def validate_namespace_paths(
        cls, namespaces: dict[str, NamespaceBindingConfig]
    ) -> dict[str, NamespaceBindingConfig]:
        for path in namespaces:
            canonicalize_namespace_path(path)
        return namespaces

    @model_validator(mode="after")
    def validate_references_and_duplicates(self) -> "AuthorizationSourceConfig":
        seen_identities: set[ProviderIdentity] = set()
        for group in self.groups.values():
            for identity in group.identities:
                if identity in seen_identities:
                    raise ValueError(
                        f"Duplicate identity: {identity.provider}:{identity.subject}"
                    )
                seen_identities.add(identity)

        for namespace in self.namespaces.values():
            for binding in namespace.groups:
                if binding.name not in self.groups:
                    raise ValueError(f"Unknown group: {binding.name}")
                if binding.role not in self.roles:
                    raise ValueError(f"Unknown role: {binding.role}")
        return self


def load_authorization_source(path: str | Path) -> AuthorizationSourceConfig:
    with Path(path).open(encoding="utf-8") as stream:
        source = yaml.load(stream, Loader=_UniqueKeyLoader)
    return AuthorizationSourceConfig.model_validate(source)
