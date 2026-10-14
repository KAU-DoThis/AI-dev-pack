from __future__ import annotations

from pathlib import Path
from typing import Literal, Union


def _patch_save_audio() -> None:
    import demucs.audio as demucs_audio
    import demucs.separate as demucs_separate
    import torch

    def save_audio(
        wav: torch.Tensor,
        path: Union[str, Path],
        samplerate: int,
        bitrate: int = 320,
        clip: Literal["rescale", "clamp", "tanh", "none"] = "rescale",
        bits_per_sample: Literal[16, 24, 32] = 16,
        as_float: bool = False,
        preset: Literal[2, 3, 4, 5, 6, 7] = 2,
    ) -> None:
        wav = demucs_audio.prevent_clip(wav, mode=clip)
        path = Path(path)
        suffix = path.suffix.lower()
        if suffix == ".mp3":
            demucs_audio.encode_mp3(wav, path, samplerate, bitrate, preset, verbose=True)
            return
        if suffix not in {".wav", ".flac"}:
            raise ValueError(f"Invalid suffix for path: {suffix}")

        import soundfile as sf

        data = wav.detach().cpu()
        if data.ndim == 1:
            data = data.unsqueeze(0)
        data = data.transpose(0, 1).numpy()
        subtype = "FLOAT" if as_float or bits_per_sample == 32 else f"PCM_{bits_per_sample}"
        sf.write(str(path), data, samplerate, subtype=subtype)

    demucs_audio.save_audio = save_audio
    demucs_separate.save_audio = save_audio


def main() -> None:
    _patch_save_audio()
    from demucs.separate import main as demucs_main

    demucs_main()


if __name__ == "__main__":
    main()
