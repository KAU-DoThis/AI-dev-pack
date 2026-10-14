import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vocal_tools import list_audio_models_payload


def main():
    result = list_audio_models_payload(
        {
            "models_dir": "test_models",
            "include_separation_models": True,
            "include_voice_models": True,
        }
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
