from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional


AUDIO_EXTENSIONS = (".wav", ".flac", ".mp3")


def collect_stem_outputs(
    output_dir,
    stem_names: Iterable[str],
    input_stem: Optional[str] = None,
) -> dict[str, Path]:
    """Collect likely stem outputs from a flat or nested output directory."""
    output_dir = Path(output_dir)
    if not output_dir.exists():
        return {}

    audio_files = _audio_files(output_dir)
    stems: dict[str, Path] = {}
    for stem_name in stem_names:
        stem = str(stem_name)
        path = _find_stem_file(audio_files, stem, input_stem)
        if path is not None:
            stems[stem] = path
    return stems


def list_audio_outputs(output_dir) -> list[Path]:
    return _audio_files(Path(output_dir))


def _audio_files(output_dir: Path) -> list[Path]:
    files = [
        path
        for path in output_dir.rglob("*")
        if path.is_file() and path.suffix.lower() in AUDIO_EXTENSIONS
    ]
    return sorted(files, key=_file_sort_key)


def _find_stem_file(
    audio_files: list[Path],
    stem_name: str,
    input_stem: Optional[str],
) -> Optional[Path]:
    exact = [path for path in audio_files if path.stem == stem_name]
    if exact:
        return exact[0]

    lower_name = stem_name.lower()
    lower = [path for path in audio_files if path.stem.lower() == lower_name]
    if lower:
        return lower[0]

    if input_stem:
        prefixed_name = f"{input_stem}_{stem_name}".lower()
        prefixed = [path for path in audio_files if path.stem.lower() == prefixed_name]
        if prefixed:
            return prefixed[0]

    return None


def _file_sort_key(path: Path) -> tuple[int, int, str]:
    try:
        extension_rank = AUDIO_EXTENSIONS.index(path.suffix.lower())
    except ValueError:
        extension_rank = len(AUDIO_EXTENSIONS)
    return (extension_rank, len(path.parts), str(path).lower())
