import json
from pathlib import Path

import librosa
import numpy as np
import soundfile as sf

from vocal_tools.mdx import run_mdx


VOCAL_MODEL = "Kim_Vocal_2.onnx"
KARAOKE_MODEL = "UVR_MDXNET_KARA_2.onnx"
DEREVERB_MODEL = "Reverb_HQ_By_FoxJoy.onnx"


def ensure_stereo_44k(input_path: Path, work_dir: Path) -> Path:
    """Convert input audio to stereo 44.1k WAV if needed."""
    wave, sr = librosa.load(str(input_path), sr=44100, mono=False)

    # librosa returns shape (n,) for mono and (2, n) for stereo.
    if isinstance(wave, np.ndarray) and wave.ndim == 1:
        stereo_wave = np.vstack([wave, wave])
        out_path = work_dir / f"{input_path.stem}_stereo.wav"
        sf.write(str(out_path), stereo_wave.T, 44100)
        return out_path

    if sr != 44100 or input_path.suffix.lower() != ".wav":
        out_path = work_dir / f"{input_path.stem}_44k.wav"
        sf.write(str(out_path), wave.T, 44100)
        return out_path

    return input_path


def load_mdx_params(models_dir: Path) -> dict:
    model_data_path = models_dir / "model_data.json"
    if not model_data_path.exists():
        raise FileNotFoundError(f"model_data.json not found: {model_data_path}")
    with model_data_path.open("r", encoding="utf-8") as infile:
        return json.load(infile)


def validate_models(models_dir: Path) -> tuple[Path, Path, Path]:
    vocal_model_path = models_dir / VOCAL_MODEL
    karaoke_model_path = models_dir / KARAOKE_MODEL
    dereverb_model_path = models_dir / DEREVERB_MODEL

    for model_path in (vocal_model_path, karaoke_model_path, dereverb_model_path):
        if not model_path.exists():
            raise FileNotFoundError(f"MDX model not found: {model_path}")

    return vocal_model_path, karaoke_model_path, dereverb_model_path


def separate_vocals(
    input_audio: Path,
    output_dir: Path,
    models_dir: Path,
    denoise: bool,
    keep_input: bool,
    do_main_backup_split: bool,
    do_dereverb: bool,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    mdx_model_params = load_mdx_params(models_dir)
    vocal_model_path, karaoke_model_path, dereverb_model_path = validate_models(models_dir)

    safe_input = ensure_stereo_44k(input_audio, output_dir)

    print("[1/3] Separating vocals and instrumental...")
    vocals_path, instrumental_path = run_mdx(
        mdx_model_params,
        str(output_dir),
        str(vocal_model_path),
        str(safe_input),
        denoise=denoise,
        keep_orig=keep_input,
    )

    main_vocals_path = None
    backup_vocals_path = None
    main_vocals_dereverb_path = None

    if not do_main_backup_split:
        return {
            "input": str(input_audio),
            "vocals": str(vocals_path),
            "instrumental": str(instrumental_path),
            "main_vocals": None,
            "backup_vocals": None,
            "main_vocals_dereverb": None,
        }

    print("[2/3] Separating main and backup vocals...")
    backup_vocals_path, main_vocals_path = run_mdx(
        mdx_model_params,
        str(output_dir),
        str(karaoke_model_path),
        str(vocals_path),
        suffix="Backup",
        invert_suffix="Main",
        denoise=denoise,
        keep_orig=True,
    )

    if do_dereverb:
        print("[3/3] Removing reverb from main vocals...")
        _, main_vocals_dereverb_path = run_mdx(
            mdx_model_params,
            str(output_dir),
            str(dereverb_model_path),
            str(main_vocals_path),
            invert_suffix="DeReverb",
            exclude_main=True,
            denoise=denoise,
            keep_orig=True,
        )
    else:
        print("[3/3] Skipped (do_dereverb=False)")

    return {
        "input": str(input_audio),
        "vocals": str(vocals_path),
        "instrumental": str(instrumental_path),
        "main_vocals": str(main_vocals_path),
        "backup_vocals": str(backup_vocals_path),
        "main_vocals_dereverb": str(main_vocals_dereverb_path) if main_vocals_dereverb_path else None,
    }


def main() -> None:
    # -------------------- USER SETTINGS (edit here) --------------------

    input_dir = Path("temp/gobak").resolve()
    input_audio = input_dir / "test.mp3"
    output_dir = input_dir / "output"
    models_dir = Path("data/models/mdx").resolve()

    # Common options
    denoise = True
    keep_input = True

    # Step toggles
    # True/False only. Toggle what you want to run in VS Code.
    do_main_backup_split = True
    do_dereverb = True

    # Quick presets (uncomment one block if needed)
    # 1) Fast split only (vocals + instrumental)
    # do_main_backup_split = False
    # do_dereverb = False

    # 2) Vocals + Main/Backup split, no dereverb
    # do_main_backup_split = True
    # do_dereverb = False

    # 3) Full pipeline (default)
    # do_main_backup_split = True
    # do_dereverb = True
    # -------------------------------------------------------------------

    if not input_audio.exists():
        raise FileNotFoundError(f"Input audio not found: {input_audio}")

    outputs = separate_vocals(
        input_audio=input_audio,
        output_dir=output_dir,
        models_dir=models_dir,
        denoise=denoise,
        keep_input=keep_input,
        do_main_backup_split=do_main_backup_split,
        do_dereverb=do_dereverb,
    )

    print("\nDone. Output files:")
    for key, value in outputs.items():
        print(f"- {key}: {value}")


if __name__ == "__main__":
    main()
