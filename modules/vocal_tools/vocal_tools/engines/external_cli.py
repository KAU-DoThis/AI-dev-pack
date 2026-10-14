from __future__ import annotations

import string
from pathlib import Path
from typing import Any, Mapping, Sequence

from vocal_tools.core.errors import SeparationError, UnsupportedEngineError


SUPPORTED_ADAPTER_BACKENDS = {"auto", "external_cli", "python"}


def resolve_backend(value: Any, *, engine_name: str) -> str:
    backend = str(value or "auto").strip().lower()
    if backend not in SUPPORTED_ADAPTER_BACKENDS:
        raise UnsupportedEngineError(
            f"{engine_name} backend {backend!r} is not supported. "
            "Use 'auto', 'external_cli', or 'python'."
        )
    return backend


def runtime_info(requested_device: Any) -> dict[str, Any]:
    requested = str(requested_device or "auto").strip().lower()
    if requested not in {"auto", "cuda", "cpu"}:
        raise SeparationError(
            f"Unsupported device {requested_device!r}. Use 'auto', 'cuda', or 'cpu'."
        )

    torch_version = None
    cuda_available = False
    cuda_device_name = None
    try:
        import torch

        torch_version = getattr(torch, "__version__", None)
        cuda_available = bool(torch.cuda.is_available())
        if cuda_available:
            try:
                cuda_device_name = torch.cuda.get_device_name(0)
            except Exception:
                cuda_device_name = None
    except ImportError:
        torch_version = None

    if requested == "auto":
        selected = "cuda" if cuda_available else "cpu"
    else:
        selected = requested

    if selected == "cuda" and not cuda_available:
        raise SeparationError("CUDA was requested, but torch.cuda.is_available() is False.")

    return {
        "requested_device": requested,
        "selected_device": selected,
        "cuda_available": cuda_available,
        "cuda_device_name": cuda_device_name,
        "torch_version": torch_version,
    }


def render_command_template(
    command_template: Any,
    placeholders: Mapping[str, Any],
    *,
    engine_name: str,
) -> list[str]:
    if command_template in (None, ""):
        raise UnsupportedEngineError(
            f"{engine_name} external_cli backend requires options['command'] as a list of arguments."
        )
    if not isinstance(command_template, Sequence) or isinstance(command_template, (str, bytes)):
        raise UnsupportedEngineError(
            f"{engine_name} external_cli command must be a list of arguments, "
            f"got {type(command_template).__name__}."
        )

    values = {
        key: str(value)
        for key, value in placeholders.items()
        if value not in (None, "")
    }
    rendered: list[str] = []
    for part in command_template:
        if not isinstance(part, (str, Path)):
            raise UnsupportedEngineError(
                f"{engine_name} external_cli command arguments must be strings, "
                f"got {type(part).__name__}."
            )
        text = str(part)
        missing = _missing_placeholders(text, values)
        if missing:
            raise SeparationError(
                f"{engine_name} command template references unavailable placeholder(s): "
                f"{', '.join(sorted(missing))}. Provide the value in options/defaults or remove it."
            )
        try:
            rendered.append(text.format(**values))
        except (IndexError, KeyError, ValueError) as exc:
            raise SeparationError(f"Invalid {engine_name} command template argument {text!r}: {exc}") from exc
    if not rendered:
        raise UnsupportedEngineError(
            f"{engine_name} external_cli backend requires a non-empty options['command'] list."
        )
    return rendered


def tail(text: str, limit: int = 1200) -> str:
    if len(text) <= limit:
        return text
    return text[-limit:]


def _missing_placeholders(text: str, values: Mapping[str, str]) -> set[str]:
    missing: set[str] = set()
    formatter = string.Formatter()
    try:
        parsed = formatter.parse(text)
        for _, field_name, _, _ in parsed:
            if field_name is None:
                continue
            name = field_name.split(".", 1)[0].split("[", 1)[0]
            if name not in values:
                missing.add(name)
    except ValueError as exc:
        raise SeparationError(f"Invalid command template argument {text!r}: {exc}") from exc
    return missing
