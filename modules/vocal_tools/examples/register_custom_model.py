import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vocal_tools import ModelCatalog


def main():
    catalog = ModelCatalog(
        model_dir="data/models",
        user_manifest_path="data/user_models.json",
    )
    model = catalog.register_local_model(
        model_id="my_custom_vocal_model",
        name="My Custom Vocal Model",
        arch="mdx_onnx",
        filename="custom_model.onnx",
        tasks=["vocal_separation"],
        output_stems=["vocals", "instrumental"],
        tags=["custom", "mdx"],
        defaults={
            "primary_stem": "vocals",
            "secondary_stem": "instrumental",
            "sample_rate": 44100,
            "stereo": True,
        },
        options=[
            {
                "name": "denoise",
                "label": "Denoise",
                "type": "bool",
                "default": False,
            }
        ],
    )
    manifest_path = catalog.save_user_manifest()
    print(f"Registered {model.id} in {manifest_path}")
    print(json.dumps(catalog.list_model_cards(tag="custom"), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
