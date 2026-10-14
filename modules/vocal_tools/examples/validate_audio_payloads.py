import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vocal_tools import (
    validate_audio_cover_pipeline_payload,
    validate_audio_rvc_convert_payload,
    validate_audio_separate_payload,
)


def main():
    payloads = {
        "separate": validate_audio_separate_payload(
            {
                "models_dir": Path("test_models/mdx"),
                "input_audio": "input.wav",
                "output_dir": "output/separate",
                "model_id": "kim_vocal_2",
                "options": {"denoise": False},
            }
        ),
        "rvc": validate_audio_rvc_convert_payload(
            {
                "models_dir": "test_models",
                "input_audio": "vocals.wav",
                "output_audio": "output/rvc/iut.wav",
                "voice_model": "iut",
                "options": {"pitch_change": 0, "f0_method": "rmvpe"},
            }
        ),
        "cover": validate_audio_cover_pipeline_payload(
            {
                "models_dir": "test_models",
                "input_audio": "input.wav",
                "output_dir": "output/cover",
                "mdx_options": {"denoise": False},
                "rvc_options": {"pitch_change": 0, "f0_method": "rmvpe"},
                "mix_options": {"normalize": True, "peak": 0.98},
            }
        ),
    }
    print(json.dumps(payloads, indent=2, ensure_ascii=False))

    try:
        validate_audio_separate_payload({"models_dir": "test_models"})
    except ValueError as exc:
        print(f"expected validation error: {exc}")


if __name__ == "__main__":
    main()
