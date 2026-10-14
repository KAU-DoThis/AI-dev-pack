import argparse
import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vocal_tools import AudioMixOptions, mix_vocal_with_instrumental


def _json(data):
    return json.dumps(data, indent=2, ensure_ascii=False)


def _audio_info(path):
    import numpy as np
    import soundfile as sf

    info = sf.info(str(path))
    audio, _ = sf.read(str(path), dtype="float32", always_2d=True)
    peak = float(np.max(np.abs(audio))) if audio.size else 0.0
    return {
        "samplerate": info.samplerate,
        "channels": info.channels,
        "frames": info.frames,
        "duration_sec": float(info.frames) / float(info.samplerate) if info.samplerate else None,
        "format": info.format,
        "subtype": info.subtype,
        "peak": peak,
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


def _classify_error(exc):
    text = f"{type(exc).__name__}: {exc}".lower()
    if "soundfile" in text or "libsndfile" in text or "error opening" in text:
        return "soundfile read/write problem"
    if "scipy" in text or "resample_poly" in text:
        return "scipy resample problem"
    if "sample rate" in text or "samplerate" in text:
        return "sample rate mismatch handling problem"
    if "channel" in text or "mono" in text or "stereo" in text:
        return "mono/stereo conversion problem"
    if "frame" in text or "length" in text or "padding" in text or "trim" in text:
        return "length mismatch handling problem"
    if "clip" in text or "normalize" in text or "peak" in text:
        return "clipping/normalize problem"
    return "audio mix verification failed"


def _print_failure(exc):
    print("== Failure ==")
    print(_json({
        "classification": _classify_error(exc),
        "error_type": type(exc).__name__,
        "error": str(exc),
    }))
    print("traceback:")
    print("".join(traceback.format_exception(type(exc), exc, exc.__traceback__, limit=12)))


def main():
    parser = argparse.ArgumentParser(description="Verify vocal + instrumental audio mixing.")
    parser.add_argument("--vocal", default="output/verify_pipeline/rvc/iut_converted.wav")
    parser.add_argument("--instrumental", default="output/verify_pipeline/mdx/test_tone_10s_instrumental.wav")
    parser.add_argument("--output", default="output/verify_mix/cover_mix.wav")
    parser.add_argument("--vocal-gain-db", type=float, default=0.0)
    parser.add_argument("--instrumental-gain-db", type=float, default=0.0)
    parser.add_argument("--output-sample-rate", type=int, default=None)
    parser.add_argument("--output-channels", type=int, default=2)
    parser.add_argument("--no-normalize", action="store_true")
    parser.add_argument("--peak", type=float, default=0.98)
    parser.add_argument("--trim-to-shortest", action="store_true")
    args = parser.parse_args()

    try:
        options = AudioMixOptions(
            vocal_gain_db=args.vocal_gain_db,
            instrumental_gain_db=args.instrumental_gain_db,
            output_sample_rate=args.output_sample_rate,
            output_channels=args.output_channels,
            normalize=not args.no_normalize,
            peak=args.peak,
            trim_to_shortest=args.trim_to_shortest,
        )
        print("== Input ==")
        print(_json({
            "vocal": _file_report(args.vocal),
            "instrumental": _file_report(args.instrumental),
            "output": str(args.output),
            "options": options.__dict__,
        }))

        result = mix_vocal_with_instrumental(
            vocal_path=args.vocal,
            instrumental_path=args.instrumental,
            output_path=args.output,
            options=options,
        )
        output_report = _file_report(args.output)
        if not output_report["exists"] or not output_report["size"]:
            raise RuntimeError(f"Mix output was not created or is empty: {args.output}")

        print("== Result ==")
        print(_json({
            "mix": result.to_dict(),
            "output": output_report,
        }))
        print("audio mix verification ok")
    except Exception as exc:
        _print_failure(exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
