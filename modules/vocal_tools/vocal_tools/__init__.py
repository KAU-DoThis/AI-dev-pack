from __future__ import annotations

from importlib import import_module
from typing import Any


# Catalog
_CATALOG_EXPORTS = {
    "DEFAULT_UVR_SOURCES": ".catalog",
    "load_uvr_model_entries": ".catalog",
    "ModelCatalog": ".catalog",
    "ModelStore": ".catalog",
    "ResolvedModel": ".catalog",
    "resolve_model": ".catalog",
    "RVCVoiceCatalog": ".catalog",
    "UVRModelImporter": ".catalog",
}

# Core Types
_CORE_TYPE_EXPORTS = {
    "OptionSpec": ".core",
    "SeparationModelSpec": ".core",
    "SeparationRequest": ".core",
    "SeparationResult": ".core",
    "ProgressEvent": ".core",
}

# Separation
_SEPARATION_EXPORTS = {
    "Separator": ".separator",
}

# RVC
_RVC_EXPORTS = {
    "RVCConverter": ".rvc",
    "RVCConversionOptions": ".rvc",
    "RVCVoiceModelPaths": ".rvc",
}

# Text to speech
_TTS_EXPORTS = {
    "TTS": ".tts",
    "TTSResult": ".tts",
    "EdgeTTS": ".tts",
    "Qwen3TTS": ".tts",
    "DEFAULT_QWEN3_TTS_MODEL": ".tts",
    "TTSError": ".core",
    "TTSDependencyError": ".core",
}

# Mixing
_MIXING_EXPORTS = {
    "AudioMixOptions": ".audio",
    "AudioMixResult": ".audio",
    "mix_vocal_with_instrumental": ".audio",
}

# Pipelines
_PIPELINE_EXPORTS = {
    "CoverPipelineOptions": ".pipelines",
    "CoverPipelineResult": ".pipelines",
    "run_cover_pipeline": ".pipelines",
}

# Task Helpers
_TASK_HELPER_EXPORTS = {
    "run_tts_payload": ".tasks",
    "run_tts_payload_async": ".tasks",
    "run_audio_separate_payload": ".tasks",
    "run_audio_rvc_convert_payload": ".tasks",
    "run_audio_cover_pipeline_payload": ".tasks",
    "list_audio_models_payload": ".tasks",
    "list_rvc_voice_models_payload": ".tasks",
}

# Validation
_VALIDATION_EXPORTS = {
    "validate_tts_payload": ".tasks",
    "validate_audio_separate_payload": ".tasks",
    "validate_audio_rvc_convert_payload": ".tasks",
    "validate_audio_cover_pipeline_payload": ".tasks",
}

# Compatibility exports from the older direct MDX API.
_LEGACY_MDX_EXPORTS = {
    "MDXModelNames": ".mdx",
    "MDXSeparationResult": ".mdx",
    "MDXSeparator": ".mdx",
}

_EXPORTS = {
    **_CATALOG_EXPORTS,
    **_CORE_TYPE_EXPORTS,
    **_SEPARATION_EXPORTS,
    **_RVC_EXPORTS,
    **_TTS_EXPORTS,
    **_MIXING_EXPORTS,
    **_PIPELINE_EXPORTS,
    **_TASK_HELPER_EXPORTS,
    **_VALIDATION_EXPORTS,
    **_LEGACY_MDX_EXPORTS,
}

__all__ = [
    *_CATALOG_EXPORTS,
    *_CORE_TYPE_EXPORTS,
    *_SEPARATION_EXPORTS,
    *_RVC_EXPORTS,
    *_TTS_EXPORTS,
    *_MIXING_EXPORTS,
    *_PIPELINE_EXPORTS,
    *_TASK_HELPER_EXPORTS,
    *_VALIDATION_EXPORTS,
    *_LEGACY_MDX_EXPORTS,
]


def __getattr__(name: str) -> Any:
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module 'vocal_tools' has no attribute {name!r}")

    module = import_module(module_name, __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value
