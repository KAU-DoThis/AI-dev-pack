"""TTS adapters; importing this module does not load either backend runtime."""
from vocal_tools.core.errors import InvalidOptionError
from ._common import TTSResult
from .edge import EdgeTTS
from .qwen import DEFAULT_QWEN3_TTS_MODEL, Qwen3TTS


class TTS:
    """Common entry point. Keep an instance alive to reuse Qwen model/prompt caches."""

    def __init__(self, engine='edge', **config):
        if engine == 'edge':
            self.backend = EdgeTTS(**config)
        elif engine == 'qwen3':
            self.backend = Qwen3TTS(**config)
        else:
            raise InvalidOptionError('TTS engine must be edge or qwen3.')
        self.engine = engine

    def synthesize(self, text, output_path, **kwargs) -> TTSResult:
        return self.backend.synthesize(text, output_path, **kwargs)

    async def synthesize_async(self, text, output_path, **kwargs) -> TTSResult:
        return await self.backend.synthesize_async(text, output_path, **kwargs)

    def close(self):
        self.backend.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


__all__ = ['TTS', 'TTSResult', 'EdgeTTS', 'Qwen3TTS', 'DEFAULT_QWEN3_TTS_MODEL']
