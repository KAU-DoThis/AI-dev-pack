from __future__ import annotations

import argparse
from contextlib import redirect_stdout
import json
import sys
import traceback
from pathlib import Path
from typing import Any

from vocal_tools.core.options import parse_key_value_options
from vocal_tools.tasks import (
    list_audio_models_payload,
    list_rvc_voice_models_payload,
    run_audio_cover_pipeline_payload,
    run_audio_rvc_convert_payload,
    run_audio_separate_payload,
    run_tts_payload,
)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 2

    try:
        args.func(args)
    except Exception as exc:
        if getattr(args, "debug", False):
            traceback.print_exc()
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vocal-tools",
        description="Headless command-line utilities for the vocal_tools audio engine library.",
    )
    parser.add_argument("--debug", action="store_true", help="Show tracebacks when a command fails.")
    subparsers = parser.add_subparsers(dest="command")

    models = subparsers.add_parser("models", help="List separation models and download status.")
    model_subparsers = models.add_subparsers(dest="models_command")
    models_list = model_subparsers.add_parser("list", help="List separation model cards.")
    _add_model_listing_args(models_list)
    models_list.add_argument("--json", action="store_true", help="Print JSON output.")
    models_list.add_argument("--arch", help="Filter by separation architecture, for example mdx_onnx.")
    models_list.add_argument("--task", help="Filter by task, for example vocal_separation.")
    models_list.set_defaults(func=cmd_models_list)

    models_status = model_subparsers.add_parser("status", help="Show separation model download status.")
    _add_model_listing_args(models_status)
    models_status.add_argument("--json", action="store_true", help="Print JSON output.")
    models_status.set_defaults(func=cmd_models_status)

    voices = subparsers.add_parser("voices", help="List RVC voice models.")
    voice_subparsers = voices.add_subparsers(dest="voices_command")
    voices_list = voice_subparsers.add_parser("list", help="List RVC voice models.")
    voices_list.add_argument("--models-dir", required=True, help="Root directory containing RVC voice models.")
    voices_list.add_argument("--json", action="store_true", help="Print JSON output.")
    voices_list.set_defaults(func=cmd_voices_list)

    separate = subparsers.add_parser("separate", help="Run source separation.")
    separate.add_argument("--models-dir", default=".", help="Separation model directory.")
    separate.add_argument("--model", help="Model id, filename, directory, or direct model file path.")
    separate.add_argument("--model-id", default="kim_vocal_2", help="Model id from the catalog.")
    separate.add_argument(
        "--model-dir",
        dest="model_dirs",
        action="append",
        default=[],
        help="Additional model search directory. Can be repeated.",
    )
    separate.add_argument(
        "--model-dirs",
        dest="model_dirs",
        action="append",
        help="Additional model search directory. Can be repeated.",
    )
    separate.add_argument("--download-dir", help="Directory for resolver-managed model downloads.")
    separate.add_argument(
        "--no-auto-download",
        action="store_false",
        dest="auto_download",
        help="Do not download missing catalog models.",
    )
    separate.add_argument(
        "--no-verify",
        action="store_false",
        dest="verify",
        help="Skip checksum and file-size verification during resolver downloads.",
    )
    separate.add_argument("--input", required=True, dest="input_audio", help="Input audio path.")
    separate.add_argument("--output-dir", required=True, help="Output directory for stems.")
    separate.add_argument(
        "--option",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="Model option override. Can be repeated.",
    )
    separate.add_argument("--builtin-manifest-path", help="Optional builtin manifest override.")
    separate.add_argument("--user-manifest-path", help="Optional user manifest path.")
    separate.add_argument("--remote-manifest-url", help="Optional remote manifest URL.")
    separate.add_argument("--remote-manifest-cache-path", help="Optional remote manifest cache path.")
    separate.add_argument("--json", action="store_true", help="Print JSON output.")
    separate.set_defaults(func=cmd_separate)

    rvc = subparsers.add_parser("rvc-convert", help="Run RVC voice conversion.")
    rvc.add_argument("--models-dir", required=True, help="Root directory containing Hubert/RMVPE and voices.")
    rvc.add_argument("--voice-model", help="Voice model id, directory, or .pth path.")
    rvc.add_argument("--model", help="Voice model id, directory, or direct .pth path. Overrides --voice-model.")
    rvc.add_argument("--index", help="Optional .index file. Overrides automatic index discovery.")
    rvc.add_argument(
        "--model-dir",
        dest="model_dirs",
        action="append",
        default=[],
        help="Additional RVC voice model search directory. Can be repeated.",
    )
    rvc.add_argument(
        "--model-dirs",
        dest="model_dirs",
        action="append",
        help="Additional RVC voice model search directory. Can be repeated.",
    )
    rvc.add_argument(
        "--download-dir",
        help="Directory for resolver-managed downloads. RVC auto-download is limited to single-file models.",
    )
    rvc.add_argument(
        "--auto-download",
        action="store_true",
        help="Download resolver-supported single-file RVC models when available.",
    )
    rvc.add_argument(
        "--no-verify",
        action="store_false",
        dest="verify",
        help="Skip checksum and file-size verification during resolver downloads.",
    )
    rvc.add_argument("--input", required=True, dest="input_audio", help="Input vocal audio path.")
    rvc.add_argument("--output", required=True, dest="output_audio", help="Output converted audio path.")
    rvc.add_argument("--hubert-path", help="Optional Hubert model path.")
    rvc.add_argument("--rmvpe-path", help="Optional RMVPE model path.")
    rvc.add_argument("--device", default="auto", help="Device for RVC conversion: auto, cpu, cuda:0, etc.")
    rvc.add_argument("--is-half", dest="is_half", help="Half precision setting: auto, true, or false.")
    rvc.add_argument(
        "--option",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="RVC option override. Can be repeated.",
    )
    rvc.add_argument("--json", action="store_true", help="Print JSON output.")
    rvc.set_defaults(func=cmd_rvc_convert)

    cover = subparsers.add_parser("cover", help="Run MDX -> RVC -> Mix cover pipeline.")
    cover.add_argument("--models-dir", required=True, help="Root model directory.")
    cover.add_argument("--input", required=True, dest="input_audio", help="Input audio path.")
    cover.add_argument("--output-dir", required=True, help="Output directory for pipeline artifacts.")
    cover.add_argument("--mdx-model-id", default="kim_vocal_2", help="MDX vocal separation model id.")
    cover.add_argument("--voice-model", default="iut", help="RVC voice model id, directory, or .pth path.")
    cover.add_argument("--vocal-stem", default="vocals", help="Stem name to feed into RVC.")
    cover.add_argument("--instrumental-stem", default="instrumental", help="Stem name to use as backing audio.")
    cover.add_argument("--rvc-output-name", help="Optional converted vocal filename.")
    cover.add_argument("--mix-output-name", default="cover_mix.wav", help="Final mixed filename.")
    cover.add_argument("--no-keep-intermediate", action="store_false", dest="keep_intermediate")
    cover.add_argument("--use-dereverb", action="store_true", help="Run optional dereverb MDX pass.")
    cover.add_argument("--dereverb-model-id", default="reverb_hq_by_foxjoy")
    cover.add_argument("--use-karaoke-split", action="store_true", help="Run optional main/backing split pass.")
    cover.add_argument("--karaoke-model-id", default="uvr_mdxnet_kara_2")
    cover.add_argument("--mdx-option", action="append", default=[], metavar="KEY=VALUE")
    cover.add_argument("--rvc-option", action="append", default=[], metavar="KEY=VALUE")
    cover.add_argument("--mix-option", action="append", default=[], metavar="KEY=VALUE")
    cover.add_argument("--dereverb-option", action="append", default=[], metavar="KEY=VALUE")
    cover.add_argument("--karaoke-option", action="append", default=[], metavar="KEY=VALUE")
    cover.add_argument("--json", action="store_true", help="Print JSON output.")
    cover.set_defaults(func=cmd_cover)

    tts = subparsers.add_parser("tts", help="Synthesize speech with Edge or a Qwen3-TTS reference voice.")
    tts.add_argument("--engine", choices=["edge", "qwen3"], default="edge")
    tts.add_argument("--text", required=True, help="Text to speak.")
    tts.add_argument("--output", required=True, dest="output_path", help="MP3/WAV for Edge, WAV for Qwen3.")
    tts.add_argument("--voice", help="Edge voice ID, default ko-KR-SunHiNeural.")
    tts.add_argument("--reference-audio", help="Qwen3 reference audio file.")
    tts.add_argument("--reference-text", help="Transcript of the reference audio, when known.")
    tts.add_argument("--language", help="Qwen3 language, for example Korean or ko; default Auto.")
    tts.add_argument("--x-vector-only", dest="x_vector_only_mode", action="store_true", default=None,
                     help="Clone speaker embedding only; automatically used when reference text is omitted.")
    tts.add_argument("--model", help="Qwen3-TTS Base model ID or directory; default Qwen/Qwen3-TTS-12Hz-0.6B-Base.")
    tts.add_argument("--device", help="Qwen3 device: auto, cpu, cuda:0, etc.")
    tts.add_argument("--dtype", choices=["auto", "float32", "float16", "bfloat16"])
    tts.add_argument("--cache-dir", help="Qwen3 model cache directory.")
    tts.add_argument("--local-files-only", action="store_true", default=None, help="Use cached/local Qwen3 models only.")
    tts.add_argument("--option", action="append", default=[], metavar="KEY=VALUE",
                     help="Edge rate/volume/pitch or Qwen3 generation option. May be repeated.")
    tts.add_argument("--json", action="store_true")
    tts.set_defaults(func=cmd_tts)

    return parser


def cmd_models_list(args: argparse.Namespace) -> None:
    result = list_audio_models_payload(_model_listing_payload(args, include_voice=False))
    models = _filter_models(result["separation_models"], arch=args.arch, task=args.task)
    if args.json:
        _print_json({"separation_models": models, "sources": result.get("sources", [])})
        return

    _print_table(
        [
            {
                "id": card.get("id"),
                "name": card.get("name"),
                "arch": card.get("arch"),
                "downloaded": card.get("downloaded"),
                "status": card.get("download_status"),
                "stems": ", ".join(card.get("output_stems") or []),
            }
            for card in models
        ],
        ["id", "name", "arch", "downloaded", "status", "stems"],
        empty="No separation models found.",
    )


def cmd_models_status(args: argparse.Namespace) -> None:
    result = list_audio_models_payload(_model_listing_payload(args, include_voice=False))
    models = result["separation_models"]
    if args.json:
        _print_json({"models": [_status_card(card) for card in models], "sources": result.get("sources", [])})
        return

    _print_table(
        [_status_card(card) for card in models],
        ["id", "status", "downloaded", "managed_by", "path"],
        empty="No separation models found.",
    )


def cmd_voices_list(args: argparse.Namespace) -> None:
    result = list_rvc_voice_models_payload({"models_dir": args.models_dir})
    voices = result["voice_models"]
    if args.json:
        _print_json(result)
        return

    _print_table(
        [
            {
                "id": card.get("id"),
                "name": card.get("name"),
                "downloaded": card.get("downloaded"),
                "has_index": card.get("has_index"),
                "pth_path": card.get("pth_path"),
            }
            for card in voices
        ],
        ["id", "name", "downloaded", "has_index", "pth_path"],
        empty="No RVC voice models found.",
    )


def cmd_separate(args: argparse.Namespace) -> None:
    from vocal_tools.catalog import ModelCatalog
    from vocal_tools.separator import Separator

    catalog = ModelCatalog(
        model_dir=Path(args.models_dir),
        builtin_manifest_path=args.builtin_manifest_path,
        user_manifest_path=args.user_manifest_path,
        remote_manifest_url=args.remote_manifest_url,
        remote_manifest_cache_path=args.remote_manifest_cache_path,
    )
    separator = Separator(catalog=catalog)
    result_obj = separator.separate(
        input_audio=args.input_audio,
        output_dir=args.output_dir,
        model=args.model,
        model_id=args.model_id,
        models_dir=args.models_dir,
        model_dirs=args.model_dirs,
        download_dir=args.download_dir,
        auto_download=args.auto_download,
        verify=args.verify,
        options=parse_key_value_options(args.option),
        progress_callback=_progress_callback(args.json),
    )
    result = result_obj.to_dict()
    if args.json:
        _print_json(result)
        return

    print("Separation complete.")
    _print_table(
        [{"stem": stem, "path": path} for stem, path in result.get("stems", {}).items()],
        ["stem", "path"],
        empty="No stems were returned.",
    )


def cmd_rvc_convert(args: argparse.Namespace) -> None:
    from vocal_tools.catalog import ModelCatalog
    from vocal_tools.rvc import RVCConversionOptions, RVCConverter

    option_values = parse_key_value_options(args.option)
    if args.is_half is not None:
        is_half = parse_key_value_options([f"is_half={args.is_half}"])["is_half"]
    else:
        is_half = "auto"

    catalog = ModelCatalog(model_dir=Path(args.models_dir))
    converter = RVCConverter(
        models_dir=args.models_dir,
        hubert_path=args.hubert_path,
        rmvpe_path=args.rmvpe_path,
        device=args.device,
        is_half=is_half,
        catalog=catalog,
    )
    options = RVCConversionOptions(**option_values)
    converted = converter.convert(
        input_audio=args.input_audio,
        output_audio=args.output_audio,
        voice_model=args.voice_model,
        model=args.model,
        index=args.index,
        model_dirs=args.model_dirs,
        download_dir=args.download_dir,
        auto_download=args.auto_download,
        verify=args.verify,
        options=options,
    )
    converted = Path(converted)
    result = {
        "input_audio": str(args.input_audio),
        "output_audio": str(converted),
        "voice_model": str(args.model or args.voice_model),
        "exists": converted.exists(),
        "size": converted.stat().st_size if converted.exists() else None,
        "options_used": options.__dict__,
    }
    if args.json:
        _print_json(result)
        return

    print("RVC conversion complete.")
    print(result.get("output_audio"))


def cmd_cover(args: argparse.Namespace) -> None:
    payload = {
        "models_dir": args.models_dir,
        "input_audio": args.input_audio,
        "output_dir": args.output_dir,
        "mdx_model_id": args.mdx_model_id,
        "voice_model": args.voice_model,
        "mdx_options": parse_key_value_options(args.mdx_option),
        "rvc_options": parse_key_value_options(args.rvc_option),
        "mix_options": parse_key_value_options(args.mix_option),
        "vocal_stem": args.vocal_stem,
        "instrumental_stem": args.instrumental_stem,
        "rvc_output_name": args.rvc_output_name,
        "mix_output_name": args.mix_output_name,
        "keep_intermediate": args.keep_intermediate,
        "use_dereverb": args.use_dereverb,
        "dereverb_model_id": args.dereverb_model_id,
        "dereverb_options": parse_key_value_options(args.dereverb_option),
        "use_karaoke_split": args.use_karaoke_split,
        "karaoke_model_id": args.karaoke_model_id,
        "karaoke_options": parse_key_value_options(args.karaoke_option),
    }
    result = run_audio_cover_pipeline_payload(payload, progress_callback=_progress_callback(args.json))
    if args.json:
        _print_json(result)
        return

    print("Cover pipeline complete.")
    mix = result.get("mix") or {}
    print(mix.get("output_path") or result.get("output_dir"))


def cmd_tts(args: argparse.Namespace) -> None:
    payload = {"engine": args.engine, "text": args.text, "output_path": args.output_path,
               "options": parse_key_value_options(args.option), "config": {}}
    for key in ("voice", "reference_audio", "reference_text", "language", "x_vector_only_mode"):
        if getattr(args, key) is not None:
            payload[key] = getattr(args, key)
    for key in ("model", "device", "dtype", "cache_dir", "local_files_only"):
        if getattr(args, key) is not None:
            payload["config"][key] = getattr(args, key)
    # Upstream model loaders may print status banners. Keep stdout JSON-safe.
    with redirect_stdout(sys.stderr):
        result = run_tts_payload(payload)
    if args.json:
        _print_json(result)
    else:
        print(result["output_path"])


def _add_model_listing_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--models-dir", required=True, help="Root model directory.")
    parser.add_argument("--separation-models-dir", help="Override separation model directory.")
    parser.add_argument("--builtin-manifest-path", help="Optional builtin manifest override.")
    parser.add_argument("--user-manifest-path", help="Optional user manifest path.")
    parser.add_argument("--remote-manifest-url", help="Optional remote manifest URL.")
    parser.add_argument("--remote-manifest-cache-path", help="Optional remote manifest cache path.")
    parser.add_argument(
        "--include-unsupported",
        action="store_true",
        help="Include model cards for architectures without a runnable vocal_tools engine.",
    )
    parser.add_argument("--source", help="Filter model cards by source, for example builtin, remote, user, or uvr.")


def _model_listing_payload(args: argparse.Namespace, *, include_voice: bool) -> dict[str, Any]:
    return {
        "models_dir": args.models_dir,
        "separation_models_dir": args.separation_models_dir,
        "builtin_manifest_path": args.builtin_manifest_path,
        "user_manifest_path": args.user_manifest_path,
        "remote_manifest_url": args.remote_manifest_url,
        "remote_manifest_cache_path": args.remote_manifest_cache_path,
        "include_unsupported": args.include_unsupported,
        "source": args.source,
        "include_separation_models": True,
        "include_voice_models": include_voice,
    }


def _filter_models(
    models: list[dict[str, Any]],
    *,
    arch: str | None = None,
    task: str | None = None,
) -> list[dict[str, Any]]:
    filtered = list(models)
    if arch is not None:
        filtered = [card for card in filtered if card.get("arch") == arch]
    if task is not None:
        filtered = [card for card in filtered if task in (card.get("tasks") or [])]
    return filtered


def _status_card(card: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": card.get("id"),
        "status": card.get("download_status"),
        "downloaded": card.get("downloaded"),
        "managed_by": card.get("managed_by"),
        "path": card.get("model_path"),
    }


def _progress_callback(json_mode: bool):
    if json_mode:
        return None

    def relay(event: dict[str, Any]) -> None:
        stage = event.get("stage")
        if stage in {"task_start", "task_complete"}:
            print(f"[{stage}] {event.get('task', '')}".rstrip(), file=sys.stderr)

    return relay


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _print_table(rows: list[dict[str, Any]], columns: list[str], *, empty: str) -> None:
    if not rows:
        print(empty)
        return

    normalized = [{column: _cell(row.get(column)) for column in columns} for row in rows]
    widths = {
        column: max(len(column), *(len(row[column]) for row in normalized))
        for column in columns
    }
    header = "  ".join(column.ljust(widths[column]) for column in columns)
    divider = "  ".join("-" * widths[column] for column in columns)
    print(header)
    print(divider)
    for row in normalized:
        print("  ".join(row[column].ljust(widths[column]) for column in columns))


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return ", ".join(_cell(item) for item in value)
    return str(value)


if __name__ == "__main__":
    raise SystemExit(main())
