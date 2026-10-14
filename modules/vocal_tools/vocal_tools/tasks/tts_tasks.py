from __future__ import annotations

from vocal_tools.core.errors import InvalidOptionError
from vocal_tools.tts import TTS
from vocal_tools.tts._common import options_dict, validate_text


_ARGUMENTS = {
    'edge': {'voice'},
    'qwen3': {'reference_audio', 'reference_text', 'language', 'x_vector_only_mode'},
}
_CONFIG = {
    'edge': {'voice', 'proxy', 'connect_timeout', 'receive_timeout'},
    'qwen3': {'model', 'device', 'dtype', 'attn_implementation', 'cache_dir',
              'local_files_only', 'reference_cache_size'},
}


def validate_tts_payload(payload: dict) -> dict:
    if not isinstance(payload, dict) or any(not isinstance(key, str) for key in payload):
        raise InvalidOptionError('TTS payload must be a dict with string keys.')
    data = dict(payload)
    engine = data.get('engine', 'edge')
    if not isinstance(engine, str) or engine not in _ARGUMENTS:
        raise InvalidOptionError('TTS engine must be edge or qwen3.')
    unknown = data.keys() - {'engine', 'text', 'output_path', 'options', 'config'} - _ARGUMENTS[engine]
    if unknown:
        raise InvalidOptionError(f'Unknown {engine} TTS payload field(s): {", ".join(sorted(unknown))}')
    validate_text(data.get('text'))
    if not data.get('output_path'):
        raise InvalidOptionError('Missing required TTS payload field: output_path')
    if engine == 'qwen3' and not data.get('reference_audio'):
        raise InvalidOptionError('Qwen3-TTS requires reference_audio.')
    data['engine'] = engine
    data['config'] = options_dict(data.get('config'))
    data['options'] = options_dict(data.get('options'))
    unknown_config = data['config'].keys() - _CONFIG[engine]
    if unknown_config:
        raise InvalidOptionError(f'Unknown {engine} TTS config field(s): {", ".join(sorted(unknown_config))}')
    return data


def _prepare(payload, synthesizer):
    data = validate_tts_payload(payload)
    if synthesizer is not None:
        if not isinstance(synthesizer, TTS) or synthesizer.engine != data['engine']:
            raise InvalidOptionError('synthesizer must be a TTS instance with the same engine as the payload.')
        if data['config']:
            raise InvalidOptionError('Configure a reused synthesizer at construction; omit payload.config.')
    else:
        synthesizer = TTS(data['engine'], **data['config'])
    kwargs = {name: data[name] for name in _ARGUMENTS[data['engine']] if name in data}
    kwargs['options'] = data['options']
    return data, synthesizer, kwargs


def run_tts_payload(payload: dict, *, synthesizer: TTS | None = None, progress_callback=None) -> dict:
    """JSON-safe TTS. Supply a persistent TTS instance to reuse Qwen model caches."""
    owned = synthesizer is None
    data, synthesizer, kwargs = _prepare(payload, synthesizer)
    try:
        return synthesizer.synthesize(data['text'], data['output_path'],
            progress_callback=progress_callback, **kwargs).to_dict()
    finally:
        if owned:
            synthesizer.close()


async def run_tts_payload_async(payload: dict, *, synthesizer: TTS | None = None, progress_callback=None) -> dict:
    owned = synthesizer is None
    data, synthesizer, kwargs = _prepare(payload, synthesizer)
    try:
        return (await synthesizer.synthesize_async(data['text'], data['output_path'],
            progress_callback=progress_callback, **kwargs)).to_dict()
    finally:
        if owned:
            # Qwen close takes a lock and may wait for ongoing GPU work after cancellation.
            import asyncio
            await asyncio.to_thread(synthesizer.close)
