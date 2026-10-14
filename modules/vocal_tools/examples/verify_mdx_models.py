import argparse
import json
import math
import struct
import sys
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vocal_tools import ModelCatalog, Separator


MODEL_IDS = ("kim_vocal_2", "uvr_mdxnet_kara_2", "reverb_hq_by_foxjoy")


def _json(data):
    return json.dumps(data, indent=2, ensure_ascii=False)


def _ensure_test_tone(path):
    path = Path(path)
    if path.exists():
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    samplerate = 44100
    duration_sec = 10
    amplitude = 0.12
    frames = samplerate * duration_sec
    with wave.open(str(path), "wb") as outfile:
        outfile.setnchannels(2)
        outfile.setsampwidth(2)
        outfile.setframerate(samplerate)
        for index in range(frames):
            t = index / samplerate
            left = amplitude * math.sin(2.0 * math.pi * 440.0 * t)
            right = amplitude * math.sin(2.0 * math.pi * 660.0 * t)
            outfile.writeframesraw(struct.pack("<hh", int(left * 32767), int(right * 32767)))
    return True


def _file_report(path):
    path = Path(path)
    report = {
        "path": str(path),
        "exists": path.exists(),
        "size": path.stat().st_size if path.exists() else None,
    }
    report["audio"] = _audio_info(path) if path.exists() else None
    return report


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


def _print_model_cards(catalog):
    print("== Model cards ==")
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


def _verify_model(separator, model_id, input_audio, output_dir):
    print(f"== Verify {model_id} ==")

    def progress(event):
        payload = event.to_dict() if hasattr(event, "to_dict") else event
        print("[progress]", _json(payload))

    result = separator.separate(
        input_audio=input_audio,
        output_dir=Path(output_dir) / model_id,
        model_id=model_id,
        options={"denoise": False},
        progress_callback=progress,
    )
    result_dict = result.to_dict()
    print("result:")
    print(_json(result_dict))
    print("stem_keys:")
    print(_json(list(result.stems)))
    print("runtime:")
    print(_json(result.metadata.get("runtime")))

    print("stem_files:")
    print(_json({stem: _file_report(path) for stem, path in result.stems.items()}))
    return result


def main():
    parser = argparse.ArgumentParser(description="Verify builtin MDX models through ModelCatalog + Separator.")
    parser.add_argument("--models-dir", default="test_models/mdx")
    parser.add_argument("--input", default="output/verify_mdx/test_tone_10s.wav")
    parser.add_argument("--output-dir", default="output/verify_mdx_models")
    args = parser.parse_args()

    input_audio = Path(args.input)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    created = _ensure_test_tone(input_audio)
    print("== Input ==")
    print(_json({"path": str(input_audio), "created": created, **_file_report(input_audio)}))

    catalog = ModelCatalog(model_dir=args.models_dir)
    _print_model_cards(catalog)

    separator = Separator(catalog=catalog)
    summary = {}
    for model_id in MODEL_IDS:
        result = _verify_model(separator, model_id, input_audio, output_dir)
        summary[model_id] = {
            "stems": {stem: str(path) for stem, path in result.stems.items()},
            "runtime": result.metadata.get("runtime"),
        }

    print("== Summary ==")
    print(_json(summary))


if __name__ == "__main__":
    main()
