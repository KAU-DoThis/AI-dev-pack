from __future__ import annotations

import asyncio
from collections import OrderedDict
from pathlib import Path
from threading import RLock

from vocal_tools.core.errors import InvalidOptionError, TTSDependencyError, TTSError
from ._common import TTSResult, atomic_output, emit, options_dict, output_path, validate_text


DEFAULT_QWEN3_TTS_MODEL = 'Qwen/Qwen3-TTS-12Hz-0.6B-Base'
_LANGUAGES = {'auto': 'Auto', 'zh': 'Chinese', 'en': 'English', 'ja': 'Japanese',
              'ko': 'Korean', 'de': 'German', 'fr': 'French', 'ru': 'Russian',
              'pt': 'Portuguese', 'es': 'Spanish', 'it': 'Italian'}
_DTYPES = {'auto', 'float32', 'float16', 'bfloat16'}


class Qwen3TTS:
    """Lazy, reusable Qwen3-TTS Base voice cloning with a bounded reference cache.

    Calls on one instance are serialized. Model IDs download into the Hugging Face
    cache on first use; a local model directory or local_files_only=True works offline.
    """

    def __init__(self, model=DEFAULT_QWEN3_TTS_MODEL, *, device='auto', dtype='auto',
                 attn_implementation='sdpa', cache_dir=None, local_files_only=False,
                 reference_cache_size=8):
        if not isinstance(model, (str, Path)) or not str(model).strip():
            raise InvalidOptionError('model must be a Qwen3-TTS Base model ID or local directory.')
        if any(part in str(model) for part in ('CustomVoice', 'VoiceDesign')):
            raise InvalidOptionError('Reference voice TTS requires a Qwen3-TTS Base model, not CustomVoice or VoiceDesign.')
        if not isinstance(dtype, str) or dtype not in _DTYPES:
            raise InvalidOptionError(f'dtype must be one of: {", ".join(sorted(_DTYPES))}')
        if not isinstance(device, str) or not device.strip():
            raise InvalidOptionError('device must be auto, cpu, cuda:0, or another PyTorch device string.')
        if attn_implementation not in ('sdpa', 'eager', 'flash_attention_2'):
            raise InvalidOptionError('attn_implementation must be sdpa, eager, or flash_attention_2.')
        if not isinstance(local_files_only, bool):
            raise InvalidOptionError('local_files_only must be a boolean.')
        if isinstance(reference_cache_size, bool) or not isinstance(reference_cache_size, int) or reference_cache_size < 0:
            raise InvalidOptionError('reference_cache_size must be a non-negative integer.')
        self.model = str(model)
        self.device = device
        self.dtype = dtype
        self.attn_implementation = attn_implementation
        self.cache_dir = str(cache_dir) if cache_dir is not None else None
        self.local_files_only = local_files_only
        self.reference_cache_size = reference_cache_size
        self._model = None
        self._references = OrderedDict()
        self._lock = RLock()
        self._runtime_settings = {}

    def synthesize(self, text, output_path, *, reference_audio, reference_text=None,
                   language='Auto', x_vector_only_mode=None, options=None, progress_callback=None) -> TTSResult:
        text = validate_text(text)
        path = _output_path(output_path)
        if not isinstance(reference_audio, (str, Path)) or not str(reference_audio).strip():
            raise InvalidOptionError('reference_audio must be a local audio file path.')
        reference = Path(reference_audio).resolve()
        if not reference.is_file():
            raise FileNotFoundError(f'Reference audio not found: {reference}')
        if reference == path.resolve():
            raise InvalidOptionError('output_path must differ from reference_audio.')
        if reference_text is not None and not isinstance(reference_text, str):
            raise InvalidOptionError('reference_text must be a string or None.')
        transcript = reference_text.strip() if reference_text else None
        if x_vector_only_mode is not None and not isinstance(x_vector_only_mode, bool):
            raise InvalidOptionError('x_vector_only_mode must be a boolean or None.')
        vector_only = not bool(transcript) if x_vector_only_mode is None else x_vector_only_mode
        if not vector_only and not transcript:
            raise InvalidOptionError('reference_text is required unless x_vector_only_mode=True.')
        if not isinstance(language, str) or not language.strip():
            raise InvalidOptionError('language must be a non-empty string, for example Korean or ko.')
        language = _LANGUAGES.get(language.strip().lower(), language.strip())
        generation = options_dict(options)
        reserved = generation.keys() & {'text', 'language', 'ref_audio', 'ref_text', 'x_vector_only_mode', 'voice_clone_prompt'}
        if reserved:
            raise InvalidOptionError(f'Pass {", ".join(sorted(reserved))} as synthesis arguments, not options.')
        if 'max_new_tokens' in generation and (isinstance(generation['max_new_tokens'], bool)
                or not isinstance(generation['max_new_tokens'], int) or generation['max_new_tokens'] <= 0):
            raise InvalidOptionError('max_new_tokens must be a positive integer.')
        emit(progress_callback, 'qwen3', 'start', model=self.model)
        with self._lock:
            try:
                self._ensure_model(progress_callback)
                import numpy as np
                import soundfile as sf

                stat = reference.stat()
                key = (str(reference), stat.st_mtime_ns, stat.st_size, transcript, vector_only)
                cached = key in self._references
                emit(progress_callback, 'qwen3', 'reference', cached=cached)
                if cached:
                    prompt = self._references[key]
                    self._references.move_to_end(key)
                else:
                    prompt = self._model.create_voice_clone_prompt(ref_audio=str(reference),
                        ref_text=transcript, x_vector_only_mode=vector_only)
                    if self.reference_cache_size:
                        self._references[key] = prompt
                        while len(self._references) > self.reference_cache_size:
                            self._references.popitem(last=False)
                emit(progress_callback, 'qwen3', 'synthesis')
                wavs, sample_rate = self._model.generate_voice_clone(text=text, language=language,
                    voice_clone_prompt=prompt, **generation)
                if len(wavs) != 1:
                    raise TTSError('Expected one waveform for one input text.')
                waveform = np.asarray(wavs[0])
                if waveform.ndim != 1 or not waveform.size or not np.isfinite(waveform).all() or sample_rate <= 0:
                    raise TTSError('Qwen3-TTS returned invalid or empty audio.')
                with atomic_output(path) as temporary:
                    sf.write(str(temporary), waveform, int(sample_rate), format='WAV', subtype='PCM_16')
                result = TTSResult(path, 'qwen3', int(sample_rate), 1, len(waveform) / int(sample_rate),
                    model=self.model, options_used=generation,
                    metadata={'language': language, 'reference_audio': str(reference),
                              'x_vector_only_mode': vector_only, 'reference_cached': cached,
                              **self._runtime_settings})
            except TTSError:
                raise
            except Exception as exc:
                raise TTSError(f'Qwen3-TTS synthesis failed: {exc}') from exc
        emit(progress_callback, 'qwen3', 'complete', output_path=str(path))
        return result

    async def synthesize_async(self, text, output_path, **kwargs) -> TTSResult:
        """Run blocking inference in a worker thread. Callbacks run in that thread."""
        return await asyncio.to_thread(self.synthesize, text, output_path, **kwargs)

    def _ensure_model(self, callback):
        if self._model is not None:
            return
        try:
            import torch
            from qwen_tts import Qwen3TTSModel
            from huggingface_hub import snapshot_download
        except ImportError as exc:
            raise TTSDependencyError('Install Qwen3-TTS support with: pip install "vocal-tools[qwen-tts]"') from exc
        device = self.device
        if device == 'auto':
            device = 'cuda:0' if torch.cuda.is_available() else 'cpu'
        dtype = self.dtype
        if dtype == 'auto':
            dtype = ('bfloat16' if torch.cuda.is_bf16_supported() else 'float16') if device.startswith('cuda') else 'float32'
        emit(callback, 'qwen3', 'load', model=self.model, device=device)
        local = Path(self.model).expanduser()
        if local.is_dir():
            source = str(local.resolve())
        elif local.is_absolute() or str(self.model).startswith(('.', '~')) or '\\' in self.model:
            raise TTSError(f'Qwen3-TTS model directory not found: {local}')
        else:
            # Resolve to a snapshot first: upstream processor loading does not
            # forward cache_dir/local_files_only from from_pretrained kwargs.
            source = snapshot_download(repo_id=self.model, cache_dir=self.cache_dir,
                                       local_files_only=self.local_files_only)
        model = Qwen3TTSModel.from_pretrained(source, device_map=device, dtype=getattr(torch, dtype),
                                            attn_implementation=self.attn_implementation)
        model_type = getattr(getattr(model, 'model', None), 'tts_model_type', None)
        if model_type is not None and model_type != 'base':
            raise TTSError(f'Voice cloning requires a Base model; loaded model type is {model_type!r}.')
        self._model = model
        self._runtime_settings = {'device': device, 'dtype': dtype}

    def clear_reference_cache(self):
        with self._lock:
            self._references.clear()

    def close(self):
        """Release owned model/prompt references. A later call reloads the model."""
        with self._lock:
            self._references.clear()
            self._model = None
            self._runtime_settings = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _output_path(value):
    return output_path(value, ('.wav',))
