from recap.authentication.models import ActorKind, ProviderIdentity, RequestActor
from recap.authorization.scopes import Scope


def single_user_actor(
    *, credential_fingerprint: str, provider: str = "single-user"
) -> RequestActor:
    return RequestActor(
        actor_id="single-user",
        kind=ActorKind.USER,
        identities=(
            ProviderIdentity(provider=provider, subject="single-user"),
        ),
        credential_scopes=frozenset(Scope),
        namespace_restrictions=None,
        credential_fingerprint=credential_fingerprint,
    )


__all__ = ["single_user_actor"]
