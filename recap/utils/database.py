from sqlalchemy.exc import MultipleResultsFound, NoResultFound


def _load_single(session, statement, *, label: str):
    try:
        return session.scalars(statement).one()
    except NoResultFound as exc:
        raise LookupError(f"{label}: no rows matched the query") from exc
    except MultipleResultsFound as exc:
        raise LookupError(f"{label}: expected exactly one row, got multiple") from exc
