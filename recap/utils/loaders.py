from sqlalchemy.orm import joinedload, selectinload


def chain_load(*attrs, joined=()):
    """Build select-in chains with explicit joined edges for scalar dependencies."""
    loader = (
        joinedload(attrs[0])
        if any(attrs[0] is edge for edge in joined)
        else selectinload(attrs[0])
    )
    for attr in attrs[1:]:
        loader = (
            loader.joinedload(attr)
            if any(attr is edge for edge in joined)
            else loader.selectinload(attr)
        )
    return loader
