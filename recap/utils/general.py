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


def _to_datetime(v):
    if isinstance(v, datetime):
        return v
    if isinstance(v, str):
        # matches your original strict format
        return datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ")
    if v is None:
        return datetime.now()
    raise ValueError("datetime_value accepts: ISO8601 string or datetime object")


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
}

TARGET_FIELD = {
    "int": "int_value",
    "float": "float_value",
    "bool": "bool_value",
    "str": "str_value",
    "datetime": "datetime_value",
    "array": "array_value",
}


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
