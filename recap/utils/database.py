from sqlalchemy import select
from sqlalchemy.exc import MultipleResultsFound, NoResultFound
from sqlalchemy.orm import Session


def get_or_create(session: Session, model, where: dict, defaults: dict | None = None):
    stmt = select(model).filter_by(**where)
    obj = session.execute(stmt).scalar_one_or_none()
    if obj:
        # update with defaults (no destructive changes)
        if defaults:
            for k, v in defaults.items():
                if getattr(obj, k, None) is None:
                    setattr(obj, k, v)
        return obj, False
    obj = model(**{**where, **(defaults or {})})
    session.add(obj)
    session.flush()
    return obj, True


def load_single(session, statement, *, label: str):
    try:
        return session.scalars(statement).one()
    except NoResultFound as exc:
        compiled = statement.compile()
        raise LookupError(
            f"{label}: no rows matched the query: {compiled} {compiled.params}"
        ) from exc
    except MultipleResultsFound as exc:
        raise LookupError(f"{label}: expected exactly one row, got multiple") from exc
