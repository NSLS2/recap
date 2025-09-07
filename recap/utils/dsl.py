from sqlalchemy import select
from sqlalchemy.orm import Session


def _get_or_create(session: Session, model, where: dict, defaults: dict | None = None):
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
    return obj, True


class AliasMixin:
    """
    This is a pydantic model mixin that allows a user to access a
    field via its name in the database. For e.g. if a paramter group is
    called "Sample Temperature", its pydantic field is converted to sample_temperature
    But if the user wants to use the original string. This can be used by:
    parameter.get("Sample Temperature") or
    parameter.get("sample_temperature") if they want to use the slugified string

    """

    def get(self, alias: str):
        for name, field in self.__class__.model_fields.items():
            if alias in (field.alias, name):
                return getattr(self, name)
        raise KeyError(f"No field with alias '{alias}'")

    def set(self, alias: str, value):
        for name, field in self.__class__.model_fields.items():
            if alias in (field.alias, name):
                setattr(self, name, value)
                return
        raise KeyError(f"No field with alias '{alias}'")
