import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vocal_tools import (
    run_audio_cover_pipeline_payload,
    run_audio_rvc_convert_payload,
    run_audio_separate_payload,
)


def print_progress(event):
    print(json.dumps(event, indent=2, ensure_ascii=False))


SEPARATE_PAYLOAD = {
    "models_dir": "test_models/mdx",
    "input_audio": "input.wav",
    "output_dir": "output/separate",
    "model_id": "kim_vocal_2",
    "options": {"denoise": False},
}

RVC_PAYLOAD = {
    "models_dir": "test_models",
    "input_audio": "vocals.wav",
    "output_audio": "output/rvc/iut.wav",
    "voice_model": "iut",
    "options": {
        "pitch_change": 0,
        "f0_method": "rmvpe",
    },
}

COVER_PIPELINE_PAYLOAD = {
    "models_dir": "test_models",
    "input_audio": "input.wav",
    "output_dir": "output/cover",
    "mdx_model_id": "kim_vocal_2",
    "voice_model": "iut",
    "mdx_options": {"denoise": False},
    "rvc_options": {
        "pitch_change": 0,
        "f0_method": "rmvpe",
    },
    "mix_options": {
        "normalize": True,
        "peak": 0.98,
    },
}


def main():
    print("Payload helpers are pure Python functions. They do not create a server or queue.")
    print(json.dumps(
        {
            "separate": SEPARATE_PAYLOAD,
            "rvc": RVC_PAYLOAD,
            "cover_pipeline": COVER_PIPELINE_PAYLOAD,
        },
        indent=2,
        ensure_ascii=False,
    ))

    # Uncomment one call when inputs and models are ready:
    # result = run_audio_separate_payload(SEPARATE_PAYLOAD, progress_callback=print_progress)
    # result = run_audio_rvc_convert_payload(RVC_PAYLOAD, progress_callback=print_progress)
    # result = run_audio_cover_pipeline_payload(COVER_PIPELINE_PAYLOAD, progress_callback=print_progress)
    # print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
