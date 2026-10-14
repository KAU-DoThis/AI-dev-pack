from __future__ import annotations

import math
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any


@dataclass
class AudioMixOptions:
    vocal_gain_db: float = 0.0
    instrumental_gain_db: float = 0.0
    output_sample_rate: int | None = None
    output_channels: int = 2
    normalize: bool = True
    peak: float = 0.98
    trim_to_shortest: bool = False


@dataclass
class AudioMixResult:
    vocal_path: Path
    instrumental_path: Path
    output_path: Path
    sample_rate: int
    channels: int
    frames: int
    duration_sec: float
    options_used: dict[str, Any]
    metadata: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "vocal_path": str(self.vocal_path),
            "instrumental_path": str(self.instrumental_path),
            "output_path": str(self.output_path),
            "sample_rate": self.sample_rate,
            "channels": self.channels,
            "frames": self.frames,
            "duration_sec": self.duration_sec,
            "options_used": _to_jsonable(self.options_used),
            "metadata": _to_jsonable(self.metadata),
        }


def mix_vocal_with_instrumental(
    vocal_path: str | Path,
    instrumental_path: str | Path,
    output_path: str | Path,
    options: AudioMixOptions | None = None,
) -> AudioMixResult:
    options = options or AudioMixOptions()
    vocal_path = Path(vocal_path)
    instrumental_path = Path(instrumental_path)
    output_path = Path(output_path)

    if not vocal_path.exists():
        raise FileNotFoundError(f"Vocal audio not found: {vocal_path}")
    if not instrumental_path.exists():
        raise FileNotFoundError(f"Instrumental audio not found: {instrumental_path}")

    output_channels = int(options.output_channels)
    if output_channels < 1:
        raise ValueError("output_channels must be >= 1.")
    if options.output_sample_rate is not None and int(options.output_sample_rate) < 1:
        raise ValueError("output_sample_rate must be >= 1 when provided.")
    peak = float(options.peak)
    if peak <= 0.0 or peak > 1.0:
        raise ValueError("peak must be > 0.0 and <= 1.0.")

    try:
        import numpy as np
        import soundfile as sf
    except ImportError as exc:
        raise RuntimeError(
            "Audio mixing requires numpy and soundfile. Install the audio runtime dependencies first."
        ) from exc

    vocal_audio, vocal_sr, vocal_info = _read_audio(sf, np, vocal_path)
    instrumental_audio, instrumental_sr, instrumental_info = _read_audio(sf, np, instrumental_path)
    output_sample_rate = int(options.output_sample_rate or instrumental_sr)

    vocal_resampled = _resample_audio(np, vocal_audio, vocal_sr, output_sample_rate)
    instrumental_resampled = _resample_audio(np, instrumental_audio, instrumental_sr, output_sample_rate)
    vocal_channels = _convert_channels(np, vocal_resampled, output_channels)
    instrumental_channels = _convert_channels(np, instrumental_resampled, output_channels)

    if options.trim_to_shortest:
        frames = min(vocal_channels.shape[0], instrumental_channels.shape[0])
        vocal_aligned = vocal_channels[:frames]
        instrumental_aligned = instrumental_channels[:frames]
        alignment_mode = "trim_to_shortest"
    else:
        frames = max(vocal_channels.shape[0], instrumental_channels.shape[0])
        vocal_aligned = _pad_to_frames(np, vocal_channels, frames)
        instrumental_aligned = _pad_to_frames(np, instrumental_channels, frames)
        alignment_mode = "pad_to_longest"

    vocal_gain = _db_to_linear(float(options.vocal_gain_db))
    instrumental_gain = _db_to_linear(float(options.instrumental_gain_db))
    mixed = (vocal_aligned * vocal_gain) + (instrumental_aligned * instrumental_gain)
    mixed = np.nan_to_num(mixed, copy=False)

    pre_normalize_peak = _peak(np, mixed)
    normalization_gain = 1.0
    normalize_applied = False
    if options.normalize and pre_normalize_peak > peak:
        normalization_gain = peak / pre_normalize_peak
        mixed *= normalization_gain
        normalize_applied = True

    post_normalize_peak = _peak(np, mixed)
    limiter_applied = False
    if post_normalize_peak > 1.0:
        mixed = np.clip(mixed, -1.0, 1.0)
        limiter_applied = True

    output_peak = _peak(np, mixed)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(output_path), mixed.astype("float32", copy=False), output_sample_rate, format="WAV")

    options_used = asdict(options)
    options_used["resolved_output_sample_rate"] = output_sample_rate
    options_used["resolved_output_channels"] = output_channels
    metadata = {
        "vocal": {
            **vocal_info,
            "frames_after_resample": int(vocal_resampled.shape[0]),
            "frames_after_alignment": int(vocal_aligned.shape[0]),
            "channels_after_mix_conversion": int(vocal_channels.shape[1]),
            "gain_db": float(options.vocal_gain_db),
            "gain_linear": float(vocal_gain),
            "resampled": vocal_sr != output_sample_rate,
        },
        "instrumental": {
            **instrumental_info,
            "frames_after_resample": int(instrumental_resampled.shape[0]),
            "frames_after_alignment": int(instrumental_aligned.shape[0]),
            "channels_after_mix_conversion": int(instrumental_channels.shape[1]),
            "gain_db": float(options.instrumental_gain_db),
            "gain_linear": float(instrumental_gain),
            "resampled": instrumental_sr != output_sample_rate,
        },
        "alignment": {
            "mode": alignment_mode,
            "output_frames": int(frames),
            "vocal_padding_frames": int(max(0, frames - vocal_channels.shape[0])),
            "instrumental_padding_frames": int(max(0, frames - instrumental_channels.shape[0])),
            "vocal_trimmed_frames": int(max(0, vocal_channels.shape[0] - frames)),
            "instrumental_trimmed_frames": int(max(0, instrumental_channels.shape[0] - frames)),
        },
        "mix": {
            "pre_normalize_peak": float(pre_normalize_peak),
            "post_normalize_peak": float(post_normalize_peak),
            "output_peak": float(output_peak),
            "normalize_applied": bool(normalize_applied),
            "normalization_gain": float(normalization_gain),
            "limiter_applied": bool(limiter_applied),
        },
    }

    return AudioMixResult(
        vocal_path=vocal_path,
        instrumental_path=instrumental_path,
        output_path=output_path,
        sample_rate=output_sample_rate,
        channels=output_channels,
        frames=int(frames),
        duration_sec=float(frames) / float(output_sample_rate) if output_sample_rate else 0.0,
        options_used=options_used,
        metadata=metadata,
    )


def _read_audio(sf, np, path: Path):
    try:
        audio, sample_rate = sf.read(str(path), dtype="float32", always_2d=True)
        info = sf.info(str(path))
    except Exception as exc:
        raise RuntimeError(f"soundfile failed to read audio {path}: {exc}") from exc

    audio = np.asarray(audio, dtype=np.float64)
    audio = np.nan_to_num(audio, copy=False)
    return audio, int(sample_rate), {
        "path": str(path),
        "input_sample_rate": int(sample_rate),
        "input_channels": int(info.channels),
        "input_frames": int(info.frames),
        "input_duration_sec": float(info.frames) / float(info.samplerate) if info.samplerate else 0.0,
        "format": info.format,
        "subtype": info.subtype,
    }


def _resample_audio(np, audio, source_sample_rate: int, target_sample_rate: int):
    if source_sample_rate == target_sample_rate:
        return audio

    try:
        from scipy.signal import resample_poly
    except ImportError as exc:
        raise RuntimeError(
            "scipy.signal.resample_poly is required when input sample rates differ."
        ) from exc

    gcd = math.gcd(int(source_sample_rate), int(target_sample_rate))
    up = int(target_sample_rate) // gcd
    down = int(source_sample_rate) // gcd
    return np.asarray(resample_poly(audio, up, down, axis=0), dtype=np.float64)


def _convert_channels(np, audio, target_channels: int):
    current_channels = int(audio.shape[1])
    if current_channels == target_channels:
        return audio
    if target_channels == 1:
        return np.mean(audio, axis=1, keepdims=True)
    if current_channels == 1:
        return np.repeat(audio, target_channels, axis=1)
    if current_channels > target_channels:
        return audio[:, :target_channels]

    repeats = int(math.ceil(target_channels / current_channels))
    return np.tile(audio, (1, repeats))[:, :target_channels]


def _pad_to_frames(np, audio, frames: int):
    missing = int(frames) - int(audio.shape[0])
    if missing <= 0:
        return audio[:frames]
    return np.pad(audio, ((0, missing), (0, 0)), mode="constant")


def _db_to_linear(gain_db: float) -> float:
    return math.pow(10.0, gain_db / 20.0)


def _peak(np, audio) -> float:
    if audio.size == 0:
        return 0.0
    return float(np.max(np.abs(audio)))


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return _to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(item) for item in value]
    if hasattr(value, "item") and callable(value.item):
        try:
            return value.item()
        except Exception:
            pass
    return value
