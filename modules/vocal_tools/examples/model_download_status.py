import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vocal_tools import ModelCatalog


def main():
    catalog = ModelCatalog(model_dir="test_models/mdx")
    statuses = [
        catalog.store.get_download_status(model)
        for model in catalog.list_models()
    ]
    print(json.dumps(statuses, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
