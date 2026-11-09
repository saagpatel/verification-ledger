"""Narrow untrusted JSON from object, without leaking SDK/decoder Any types."""

from typing import cast


def mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    entries = cast(dict[object, object], value)
    result: dict[str, object] = {}
    for key, item in entries.items():
        if not isinstance(key, str):
            raise ValueError("expected string object keys")
        result[key] = item
    return result


def sequence(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError("expected a JSON array")
    return cast(list[object], value)


def string(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("expected a string")
    return value
