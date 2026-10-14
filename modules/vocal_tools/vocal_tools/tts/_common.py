from __future__ import annotations

import asyncio
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
import tempfile
from typing import Any

from vocal_tools.core.errors import InvalidOptionError, TTSError
from vocal_tools.core.progress import ProgressEvent


@dataclass(frozen=True)
class TTSResult:
    output_path: Path
    engine: str
    sample_rate: int | None = None
    channels: int | None = None
    duration_sec: float | None = None
    model: str | None = None
    voice: str | None = None
    options_used: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data['output_path'] = str(self.output_path)
        return data


def validate_text(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise InvalidOptionError('text must be a non-empty string.')
    return text


def output_path(value, suffixes) -> Path:
    if not isinstance(value, (str, Path)) or not str(value).strip():
        raise InvalidOptionError('output_path must be a non-empty string or Path.')
    path = Path(value)
    if path.suffix.lower() not in suffixes:
        raise InvalidOptionError(f'Output must use one of these extensions: {", ".join(suffixes)}')
    if path.is_dir():
        raise InvalidOptionError(f'Output is a directory: {path}')
    return path


def options_dict(options) -> dict:
    if options is None:
        return {}
    if not isinstance(options, Mapping) or any(not isinstance(k, str) for k in options):
        raise InvalidOptionError('options must be a mapping with string keys.')
    return dict(options)


def emit(callback, engine, stage, **extra):
    if callback is not None:
        callback(ProgressEvent(stage=stage, engine=engine, extra=extra))


def run_sync(async_method, *args, **kwargs):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(async_method(*args, **kwargs))
    raise TTSError(f'An event loop is already running. Use await {async_method.__name__}(...) instead.')


@contextmanager
def atomic_output(path: Path):
    """Publish only a completed file, preserving any existing output on failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.vocal-tts-', dir=path.parent) as directory:
        temporary = Path(directory) / ('audio' + path.suffix.lower())
        yield temporary
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise TTSError('TTS produced no audio data.')
        temporary.replace(path)
