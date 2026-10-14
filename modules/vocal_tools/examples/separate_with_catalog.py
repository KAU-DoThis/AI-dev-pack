import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vocal_tools import ModelCatalog, Separator
from vocal_tools.core import ModelNotDownloadedError, SeparationError


def main():
    catalog = ModelCatalog(model_dir="data/models")
    separator = Separator(catalog=catalog)

    try:
        result = separator.separate(
            input_audio=Path("input/song.mp3"),
            output_dir=Path("output/song"),
            model_id="kim_vocal_2",
            options={"denoise": True},
        )
    except ModelNotDownloadedError as exc:
        print(f"Model file is not ready: {exc}")
        return
    except FileNotFoundError as exc:
        print(f"Input file is not ready: {exc}")
        return
    except SeparationError as exc:
        print(f"Separation failed: {exc}")
        return

    for stem, path in result.stems.items():
        print(f"{stem}: {path}")


if __name__ == "__main__":
    main()
