import argparse
import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vocal_tools import ModelCatalog, Separator


def _json(data):
    return json.dumps(data, indent=2, ensure_ascii=False)


def _file_report(path):
    path = Path(path)
    return {
        "path": str(path),
        "exists": path.exists(),
        "size": path.stat().st_size if path.exists() else None,
    }


def _audio_info(path):
    try:
        import soundfile as sf
    except ImportError as exc:
        return {
            "path": str(path),
            "audio_info": "soundfile unavailable",
            "error": str(exc),
        }

    info = sf.info(str(path))
    channels = getattr(info, "channels", None)
    frames = getattr(info, "frames", None)
    samplerate = getattr(info, "samplerate", None)
    duration = float(frames) / float(samplerate) if frames and samplerate else None
    return {
        "path": str(path),
        "samplerate": samplerate,
        "channels": channels,
        "frames": frames,
        "duration_sec": duration,
    }


def _print_catalog(catalog):
    print("== Catalog models ==")
    for card in catalog.list_model_cards():
        print(
            _json(
                {
                    "id": card["id"],
                    "name": card["name"],
                    "arch": card["arch"],
                    "downloaded": card["downloaded"],
                    "model_path": card["model_path"],
                    "output_stems": card["output_stems"],
                    "options": card["options"],
                }
            )
        )


def _require_legacy_mdx():
    try:
        from vocal_tools.mdx import MDXSeparator
    except Exception as exc:
        print("== Legacy MDX import failed ==")
        print(f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
        raise SystemExit(2) from exc
    return MDXSeparator


def _verify_legacy(MDXSeparator, models_dir, input_audio, output_dir):
    print("== Legacy MDXSeparator verification ==")
    legacy = MDXSeparator(models_dir=models_dir)
    result = legacy.separate(
        input_audio=input_audio,
        output_dir=output_dir / "legacy",
        denoise=False,
        split_main_backup=False,
        dereverb=False,
    )
    result_dict = result.as_dict()
    print("legacy_result:")
    print(_json(result_dict))

    for key in ("vocals", "instrumental"):
        path = result_dict.get(key)
        print(f"legacy_{key}:")
        print(_json(_file_report(path) if path else {"path": None, "exists": False, "size": None}))
        if path:
            print(_json(_audio_info(path)))


def _verify_catalog(models_dir, input_audio, output_dir):
    print("== ModelCatalog + Separator verification ==")
    catalog = ModelCatalog(model_dir=models_dir)
    separator = Separator(catalog=catalog)

    def progress(event):
        print("[progress]", event.to_dict() if hasattr(event, "to_dict") else event)

    result = separator.separate(
        input_audio=input_audio,
        output_dir=output_dir / "catalog",
        model_id="kim_vocal_2",
        options={"denoise": False},
        progress_callback=progress,
    )
    result_dict = result.to_dict()
    print("catalog_result:")
    print(_json(result_dict))

    for key in ("vocals", "instrumental"):
        path = result.stems.get(key)
        print(f"catalog_{key}:")
        print(_json(_file_report(path) if path else {"path": None, "exists": False, "size": None}))
        if path:
            print(_json(_audio_info(path)))

    print("catalog_model_params:")
    print(_json(result.metadata.get("model_params")))
    print("catalog_options_used:")
    print(_json(result.options_used))


def main():
    parser = argparse.ArgumentParser(description="Verify vocal_tools MDX code APIs.")
    parser.add_argument("--models-dir", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    models_dir = Path(args.models_dir)
    input_audio = Path(args.input)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print("== Inputs ==")
    print(_json({"models_dir": str(models_dir), "input": str(input_audio), "output_dir": str(output_dir)}))
    print("input_audio:")
    print(_json(_file_report(input_audio)))
    print(_json(_audio_info(input_audio)))

    catalog = ModelCatalog(model_dir=models_dir)
    _print_catalog(catalog)

    MDXSeparator = _require_legacy_mdx()
    _verify_legacy(MDXSeparator, models_dir, input_audio, output_dir)
    _verify_catalog(models_dir, input_audio, output_dir)


if __name__ == "__main__":
    main()
