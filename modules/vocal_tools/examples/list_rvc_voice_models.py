import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vocal_tools import RVCVoiceCatalog, list_rvc_voice_models_payload


def main():
    catalog = RVCVoiceCatalog("test_models")
    print(json.dumps([card.to_dict() for card in catalog.list_voice_models()], indent=2, ensure_ascii=False))
    print(json.dumps(list_rvc_voice_models_payload({"models_dir": "test_models"}), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
