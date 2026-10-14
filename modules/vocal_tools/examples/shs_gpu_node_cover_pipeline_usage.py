import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vocal_tools import AudioMixOptions, CoverPipelineOptions, run_cover_pipeline


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


def _validate_optional_max_duration(input_audio, payload):
    max_duration_sec = payload.get("max_duration_sec")
    if max_duration_sec is None:
        return
    audio = _audio_info(input_audio)
    duration = audio.get("duration_sec")
    if duration is not None and duration > float(max_duration_sec):
        raise ValueError(
            f"Input audio duration {duration:.3f}s exceeds max_duration_sec={float(max_duration_sec):.3f}"
        )


def handle_cover_pipeline_payload(payload, progress_callback=None):
    models_dir = Path(payload["models_dir"])
    input_audio = Path(payload["input_audio"])
    output_dir = Path(payload["output_dir"])

    _validate_optional_max_duration(input_audio, payload)

    options = CoverPipelineOptions(
        mdx_model_id=payload.get("mdx_model_id", "kim_vocal_2"),
        voice_model=payload.get("voice_model", "iut"),
        mdx_options=dict(payload.get("mdx_options") or {}),
        rvc_options=dict(payload.get("rvc_options") or {}),
        mix_options=AudioMixOptions(**dict(payload.get("mix_options") or {})),
    )
    result = run_cover_pipeline(
        input_audio=input_audio,
        output_dir=output_dir,
        models_dir=models_dir,
        options=options,
        progress_callback=progress_callback,
    )
    return result.to_dict()


def print_progress(event):
    print(json.dumps(event, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    example_payload = {
        "models_dir": "test_models",
        "input_audio": "output/verify_mdx/test_tone_10s.wav",
        "output_dir": "output/verify_cover_pipeline",
        "mdx_model_id": "kim_vocal_2",
        "voice_model": "iut",
        "mdx_options": {
            "denoise": False,
        },
        "rvc_options": {
            "pitch_change": 0,
            "f0_method": "rmvpe",
            "index_rate": 0.66,
            "protect": 0.33,
        },
        "mix_options": {
            "vocal_gain_db": 0.0,
            "instrumental_gain_db": 0.0,
            "normalize": True,
            "peak": 0.98,
        },
        "max_duration_sec": 10.5,
    }
    result = handle_cover_pipeline_payload(example_payload, progress_callback=print_progress)
    print(json.dumps(result, indent=2, ensure_ascii=False))
