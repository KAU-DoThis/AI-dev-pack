import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vocal_tools import ModelCatalog


def main():
    remote_manifest = Path("output/examples/remote_models.json")
    remote_manifest.parent.mkdir(parents=True, exist_ok=True)
    remote_manifest.write_text(
        json.dumps(
            {
                "models": [
                    {
                        "id": "kim_vocal_2",
                        "name": "Kim Vocal 2 Remote Override",
                        "arch": "mdx_onnx",
                        "filename": "Kim_Vocal_2.onnx",
                        "tasks": ["vocal_separation"],
                        "output_stems": ["vocals", "instrumental"],
                        "tags": ["mdx", "remote", "2-stem"],
                        "description": "Example remote manifest entry overriding the builtin card.",
                        "defaults": {
                            "primary_stem": "vocals",
                            "secondary_stem": "instrumental",
                            "sample_rate": 44100,
                            "stereo": True,
                        },
                        "ui": {
                            "category": "Remote MDX",
                            "recommended": True,
                            "sort_order": 50,
                            "warning": None,
                        },
                    }
                ]
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    catalog = ModelCatalog(model_dir="test_models/mdx")
    catalog.load_remote_manifest(str(remote_manifest))

    print(json.dumps(catalog.list_sources(), indent=2, ensure_ascii=False))
    print(json.dumps(catalog.get_model("kim_vocal_2").to_dict(), indent=2, ensure_ascii=False))
    print(json.dumps(catalog.list_model_cards()[0], indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
