import json
import math
import struct
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vocal_tools import ModelCatalog, Separator


MODELS_DIR = Path("test_models/mdx")
USER_MANIFEST_PATH = Path("output/verify_mdx_models/user_models.json")
INPUT_AUDIO = Path("output/verify_mdx/test_tone_10s.wav")
OUTPUT_DIR = Path("output/verify_mdx_models/uvr_mdxnet_voc_ft")
MODEL_ID = "uvr_mdxnet_voc_ft"


def _json(data):
    return json.dumps(data, indent=2, ensure_ascii=False)


def _ensure_test_tone(path):
    if path.exists():
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    samplerate = 44100
    frames = samplerate * 10
    with wave.open(str(path), "wb") as outfile:
        outfile.setnchannels(2)
        outfile.setsampwidth(2)
        outfile.setframerate(samplerate)
        for index in range(frames):
            t = index / samplerate
            left = 0.12 * math.sin(2.0 * math.pi * 440.0 * t)
            right = 0.12 * math.sin(2.0 * math.pi * 660.0 * t)
            outfile.writeframesraw(struct.pack("<hh", int(left * 32767), int(right * 32767)))
    return True


def _audio_info(path):
    try:
        import soundfile as sf
    except ImportError as exc:
        return {"status": "soundfile unavailable", "error": str(exc)}
    info = sf.info(str(path))
    return {
        "samplerate": info.samplerate,
        "channels": info.channels,
        "frames": info.frames,
        "duration_sec": info.frames / info.samplerate if info.samplerate else None,
    }


def _file_report(path):
    path = Path(path)
    return {
        "path": str(path),
        "exists": path.exists(),
        "size": path.stat().st_size if path.exists() else None,
        "audio": _audio_info(path) if path.exists() else None,
    }


def main():
    USER_MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
    created = _ensure_test_tone(INPUT_AUDIO)
    print("== Input ==")
    print(_json({"created": created, **_file_report(INPUT_AUDIO)}))

    catalog = ModelCatalog(
        model_dir=MODELS_DIR,
        user_manifest_path=USER_MANIFEST_PATH,
    )
    model = catalog.register_local_model(
        model_id=MODEL_ID,
        name="UVR MDX-Net Voc FT",
        arch="mdx_onnx",
        filename="UVR-MDX-NET-Voc_FT.onnx",
        tasks=["vocal_separation"],
        output_stems=["vocals", "instrumental"],
        tags=["custom", "mdx", "vocal", "2-stem"],
        description="Custom-registered UVR MDX-Net vocal fine-tuned model.",
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
                "advanced": False,
            }
        ],
        overwrite=True,
    )
    saved_path = catalog.save_user_manifest()
    print("== Registered model ==")
    print(_json({"model": model.to_dict(), "user_manifest_path": str(saved_path)}))

    reloaded_catalog = ModelCatalog(
        model_dir=MODELS_DIR,
        user_manifest_path=USER_MANIFEST_PATH,
    )
    card = next(card for card in reloaded_catalog.list_model_cards() if card["id"] == MODEL_ID)
    print("== Reloaded card ==")
    print(_json(card))
    print("== Option schema ==")
    print(_json([option.to_dict() for option in reloaded_catalog.get_option_schema(MODEL_ID)]))

    separator = Separator(catalog=reloaded_catalog)

    def progress(event):
        payload = event.to_dict() if hasattr(event, "to_dict") else event
        print("[progress]", _json(payload))

    result = separator.separate(
        input_audio=INPUT_AUDIO,
        output_dir=OUTPUT_DIR,
        model_id=MODEL_ID,
        options={"denoise": False},
        progress_callback=progress,
    )
    print("== Result ==")
    print(_json(result.to_dict()))
    print("== Stem files ==")
    print(_json({stem: _file_report(path) for stem, path in result.stems.items()}))


if __name__ == "__main__":
    main()
