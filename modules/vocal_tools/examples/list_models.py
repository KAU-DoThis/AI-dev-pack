import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vocal_tools import ModelCatalog


def main():
    catalog = ModelCatalog(model_dir="data/models")
    print(json.dumps(catalog.list_model_cards(), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
