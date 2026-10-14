from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable


def validate_audio_separate_payload(payload: dict) -> dict:
    data = _payload_dict(payload)
    normalized = dict(data)
    _normalize_required_path(normalized, "models_dir")
    _normalize_required_path(normalized, "input_audio")
    _normalize_required_path(normalized, "output_dir")
    _require_string(normalized, "model_id")
    _normalize_optional_path(normalized, "builtin_manifest_path")
    _normalize_optional_path(normalized, "user_manifest_path")
    _normalize_optional_path(normalized, "remote_manifest_cache_path")
    _require_dict(normalized, "options", default={})
    return normalized


def validate_audio_rvc_convert_payload(payload: dict) -> dict:
    data = _payload_dict(payload)
    normalized = dict(data)
    _normalize_required_path(normalized, "models_dir")
    _normalize_required_path(normalized, "input_audio")
    _normalize_required_path(normalized, "output_audio")
    _require_string(normalized, "voice_model")
    _normalize_optional_path(normalized, "hubert_path")
    _normalize_optional_path(normalized, "rmvpe_path")
    _require_dict(normalized, "options", default={})
    return normalized


def validate_audio_cover_pipeline_payload(payload: dict) -> dict:
    data = _payload_dict(payload)
    normalized = dict(data)
    _normalize_required_path(normalized, "models_dir")
    _normalize_required_path(normalized, "input_audio")
    _normalize_required_path(normalized, "output_dir")
    _require_dict(normalized, "mdx_options", default={})
    _require_dict(normalized, "rvc_options", default={})
    _require_dict(normalized, "mix_options", default={})
    _require_dict(normalized, "dereverb_options", default={})
    _require_dict(normalized, "karaoke_options", default={})
    for key in (
        "mdx_model_id",
        "voice_model",
        "vocal_stem",
        "instrumental_stem",
        "rvc_output_name",
        "mix_output_name",
        "dereverb_model_id",
        "karaoke_model_id",
    ):
        if key in normalized and normalized[key] is not None:
            normalized[key] = str(normalized[key])
    for key in ("keep_intermediate", "use_dereverb", "use_karaoke_split"):
        if key in normalized:
            normalized[key] = bool(normalized[key])
    return normalized


def validate_list_audio_models_payload(payload: dict) -> dict:
    data = _payload_dict(payload)
    normalized = dict(data)
    _normalize_required_path(normalized, "models_dir")
    _normalize_optional_path(normalized, "separation_models_dir")
    _normalize_optional_path(normalized, "builtin_manifest_path")
    _normalize_optional_path(normalized, "user_manifest_path")
    _normalize_optional_path(normalized, "remote_manifest_cache_path")
    normalized["include_separation_models"] = bool(normalized.get("include_separation_models", True))
    normalized["include_voice_models"] = bool(normalized.get("include_voice_models", True))
    normalized["include_unsupported"] = bool(normalized.get("include_unsupported", False))
    if normalized.get("source") in (None, ""):
        normalized["source"] = None
    else:
        normalized["source"] = str(normalized["source"])
    return normalized


def _payload_dict(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError(f"Payload must be a dict, got {type(payload).__name__}.")
    return payload


def _normalize_required_path(payload: dict, key: str) -> None:
    value = payload.get(key)
    if value in (None, ""):
        raise ValueError(f"Missing required payload field: {key}")
    payload[key] = _path_string(value, key)


def _normalize_optional_path(payload: dict, key: str) -> None:
    value = payload.get(key)
    if value in (None, ""):
        if key in payload:
            payload[key] = None
        return
    payload[key] = _path_string(value, key)


def _path_string(value: Any, key: str) -> str:
    if isinstance(value, (str, Path)):
        return str(value)
    raise ValueError(f"Payload field {key!r} must be a string or Path, got {type(value).__name__}.")


def _require_string(payload: dict, key: str) -> None:
    value = payload.get(key)
    if value in (None, ""):
        raise ValueError(f"Missing required payload field: {key}")
    payload[key] = str(value)


def _require_dict(payload: dict, key: str, default: dict | None = None) -> None:
    value = payload.get(key, default)
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise ValueError(f"Payload field {key!r} must be a dict, got {type(value).__name__}.")
    payload[key] = dict(value)
