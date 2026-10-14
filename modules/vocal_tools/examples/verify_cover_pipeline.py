import argparse
import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vocal_tools import AudioMixOptions, CoverPipelineOptions, run_cover_pipeline


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
            f"Refusing cover pipeline verification on long input: {duration:.3f}s > {max_duration_sec:.3f}s"
        )
    return audio


def _classify_error(exc, stage):
    text = f"{type(exc).__name__}: {exc}".lower()
    if "soundfile" in text or "libsndfile" in text:
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
    if "mdx separation failed" in text:
        return "full pipeline failed during MDX separation"
    if "mdx succeeded but rvc" in text:
        return "MDX succeeded but RVC failed"
    if "rvc succeeded but mix" in text:
        return "RVC succeeded but mix failed"
    return f"full pipeline failed during {stage}"


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
    parser = argparse.ArgumentParser(description="Verify MDX to RVC to final cover mix pipeline.")
    parser.add_argument("--models-dir", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--voice-model", required=True)
    parser.add_argument("--mdx-model-id", required=True)
    parser.add_argument("--max-duration-sec", type=float, default=10.5)
    parser.add_argument("--vocal-gain-db", type=float, default=0.0)
    parser.add_argument("--instrumental-gain-db", type=float, default=0.0)
    parser.add_argument("--peak", type=float, default=0.98)
    parser.add_argument("--no-normalize", action="store_true")
    args = parser.parse_args()

    stage = {"name": "input validation"}
    try:
        input_audio = Path(args.input)
        if not input_audio.exists():
            raise FileNotFoundError(f"Input audio not found: {input_audio}")
        _require_short_audio(input_audio, args.max_duration_sec)

        def progress(event):
            stage["name"] = event.get("stage", stage["name"]) if isinstance(event, dict) else stage["name"]
            print("[progress]", _json(event))

        options = CoverPipelineOptions(
            mdx_model_id=args.mdx_model_id,
            voice_model=args.voice_model,
            mdx_options={"denoise": False},
            rvc_options=dict(DEFAULT_RVC_OPTIONS),
            mix_options=AudioMixOptions(
                vocal_gain_db=args.vocal_gain_db,
                instrumental_gain_db=args.instrumental_gain_db,
                normalize=not args.no_normalize,
                peak=args.peak,
            ),
        )

        print("== Input ==")
        print(_json({
            "models_dir": args.models_dir,
            "input_audio": _file_report(input_audio),
            "output_dir": args.output_dir,
            "options": {
                "mdx_model_id": options.mdx_model_id,
                "voice_model": options.voice_model,
                "mdx_options": options.mdx_options,
                "rvc_options": options.rvc_options,
                "mix_options": options.mix_options.__dict__,
            },
        }))

        stage["name"] = "full pipeline"
        result = run_cover_pipeline(
            input_audio=input_audio,
            output_dir=args.output_dir,
            models_dir=args.models_dir,
            options=options,
            progress_callback=progress,
        )

        stage["name"] = "output validation"
        result_dict = result.to_dict()
        stems = result_dict["mdx"]["stems"]
        for stem_name in ("vocals", "instrumental"):
            stem_path = Path(stems[stem_name])
            if not stem_path.exists():
                raise RuntimeError(f"MDX stem was not created: {stem_name} -> {stem_path}")
        converted = Path(result_dict["rvc"]["output_audio"])
        if not converted.exists() or not converted.stat().st_size:
            raise RuntimeError(f"RVC output was not created or is empty: {converted}")
        final_mix = Path(result_dict["mix"]["output_path"])
        if not final_mix.exists() or not final_mix.stat().st_size:
            raise RuntimeError(f"Final mix was not created or is empty: {final_mix}")

        print("== Result ==")
        print(_json({
            "pipeline": result_dict,
            "files": {
                "mdx_vocals": _file_report(stems["vocals"]),
                "mdx_instrumental": _file_report(stems["instrumental"]),
                "rvc_converted": _file_report(converted),
                "cover_mix": _file_report(final_mix),
            },
        }))
        print("cover pipeline verification ok")
    except Exception as exc:
        _print_failure(stage["name"], exc)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
