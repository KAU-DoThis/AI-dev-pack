import argparse
import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


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
    duration = float(info.frames) / float(info.samplerate) if info.samplerate else None
    return {
        "samplerate": info.samplerate,
        "channels": info.channels,
        "frames": info.frames,
        "duration_sec": duration,
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
            f"Refusing RVC verification on long input: {duration:.3f}s > {max_duration_sec:.3f}s"
        )
    return audio


def _classify_error(exc, stage):
    text = f"{type(exc).__name__}: {exc}".lower()
    if stage == "dependency import":
        if "fairseq" in text:
            return "RVC dependency import failed: fairseq"
        if "faiss" in text:
            return "RVC dependency import failed: faiss"
        if "parselmouth" in text:
            return "RVC dependency import failed: praat-parselmouth"
        if "pyworld" in text:
            return "RVC dependency import failed: pyworld"
        return "RVC dependency import failed"
    if "hubert" in text:
        return "Hubert loading failed"
    if "rmvpe" in text:
        return "RMVPE loading or f0 extraction failed"
    if ".pth" in text or "voice model" in text:
        return "Voice model .pth loading failed"
    if ".index" in text or "faiss" in text:
        return "Index loading failed"
    if "f0" in text or "pitch" in text:
        return "f0 extraction failed"
    if "wav" in text or "write" in text or "save" in text:
        return "Output WAV save failed"
    return f"RVC verification failed during {stage}"


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
    parser = argparse.ArgumentParser(description="Verify RVCConverter with real model loading and conversion.")
    parser.add_argument("--models-dir", required=True)
    parser.add_argument("--voice-model", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--max-duration-sec", type=float, default=10.5)
    args = parser.parse_args()

    stage = "dependency import"
    try:
        from vocal_tools.rvc import RVCConversionOptions, RVCConverter

        stage = "input validation"
        models_dir = Path(args.models_dir)
        input_audio = Path(args.input)
        output_audio = Path(args.output)
        if not input_audio.exists():
            raise FileNotFoundError(f"Input audio not found: {input_audio}")
        input_audio_info = _require_short_audio(input_audio, args.max_duration_sec)

        print("== Input ==")
        print(_json({"audio": _file_report(input_audio), "max_duration_sec": args.max_duration_sec}))

        stage = "converter creation"
        converter = RVCConverter(models_dir=models_dir)

        stage = "voice model discovery"
        paths = converter.find_voice_model(args.voice_model)
        print("== RVC paths ==")
        print(_json({
            "voice_model": args.voice_model,
            "model_path": str(paths.model_path),
            "index_path": str(paths.index_path) if paths.index_path else None,
            "hubert_path": str(converter.hubert_path),
            "rmvpe_path": str(converter.rmvpe_path),
            "device": converter.device,
            "is_half": converter.is_half,
            "files": {
                "voice_model": _file_report(paths.model_path),
                "index": _file_report(paths.index_path) if paths.index_path else None,
                "hubert": _file_report(converter.hubert_path),
                "rmvpe": _file_report(converter.rmvpe_path),
            },
        }))

        stage = "conversion"
        options = RVCConversionOptions(**DEFAULT_RVC_OPTIONS)
        print("== RVC options ==")
        print(_json(DEFAULT_RVC_OPTIONS))
        result_path = converter.convert(
            input_audio=input_audio,
            output_audio=output_audio,
            voice_model=args.voice_model,
            options=options,
        )

        stage = "output validation"
        report = _file_report(result_path)
        if not report["exists"] or not report["size"]:
            raise RuntimeError(f"RVC output was not created or is empty: {result_path}")

        print("== Output ==")
        print(_json({
            "output_audio": str(result_path),
            "exists": report["exists"],
            "size": report["size"],
            "audio": report.get("audio"),
            "input_duration_sec": input_audio_info.get("duration_sec"),
        }))
        print("rvc verification ok")
    except Exception as exc:
        _print_failure(stage, exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
