from __future__ import annotations

import asyncio
from pathlib import Path
import re
import shutil
import subprocess
import wave

from vocal_tools.core.errors import InvalidOptionError, TTSDependencyError, TTSError
from ._common import TTSResult, atomic_output, emit, options_dict, output_path, run_sync, validate_text


class EdgeTTS:
    """Microsoft Edge online TTS. Instances are cheap; no model download is needed."""

    def __init__(self, voice='ko-KR-SunHiNeural', *, proxy=None, connect_timeout=10, receive_timeout=60):
        if not isinstance(voice, str) or not voice.strip():
            raise InvalidOptionError('voice must be a non-empty Edge voice ID.')
        for name, value in [('connect_timeout', connect_timeout), ('receive_timeout', receive_timeout)]:
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise InvalidOptionError(f'{name} must be a positive integer.')
        self.voice = voice
        self.proxy = proxy
        self.connect_timeout = connect_timeout
        self.receive_timeout = receive_timeout

    def synthesize(self, text, output_path, *, voice=None, options=None, progress_callback=None) -> TTSResult:
        return run_sync(self.synthesize_async, text, output_path, voice=voice,
                        options=options, progress_callback=progress_callback)

    async def synthesize_async(self, text, output_path, *, voice=None, options=None, progress_callback=None) -> TTSResult:
        text = validate_text(text)
        path = _output_path(output_path)
        voice = self.voice if voice is None else voice
        if not isinstance(voice, str) or not voice.strip():
            raise InvalidOptionError('voice must be a non-empty Edge voice ID.')
        settings = {'rate': '+0%', 'volume': '+0%', 'pitch': '+0Hz'}
        overrides = options_dict(options)
        unknown = overrides.keys() - settings.keys()
        if unknown:
            raise InvalidOptionError(f'Unknown Edge TTS option(s): {", ".join(sorted(unknown))}')
        settings.update(overrides)
        for name, value in settings.items():
            pattern = r'[+-]\d+Hz' if name == 'pitch' else r'[+-]\d+%'
            if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
                raise InvalidOptionError(f'{name} must use a signed value such as {"+0Hz" if name == "pitch" else "+0%"}.')
        ffmpeg = shutil.which('ffmpeg') if path.suffix.lower() == '.wav' else None
        if path.suffix.lower() == '.wav' and not ffmpeg:
            raise TTSDependencyError('Edge WAV output requires ffmpeg on PATH. Use .mp3 for native output.')
        edge_tts = _runtime()
        emit(progress_callback, 'edge', 'start', voice=voice)
        try:
            with atomic_output(path) as temporary:
                mp3 = temporary if path.suffix.lower() == '.mp3' else temporary.with_suffix('.mp3')
                communicate = edge_tts.Communicate(text, voice, **settings, proxy=self.proxy,
                    connect_timeout=self.connect_timeout, receive_timeout=self.receive_timeout)
                await communicate.save(str(mp3))
                if not mp3.is_file() or mp3.stat().st_size == 0:
                    raise TTSError('Edge TTS returned no audio data.')
                duration = None
                sample_rate, channels = 24000, 1  # Edge's native MP3 output format.
                if ffmpeg:
                    await asyncio.to_thread(_decode_wav, ffmpeg, mp3, temporary)
                    with wave.open(str(temporary)) as audio:
                        sample_rate, channels = audio.getframerate(), audio.getnchannels()
                        duration = audio.getnframes() / sample_rate
        except TTSError:
            raise
        except Exception as exc:
            raise TTSError(f'Edge TTS synthesis failed: {exc}') from exc
        result = TTSResult(path, 'edge', sample_rate, channels, duration,
                           voice=voice, options_used=settings, metadata={'online': True})
        emit(progress_callback, 'edge', 'complete', output_path=str(path))
        return result

    def list_voices(self, *, locale=None) -> list[dict]:
        return run_sync(self.list_voices_async, locale=locale)

    async def list_voices_async(self, *, locale=None) -> list[dict]:
        runtime = _runtime()
        try:
            voices = await asyncio.wait_for(runtime.list_voices(proxy=self.proxy),
                                            timeout=self.connect_timeout + self.receive_timeout)
        except Exception as exc:
            raise TTSError(f'Could not list Edge TTS voices: {exc}') from exc
        return [{'id': v['ShortName'], 'name': v.get('FriendlyName', v['ShortName']),
                 'locale': v.get('Locale'), 'gender': v.get('Gender')}
                for v in voices if locale is None or str(v.get('Locale', '')).lower() == locale.lower()]

    def close(self):
        """No persistent network session is held by this wrapper."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _output_path(value):
    return output_path(value, ('.mp3', '.wav'))


def _runtime():
    try:
        import edge_tts
    except ImportError as exc:
        raise TTSDependencyError('Install Edge TTS support with: pip install "vocal-tools[edge-tts]"') from exc
    return edge_tts


def _decode_wav(ffmpeg, source: Path, target: Path):
    result = subprocess.run([ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
                             '-i', str(source), '-c:a', 'pcm_s16le', str(target)],
                            capture_output=True, text=True, errors='replace', timeout=120)
    if result.returncode:
        raise TTSError(f'Could not convert Edge output to WAV: {result.stderr.strip()}')
