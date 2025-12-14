import json
from datetime import datetime
from typing import Any

from slugify import slugify
from sqlalchemy.ext.mutable import MutableList


def _parse_array_like(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list | tuple):
        return list(value)
    if isinstance(value, str):
        s = value.strip()
        try:
            loaded_json = json.loads(s)
            if isinstance(loaded_json, list):
                return loaded_json
            return [loaded_json]
        except Exception:
            if "," in s:
                return [part.strip() for part in s.split(",")]
            return [s]
    return [value]


TRUE_STRS = {"true", "t", "yes", "1"}
FALSE_STRS = {"false", "f", "no", "0"}


def _to_bool(v):
    if isinstance(v, str):
        s = v.strip().lower()
        if s in TRUE_STRS:
            return True
        if s in FALSE_STRS:
            return False
    return bool(v)


ISO_FORMATS = (
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
)


def _to_datetime(value):
    if value is None:
        return None  # store NULL in the column

    if isinstance(value, datetime):
        return value  # already a datetime

    if isinstance(value, str):
        text = value.strip()
        if not value or value.lower() == "now":
            return datetime.now()
        for fmt in ISO_FORMATS:
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
        raise ValueError(f"Could not parse datetime string {value!r}")
    raise TypeError("datetime_value accepts None, datetime, or ISO8601 string")


def _to_array(v):
    items = _parse_array_like(v)  # your existing helper
    return MutableList(items)


CONVERTERS = {
    "int": int,
    "float": float,
    "bool": _to_bool,
    "str": str,
    "datetime": _to_datetime,
    "array": _to_array,
    "enum": str,
}


def to_json_compatible(value_type: str, value: Any) -> Any:
    """
    Coerce a raw value to the declared type and then make it JSON-serializable.
    """
    if value is None:
        return None
    try:
        converter = CONVERTERS[value_type]
    except KeyError:
        raise ValueError(f"Unsupported property type: {value_type}") from None

    coerced = converter(value)
    if isinstance(coerced, datetime):
        return coerced.isoformat()
    if isinstance(coerced, MutableList):
        return list(coerced)
    return coerced


def from_json_value(value_type: str, value: Any) -> Any:
    """
    Convert a stored JSON value back to the declared Python type.
    """
    if value is None:
        return None
    try:
        converter = CONVERTERS[value_type]
    except KeyError:
        raise ValueError(f"Unsupported property type: {value_type}") from None

    coerced = converter(value)
    if isinstance(coerced, MutableList):
        return list(coerced)
    return coerced


def generate_uppercase_alphabets(n: int) -> list:
    if n < 1:
        raise ValueError("The number must be a positive integer.")

    alphabets = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

    def get_letter(num):
        result = []
        while num > 0:
            num, remainder = divmod(num - 1, 26)
            result.append(alphabets[remainder])
        return "".join(reversed(result))

    return [get_letter(i) for i in range(1, n + 1)]


def make_slug(value: str) -> str:
    """
    Generate a slug that is always a valid Python identifier.
    """
    regex_pattern = r"[^a-z0-9_]+"  # allow only lowercase letters, digits, underscores
    slug = slugify(
        value,
        lowercase=True,
        separator="_",
        regex_pattern=regex_pattern,
    )
    # Ensure it doesn't start with a digit (prepend underscore if so)
    if slug and slug[0].isdigit():
        slug = f"_{slug}"
    return slug
