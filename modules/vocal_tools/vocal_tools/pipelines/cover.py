from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Iterable

from vocal_tools.audio import AudioMixOptions, mix_vocal_with_instrumental


@dataclass
class CoverPipelineOptions:
    mdx_model_id: str = "kim_vocal_2"
    voice_model: str = "iut"
    mdx_options: dict[str, Any] = field(default_factory=dict)
    rvc_options: dict[str, Any] = field(default_factory=dict)
    mix_options: AudioMixOptions | dict[str, Any] = field(default_factory=AudioMixOptions)
    vocal_stem: str = "vocals"
    instrumental_stem: str = "instrumental"
    rvc_output_name: str | None = None
    mix_output_name: str = "cover_mix.wav"
    keep_intermediate: bool = True
    use_dereverb: bool = False
    dereverb_model_id: str = "reverb_hq_by_foxjoy"
    dereverb_options: dict[str, Any] = field(default_factory=dict)
    use_karaoke_split: bool = False
    karaoke_model_id: str = "uvr_mdxnet_kara_2"
    karaoke_options: dict[str, Any] = field(default_factory=dict)


@dataclass
class CoverPipelineResult:
    input_audio: Path
    output_dir: Path
    mdx: dict[str, Any]
    rvc: dict[str, Any]
    mix: dict[str, Any]
    preprocessing: dict[str, Any] = field(default_factory=dict)
    options_used: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_audio": str(self.input_audio),
            "output_dir": str(self.output_dir),
            "mdx": _to_jsonable(self.mdx),
            "preprocessing": _to_jsonable(self.preprocessing),
            "rvc": _to_jsonable(self.rvc),
            "mix": _to_jsonable(self.mix),
            "options_used": _to_jsonable(self.options_used),
        }


def run_cover_pipeline(
    input_audio: str | Path,
    output_dir: str | Path,
    models_dir: str | Path,
    options: CoverPipelineOptions | None = None,
    progress_callback=None,
) -> CoverPipelineResult:
    options = options or CoverPipelineOptions()
    input_audio = Path(input_audio)
    output_dir = Path(output_dir)
    models_dir = Path(models_dir)
    mdx_output_dir = output_dir / "mdx"
    rvc_output_dir = output_dir / "rvc"
    mix_output_dir = output_dir / "mix"
    mix_options = _coerce_mix_options(options.mix_options)

    if not input_audio.exists():
        raise FileNotFoundError(f"Input audio not found: {input_audio}")

    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        from vocal_tools.catalog import ModelCatalog
        from vocal_tools.separator import Separator

        catalog = ModelCatalog(model_dir=models_dir / "mdx")
        separator = Separator(catalog=catalog)
    except Exception as exc:
        raise RuntimeError(f"Could not initialize MDX catalog/separator: {exc}") from exc

    _emit(
        progress_callback,
        "mdx_start",
        input_audio=str(input_audio),
        output_dir=str(mdx_output_dir),
        model_id=options.mdx_model_id,
    )
    try:
        mdx_result = separator.separate(
            input_audio=input_audio,
            output_dir=mdx_output_dir,
            model_id=options.mdx_model_id,
            options=dict(options.mdx_options),
            progress_callback=_progress_relay(progress_callback, "mdx_progress"),
        )
    except Exception as exc:
        raise RuntimeError(f"MDX separation failed: {exc}") from exc

    vocals = _required_stem(mdx_result.stems, options.vocal_stem, "vocal")
    instrumental = _required_stem(mdx_result.stems, options.instrumental_stem, "instrumental")
    preprocessing: dict[str, Any] = {
        "selected_vocal_stem": options.vocal_stem,
        "selected_instrumental_stem": options.instrumental_stem,
        "rvc_input_audio": str(vocals),
        "notes": [],
    }

    _emit(
        progress_callback,
        "mdx_complete",
        stems={name: str(path) for name, path in mdx_result.stems.items()},
        selected_vocal_stem=options.vocal_stem,
        selected_instrumental_stem=options.instrumental_stem,
    )

    if options.use_dereverb:
        _emit(
            progress_callback,
            "dereverb_start",
            input_audio=str(vocals),
            model_id=options.dereverb_model_id,
        )
        try:
            dereverb_result = separator.separate(
                input_audio=vocals,
                output_dir=mdx_output_dir / "dereverb",
                model_id=options.dereverb_model_id,
                options=dict(options.dereverb_options),
                progress_callback=_progress_relay(progress_callback, "dereverb_progress"),
            )
        except Exception as exc:
            raise RuntimeError(f"Optional dereverb step failed: {exc}") from exc
        vocals = (
            dereverb_result.stems.get("dry_vocals")
            or dereverb_result.stems.get(options.vocal_stem)
            or _first_stem(dereverb_result.stems)
        )
        preprocessing["dereverb"] = dereverb_result.to_dict()
        preprocessing["rvc_input_audio"] = str(vocals)
        preprocessing["notes"].append(
            "Dereverb uses an additional MDX pass when use_dereverb=True."
        )
        _emit(progress_callback, "dereverb_complete", input_audio=str(vocals))

    if options.use_karaoke_split:
        _emit(
            progress_callback,
            "karaoke_split_start",
            input_audio=str(vocals),
            model_id=options.karaoke_model_id,
        )
        try:
            karaoke_result = separator.separate(
                input_audio=vocals,
                output_dir=mdx_output_dir / "karaoke_split",
                model_id=options.karaoke_model_id,
                options=dict(options.karaoke_options),
                progress_callback=_progress_relay(progress_callback, "karaoke_split_progress"),
            )
        except Exception as exc:
            raise RuntimeError(f"Optional karaoke/main vocal split failed: {exc}") from exc
        vocals = (
            karaoke_result.stems.get(options.vocal_stem)
            or karaoke_result.stems.get("vocals")
            or _first_stem(karaoke_result.stems)
        )
        preprocessing["karaoke_split"] = karaoke_result.to_dict()
        preprocessing["rvc_input_audio"] = str(vocals)
        preprocessing["notes"].append(
            "Main/backing split is structural for now; final quality tuning is a later task."
        )
        _emit(progress_callback, "karaoke_split_complete", input_audio=str(vocals))

    _emit(
        progress_callback,
        "rvc_start",
        voice_model=options.voice_model,
        input_audio=str(vocals),
        output_dir=str(rvc_output_dir),
    )
    try:
        from vocal_tools.rvc import RVCConversionOptions, RVCConverter

        converter = RVCConverter(models_dir=models_dir)
        rvc_options = RVCConversionOptions(**dict(options.rvc_options))
        rvc_output = rvc_output_dir / _output_name(
            options.rvc_output_name,
            f"{_safe_stem(options.voice_model)}_converted.wav",
        )
        converted = converter.convert(
            input_audio=vocals,
            output_audio=rvc_output,
            voice_model=options.voice_model,
            options=rvc_options,
        )
    except Exception as exc:
        raise RuntimeError(f"MDX succeeded but RVC conversion failed: {exc}") from exc

    rvc_payload = {
        "voice_model": options.voice_model,
        "input_audio": str(vocals),
        "output_audio": str(converted),
        "exists": Path(converted).exists(),
        "size": Path(converted).stat().st_size if Path(converted).exists() else None,
        "options": _to_jsonable(asdict(rvc_options)),
    }
    _emit(progress_callback, "rvc_complete", output_audio=str(converted))

    _emit(
        progress_callback,
        "mix_start",
        vocal_audio=str(converted),
        instrumental_audio=str(instrumental),
        output_dir=str(mix_output_dir),
    )
    try:
        mix_result = mix_vocal_with_instrumental(
            vocal_path=converted,
            instrumental_path=instrumental,
            output_path=mix_output_dir / _output_name(options.mix_output_name, "cover_mix.wav"),
            options=mix_options,
        )
    except Exception as exc:
        raise RuntimeError(f"RVC succeeded but mix failed: {exc}") from exc

    _emit(progress_callback, "mix_complete", output_audio=str(mix_result.output_path))

    cleanup = None
    if not options.keep_intermediate:
        cleanup = _cleanup_intermediate_files(
            [
                *mdx_result.stems.values(),
                Path(converted),
            ]
        )
        preprocessing["cleanup"] = cleanup
        _emit(progress_callback, "cleanup_complete", cleanup=cleanup)

    return CoverPipelineResult(
        input_audio=input_audio,
        output_dir=output_dir,
        mdx=mdx_result.to_dict(),
        preprocessing=preprocessing,
        rvc=rvc_payload,
        mix=mix_result.to_dict(),
        options_used=_pipeline_options_to_dict(options, mix_options),
    )


def _progress_relay(progress_callback, stage: str):
    if progress_callback is None:
        return None

    def relay(event):
        event_payload = event.to_dict() if hasattr(event, "to_dict") else event
        _emit(progress_callback, stage, event=event_payload)

    return relay


def _emit(progress_callback, stage: str, **payload) -> None:
    if progress_callback is None:
        return
    progress_callback({"stage": stage, **_to_jsonable(payload)})


def _required_stem(stems: dict[str, Path], name: str, role: str) -> Path:
    path = stems.get(name)
    if path is None:
        raise RuntimeError(f"MDX result does not include the {role} stem {name!r}: {list(stems)}")
    return path


def _first_stem(stems: dict[str, Path]) -> Path:
    for path in stems.values():
        return path
    raise RuntimeError("Expected at least one output stem, got none.")


def _coerce_mix_options(options: AudioMixOptions | dict[str, Any]) -> AudioMixOptions:
    if isinstance(options, AudioMixOptions):
        return options
    if isinstance(options, dict):
        return AudioMixOptions(**dict(options))
    raise TypeError(f"mix_options must be AudioMixOptions or dict, got {type(options).__name__}.")


def _pipeline_options_to_dict(options: CoverPipelineOptions, mix_options: AudioMixOptions) -> dict[str, Any]:
    data = asdict(options)
    data["mix_options"] = asdict(mix_options)
    return _to_jsonable(data)


def _output_name(value: str | None, default: str) -> str:
    candidate = Path(str(value or default)).name
    return candidate or default


def _safe_stem(value: str | Path) -> str:
    name = Path(str(value)).stem or "voice"
    return "".join(char if char.isalnum() or char in ("-", "_") else "_" for char in name)


def _cleanup_intermediate_files(paths: Iterable[Path]) -> dict[str, Any]:
    removed = []
    failed = []
    for path in paths:
        try:
            path = Path(path)
            if path.exists():
                path.unlink()
                removed.append(str(path))
        except OSError as exc:
            failed.append({"path": str(path), "error": str(exc)})
    return {"removed_files": removed, "failed": failed}


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
