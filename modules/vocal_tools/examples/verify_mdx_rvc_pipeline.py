import argparse
import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vocal_tools import ModelCatalog, Separator
from vocal_tools.rvc import RVCConversionOptions, RVCConverter


DEFAULT_RVC_OPTIONS = {
    "pitch_change": 0,
    "f0_method": "rmvpe",
    "index_rate": 0.66,
    "filter_radius": 3,
    "rms_mix_rate": 1.0,
    "protect": 0.33,
}


def _json(data):
    return json.dumps(data, indent=2, ensure_ascii=False)


def _audio_info(path):
    try:
        import soundfile as sf
    except ImportError as exc:
        return {"status": "soundfile unavailable", "error": str(exc)}

    info = sf.info(str(path))
    return {
        "samplerate": info.samplerate,
        "channels": info.channels,
        "frames": info.frames,
        "duration_sec": float(info.frames) / float(info.samplerate) if info.samplerate else None,
    }


def _file_report(path):
    path = Path(path)
    report = {
        "path": str(path),
        "exists": path.exists(),
        "size": path.stat().st_size if path.exists() else None,
    }
    if path.exists() and path.suffix.lower() in {".wav", ".flac", ".mp3", ".ogg", ".m4a"}:
        try:
            report["audio"] = _audio_info(path)
        except Exception as exc:
            report["audio"] = {"status": "audio info failed", "error": str(exc)}
    return report


def _require_short_audio(path, max_duration_sec):
    audio = _audio_info(path)
    duration = audio.get("duration_sec")
    if duration is not None and duration > max_duration_sec:
        raise ValueError(
            f"Refusing pipeline verification on long input: {duration:.3f}s > {max_duration_sec:.3f}s"
        )
    return audio


def _safe_stem(value):
    name = Path(str(value)).stem or "voice"
    return "".join(char if char.isalnum() or char in ("-", "_") else "_" for char in name)


def _classify_error(exc, stage):
    text = f"{type(exc).__name__}: {exc}".lower()
    if stage.startswith("mdx"):
        return "MDX separation failed"
    if "hubert" in text:
        return "MDX succeeded, RVC failed: Hubert loading failed"
    if "rmvpe" in text:
        return "MDX succeeded, RVC failed: RMVPE loading or f0 extraction failed"
    if ".pth" in text or "voice model" in text:
        return "MDX succeeded, RVC failed: voice model .pth loading failed"
    if ".index" in text or "faiss" in text:
        return "MDX succeeded, RVC failed: index loading failed"
    if "f0" in text or "pitch" in text:
        return "MDX succeeded, RVC failed: f0 extraction failed"
    if "wav" in text or "write" in text or "save" in text:
        return "Output WAV save failed"
    return f"Pipeline verification failed during {stage}"


def _print_failure(stage, exc):
    print("== Failure ==")
    print(_json({
        "stage": stage,
        "classification": _classify_error(exc, stage),
        "error_type": type(exc).__name__,
        "error": str(exc),
    }))
    print("traceback:")
    print("".join(traceback.format_exception(type(exc), exc, exc.__traceback__, limit=12)))


def main():
    parser = argparse.ArgumentParser(description="Verify MDX vocals to RVC conversion pipeline.")
    parser.add_argument("--models-dir", required=True)
    parser.add_argument("--mdx-model-id", required=True)
    parser.add_argument("--voice-model", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--max-duration-sec", type=float, default=10.5)
    args = parser.parse_args()

    stage = "input validation"
    try:
        models_dir = Path(args.models_dir)
        input_audio = Path(args.input)
        output_dir = Path(args.output_dir)
        if not input_audio.exists():
            raise FileNotFoundError(f"Input audio not found: {input_audio}")
        _require_short_audio(input_audio, args.max_duration_sec)
        output_dir.mkdir(parents=True, exist_ok=True)

        print("== Input ==")
        print(_json({
            "models_dir": str(models_dir),
            "mdx_model_id": args.mdx_model_id,
            "voice_model": args.voice_model,
            "input_audio": _file_report(input_audio),
        }))

        stage = "mdx separation"
        catalog = ModelCatalog(model_dir=models_dir / "mdx")
        separator = Separator(catalog=catalog)

        def progress(event):
            payload = event.to_dict() if hasattr(event, "to_dict") else event
            print("[mdx progress]", _json(payload))

        mdx_result = separator.separate(
            input_audio=input_audio,
            output_dir=output_dir / "mdx",
            model_id=args.mdx_model_id,
            options={"denoise": False},
            progress_callback=progress,
        )
        vocals = mdx_result.stems.get("vocals")
        if vocals is None:
            raise RuntimeError(f"MDX result does not include a 'vocals' stem: {list(mdx_result.stems)}")

        stage = "rvc conversion"
        converter = RVCConverter(models_dir=models_dir)
        rvc_output = output_dir / "rvc" / f"{_safe_stem(args.voice_model)}_converted.wav"
        options = RVCConversionOptions(**DEFAULT_RVC_OPTIONS)
        converted = converter.convert(
            input_audio=vocals,
            output_audio=rvc_output,
            voice_model=args.voice_model,
            options=options,
        )

        stage = "output validation"
        rvc_report = _file_report(converted)
        if not rvc_report["exists"] or not rvc_report["size"]:
            raise RuntimeError(f"RVC output was not created or is empty: {converted}")

        payload = {
            "mdx": mdx_result.to_dict(),
            "rvc": {
                "voice_model": args.voice_model,
                "input_audio": str(vocals),
                "output_audio": str(converted),
                "exists": rvc_report["exists"],
                "size": rvc_report["size"],
                "audio": rvc_report.get("audio"),
                "options": DEFAULT_RVC_OPTIONS,
            },
        }
        print("== Payload ==")
        print(_json(payload))
        print("mdx+rvc pipeline verification ok")
    except Exception as exc:
        _print_failure(stage, exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
