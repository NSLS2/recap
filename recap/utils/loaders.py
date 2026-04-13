from sqlalchemy.orm import selectinload


def chain_load(*attrs):
    """Build nested selectinload options from a relationship chain."""
    loader = selectinload(attrs[0])
    for attr in attrs[1:]:
        loader = loader.selectinload(attr)
    return loader
