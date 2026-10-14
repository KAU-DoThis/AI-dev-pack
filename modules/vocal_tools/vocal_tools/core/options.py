from __future__ import annotations

from typing import Any, Iterable


def parse_key_value_options(items: Iterable[str] | None) -> dict[str, Any]:
    """Parse CLI-style key=value option strings into Python values."""
    options: dict[str, Any] = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError(f"Option must use key=value format: {item!r}")
        key, raw_value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"Option key cannot be empty: {item!r}")
        options[key] = _parse_scalar(raw_value.strip())
    return options


def _parse_scalar(value: str) -> Any:
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered in {"null", "none"}:
        return None

    try:
        if _looks_like_int(value):
            return int(value)
        if _looks_like_float(value):
            return float(value)
    except ValueError:
        return value

    return value


def _looks_like_int(value: str) -> bool:
    if not value:
        return False
    if value[0] in "+-":
        return value[1:].isdigit()
    return value.isdigit()


def _looks_like_float(value: str) -> bool:
    if not value or value.lower() in {"nan", "+nan", "-nan", "inf", "+inf", "-inf"}:
        return False
    return any(char in value for char in (".", "e", "E"))
