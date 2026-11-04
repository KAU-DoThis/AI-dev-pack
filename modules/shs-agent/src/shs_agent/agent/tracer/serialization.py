"""Loss-aware data capture. Never serialize arbitrary objects through repr()."""
from __future__ import annotations

import base64
import math
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any

from shs_agent.llm.observation import ObservationUnavailable


AUTH_KEYS = frozenset({"api_key", "apikey", "authorization", "proxy_authorization",
                       "access_token", "refresh_token", "cookie", "set_cookie", "x_api_key"})
CONNECTION_CONTAINERS = frozenset({"options", "extra", "request", "kwargs", "headers", "extra_headers"})


def capture(value: Any, *, credentials: bool = False) -> tuple[Any, tuple[dict[str, str], ...]]:
    """Copy JSON-like values and declared dataclasses without invoking SDK serializers.

    Credential exclusion follows only known connection/options containers.
    Other subtrees (messages, schemas, response formats, metadata, extra_body)
    are body data, not subject to recursive text or key masking.
    Unsupported values retain an explicit marker and an issue at the exact path.
    """
    issues: list[dict[str, str]] = []
    ancestors: set[int] = set()

    def missing(item: Any, path: str, reason: str) -> dict[str, str]:
        type_name = f"{type(item).__module__}.{type(item).__qualname__}"
        issues.append({"path": path, "reason": reason, "category": "unavailable", "python_type": type_name})
        return {"$unavailable": reason, "python_type": type_name}

    def walk(item: Any, path: str, redact: bool) -> Any:
        if item is None or type(item) in (bool, int, str):
            return item
        if isinstance(item, Enum):
            return visit(item.value, path, redact)
        if type(item) is float:
            return item if math.isfinite(item) else missing(item, path, "non_finite_number")
        if isinstance(item, bytes):
            return {"$encoding": "base64", "python_type": "bytes",
                    "data": base64.b64encode(item).decode("ascii")}
        if isinstance(item, (datetime, date)):
            return {"$encoding": "iso8601", "python_type": type(item).__name__, "data": item.isoformat()}
        if isinstance(item, ObservationUnavailable):
            return missing(item, path, item.reason)
        if id(item) in ancestors:
            return missing(item, path, "cyclic_reference")
        item_id = id(item)
        ancestors.add(item_id)
        try:
            if is_dataclass(item) and not isinstance(item, type):
                item = {entry.name: getattr(item, entry.name) for entry in fields(item)}
            if isinstance(item, dict):
                if not all(isinstance(key, str) for key in item):
                    # Preserve non-string keys as explicit entries; don't coerce 1 to "1".
                    entries = []
                    for index, (key, data) in enumerate(item.items()):
                        if redact and isinstance(key, str) and key.lower().replace("-", "_") in AUTH_KEYS:
                            issues.append({"path": f"{path}.{key}", "reason": "connection_credentials",
                                           "category": "excluded"})
                            continue
                        entries.append([visit(key, f"{path}.keys[{index}]", False),
                                        visit(data, f"{path}.values[{index}]",
                                              redact and isinstance(key, str) and key in CONNECTION_CONTAINERS)])
                    return {"$encoding": "mapping_entries", "entries": entries}
                result = {}
                for key, data in item.items():
                    normalized = key.lower().replace("-", "_")
                    entry_path = f"{path}.{key}"
                    if redact and normalized in AUTH_KEYS:
                        issues.append({"path": entry_path, "reason": "connection_credentials",
                                       "category": "excluded"})
                        continue
                    result[key] = visit(data, entry_path, redact and key in CONNECTION_CONTAINERS)
                return result
            if isinstance(item, (list, tuple)):
                return [visit(data, f"{path}[{index}]", redact) for index, data in enumerate(item)]
            return missing(item, path, "unsupported_type")
        finally:
            ancestors.discard(item_id)

    def visit(item: Any, path: str, redact: bool) -> Any:
        try:
            return walk(item, path, redact)
        except Exception as exc:
            return missing(item, path, f"capture_error:{type(exc).__name__}")

    return visit(value, "$", credentials), tuple(issues)
