import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vocal_tools import ModelCatalog, Separator


def handle_separation_payload(payload, progress_callback=None):
    catalog = ModelCatalog(
        model_dir=payload["model_dir"],
        user_manifest_path=payload.get("user_manifest_path"),
    )
    separator = Separator(catalog=catalog)
    result = separator.separate(
        input_audio=payload["input_audio"],
        output_dir=payload["output_dir"],
        model_id=payload["model_id"],
        options=payload.get("options") or {},
        progress_callback=progress_callback,
    )
    return result.to_dict()


def print_progress(event):
    print(event.to_dict())


if __name__ == "__main__":
    example_payload = {
        "model_dir": "data/models",
        "input_audio": "input/song.mp3",
        "output_dir": "output/song",
        "model_id": "kim_vocal_2",
        "options": {"denoise": True},
    }
    print(handle_separation_payload(example_payload, progress_callback=print_progress))
