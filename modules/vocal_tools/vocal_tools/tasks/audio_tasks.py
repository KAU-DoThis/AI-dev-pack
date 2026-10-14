from __future__ import annotations

from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from vocal_tools.tasks.validation import (
    validate_audio_cover_pipeline_payload,
    validate_audio_rvc_convert_payload,
    validate_audio_separate_payload,
    validate_list_audio_models_payload,
)


ProgressCallback = Optional[Callable[[Dict[str, Any]], None]]


def run_audio_separate_payload(payload: dict, progress_callback: ProgressCallback = None) -> dict:
    payload = validate_audio_separate_payload(payload)
    models_dir = Path(payload["models_dir"])
    input_audio = Path(payload["input_audio"])
    output_dir = Path(payload["output_dir"])
    model_id = str(payload["model_id"])

    _emit(progress_callback, "task_start", task="audio_separate", model_id=model_id)
    from vocal_tools.catalog import ModelCatalog
    from vocal_tools.separator import Separator

    catalog = ModelCatalog(
        model_dir=models_dir,
        builtin_manifest_path=payload.get("builtin_manifest_path"),
        user_manifest_path=payload.get("user_manifest_path"),
        remote_manifest_url=payload.get("remote_manifest_url"),
        remote_manifest_cache_path=payload.get("remote_manifest_cache_path"),
    )
    separator = Separator(catalog=catalog)
    result = separator.separate(
        input_audio=input_audio,
        output_dir=output_dir,
        model_id=model_id,
        options=dict(payload.get("options") or {}),
        progress_callback=_progress_relay(progress_callback, "separate_progress"),
    )
    data = result.to_dict()
    _emit(progress_callback, "task_complete", task="audio_separate", result=data)
    return data


def run_audio_rvc_convert_payload(payload: dict, progress_callback: ProgressCallback = None) -> dict:
    payload = validate_audio_rvc_convert_payload(payload)
    models_dir = Path(payload["models_dir"])
    input_audio = Path(payload["input_audio"])
    output_audio = Path(payload["output_audio"])
    voice_model = payload["voice_model"]

    _emit(progress_callback, "task_start", task="audio_rvc_convert", voice_model=str(voice_model))
    from vocal_tools.rvc import RVCConversionOptions, RVCConverter

    converter = RVCConverter(
        models_dir=models_dir,
        hubert_path=payload.get("hubert_path"),
        rmvpe_path=payload.get("rmvpe_path"),
        device=payload.get("device", "auto"),
        is_half=payload.get("is_half", "auto"),
    )
    options = RVCConversionOptions(**dict(payload.get("options") or {}))
    converted = converter.convert(
        input_audio=input_audio,
        output_audio=output_audio,
        voice_model=voice_model,
        options=options,
    )
    converted = Path(converted)
    data = {
        "input_audio": str(input_audio),
        "output_audio": str(converted),
        "voice_model": str(voice_model),
        "exists": converted.exists(),
        "size": converted.stat().st_size if converted.exists() else None,
        "options_used": _to_jsonable(asdict(options)),
    }
    _emit(progress_callback, "task_complete", task="audio_rvc_convert", result=data)
    return data


def run_audio_cover_pipeline_payload(payload: dict, progress_callback: ProgressCallback = None) -> dict:
    payload = validate_audio_cover_pipeline_payload(payload)
    models_dir = Path(payload["models_dir"])
    input_audio = Path(payload["input_audio"])
    output_dir = Path(payload["output_dir"])

    _emit(progress_callback, "task_start", task="audio_cover_pipeline")
    from vocal_tools.audio import AudioMixOptions
    from vocal_tools.pipelines import CoverPipelineOptions, run_cover_pipeline

    options = CoverPipelineOptions(
        mdx_model_id=payload.get("mdx_model_id", "kim_vocal_2"),
        voice_model=payload.get("voice_model", "iut"),
        mdx_options=dict(payload.get("mdx_options") or {}),
        rvc_options=dict(payload.get("rvc_options") or {}),
        mix_options=AudioMixOptions(**dict(payload.get("mix_options") or {})),
        vocal_stem=payload.get("vocal_stem", "vocals"),
        instrumental_stem=payload.get("instrumental_stem", "instrumental"),
        rvc_output_name=payload.get("rvc_output_name"),
        mix_output_name=payload.get("mix_output_name", "cover_mix.wav"),
        keep_intermediate=bool(payload.get("keep_intermediate", True)),
        use_dereverb=bool(payload.get("use_dereverb", False)),
        dereverb_model_id=payload.get("dereverb_model_id", "reverb_hq_by_foxjoy"),
        dereverb_options=dict(payload.get("dereverb_options") or {}),
        use_karaoke_split=bool(payload.get("use_karaoke_split", False)),
        karaoke_model_id=payload.get("karaoke_model_id", "uvr_mdxnet_kara_2"),
        karaoke_options=dict(payload.get("karaoke_options") or {}),
    )
    result = run_cover_pipeline(
        input_audio=input_audio,
        output_dir=output_dir,
        models_dir=models_dir,
        options=options,
        progress_callback=_progress_relay(progress_callback, "pipeline_progress"),
    )
    data = result.to_dict()
    _emit(progress_callback, "task_complete", task="audio_cover_pipeline", result=data)
    return data


def list_audio_models_payload(payload: dict) -> dict:
    payload = validate_list_audio_models_payload(payload)
    models_dir = Path(payload["models_dir"])
    include_separation = bool(payload.get("include_separation_models", True))
    include_voice = bool(payload.get("include_voice_models", True))

    separation_models = []
    sources = []
    if include_separation:
        from vocal_tools.catalog import ModelCatalog

        catalog = ModelCatalog(
            model_dir=_separation_models_dir(payload, models_dir),
            builtin_manifest_path=payload.get("builtin_manifest_path"),
            user_manifest_path=payload.get("user_manifest_path"),
            remote_manifest_url=payload.get("remote_manifest_url"),
            remote_manifest_cache_path=payload.get("remote_manifest_cache_path"),
        )
        separation_models = catalog.list_model_cards(
            include_unsupported=bool(payload.get("include_unsupported", False)),
            source=payload.get("source"),
        )
        sources = catalog.list_sources()

    voice_models = []
    if include_voice:
        voice_models = list_rvc_voice_models_payload({"models_dir": str(models_dir)})["voice_models"]

    return {
        "separation_models": _to_jsonable(separation_models),
        "voice_models": _to_jsonable(voice_models),
        "sources": _to_jsonable(sources),
    }


def list_rvc_voice_models_payload(payload: dict) -> dict:
    payload = validate_list_audio_models_payload(
        {
            "models_dir": (payload or {}).get("models_dir"),
            "include_separation_models": False,
            "include_voice_models": True,
        }
    )
    from vocal_tools.catalog import RVCVoiceCatalog

    catalog = RVCVoiceCatalog(payload["models_dir"])
    return {
        "voice_models": [card.to_dict() for card in catalog.list_voice_models()],
    }


def _separation_models_dir(payload: dict, models_dir: Path) -> Path:
    if payload.get("separation_models_dir"):
        return Path(payload["separation_models_dir"])
    mdx_dir = models_dir / "mdx"
    if mdx_dir.exists():
        return mdx_dir
    return models_dir


def _progress_relay(progress_callback: ProgressCallback, stage: str):
    if progress_callback is None:
        return None

    def relay(event):
        event_payload = event.to_dict() if hasattr(event, "to_dict") else event
        _emit(progress_callback, stage, event=event_payload)

    return relay


def _emit(progress_callback: ProgressCallback, stage: str, **payload) -> None:
    if progress_callback is None:
        return
    progress_callback({"stage": stage, **_to_jsonable(payload)})


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return _to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(item) for item in value]
    if hasattr(value, "item") and callable(value.item):
        try:
            return value.item()
        except Exception:
            pass
    return value
