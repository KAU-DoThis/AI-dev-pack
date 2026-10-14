# Text to speech

`vocal_tools` provides Edge online TTS and local Qwen3-TTS Base reference voice
synthesis. Both use `synthesize(text, output_path, ...)`, return `TTSResult`, and
accept a callback receiving `ProgressEvent`. Imports and construction do not
download models or import the optional engine runtimes.

## Installation

```powershell
python -m pip install -e ".[edge-tts]"
python -m pip install -e ".[qwen-tts]"
# Or install both:
python -m pip install -e ".[tts]"
```

These extras add TTS dependencies; the existing base audio dependencies are still
installed. Qwen's upstream package pins Transformers/Accelerate and also depends
on Gradio. Use an isolated runtime if those conflict with your host application.
FlashAttention is optional; this wrapper defaults to PyTorch SDPA, including on
Windows. A compatible GPU PyTorch installation is needed for CUDA inference.

## Edge TTS

```python
from vocal_tools import EdgeTTS

tts = EdgeTTS(voice="ko-KR-SunHiNeural")
result = tts.synthesize(
    "안녕하세요. 음성 합성 테스트입니다.",
    "output/hello.mp3",
    options={"rate": "+0%", "volume": "+0%", "pitch": "+0Hz"},
)
print(result.output_path)
print(tts.list_voices(locale="ko-KR"))
```

Edge sends the supplied text to Microsoft's online speech service. It needs an
internet connection, but no local model or API key. MP3 is the native format.
For `.wav`, the wrapper decodes the MP3 using `ffmpeg` on PATH; it does not simply
rename the MP3. MP3 duration is `None`; WAV duration is measured from its header.
Set `proxy`, `connect_timeout`, and `receive_timeout` on the constructor if needed.

In an existing asyncio event loop, use the async API:

```python
result = await tts.synthesize_async("비동기 합성입니다.", "output/async.mp3")
voices = await tts.list_voices_async(locale="ko-KR")
```

The sync Edge methods deliberately raise an actionable error inside an active
event loop instead of nesting `asyncio.run` or blocking that loop.

## Qwen3-TTS reference voice synthesis

```python
from vocal_tools import Qwen3TTS

with Qwen3TTS(device="auto", cache_dir="models/qwen_cache") as tts:
    result = tts.synthesize(
        "이 문장은 레퍼런스의 목소리를 이용해 새로 합성합니다.",
        "output/cloned.wav",
        reference_audio="reference.wav",
        reference_text="레퍼런스 녹음에서 실제로 말한 문장입니다.",
        language="Korean",  # "ko" also works
    )
    print(result.to_dict())
```

- The default is `Qwen/Qwen3-TTS-12Hz-0.6B-Base`. Pass
  `model="Qwen/Qwen3-TTS-12Hz-1.7B-Base"` to choose the larger Base model.
- Pass a downloaded **model directory** as `model` to use local weights. A Base
  model is required; CustomVoice and VoiceDesign are different workflows.
- For a Hub model ID, the first call downloads its snapshot to `cache_dir` or
  the standard Hugging Face cache. `local_files_only=True` uses existing cached
  files only. Construction alone performs no download.
- `reference_audio` is a local audio file. `reference_text` is the transcript of
  that recording, not the new text to speak.
- Without a transcript, omit `reference_text`. The wrapper automatically enables
  `x_vector_only_mode=True` (speaker embedding only); this can reduce cloning
  quality compared with audio plus its transcript. Explicit `False` requires a
  transcript. No speech-to-text transcription is performed automatically.
- Qwen output is PCM WAV at the backend's returned sample rate.
- `options` is a dict of upstream generation options, for example
  `{"max_new_tokens": 512, "temperature": 0.8}`. Too small a token limit can cut
  speech short. Language and reference arguments belong outside `options`.
- `dtype="auto"` selects BF16/FP16 on CUDA and FP32 on CPU. You can explicitly
  select `float32`, `float16`, or `bfloat16`. The default device is CUDA if
  available, otherwise CPU.

Keep the same instance for repeated requests: it reuses the loaded model and up
to eight reference prompts. Cache entries include the file path, modification
time, size, transcript, and embedding mode. Configure `reference_cache_size`, or
call `clear_reference_cache()`. `close()` / a `with` block releases owned model
and prompt references; a later call reloads them. Calls on one instance are
serialized. PyTorch may retain freed memory in its allocator cache.

`await tts.synthesize_async(...)` runs Qwen inference in a worker thread; progress
callbacks also execute on that thread. Cancelling the await does not immediately
stop an ongoing GPU generation. There is no token/audio streaming API in this
wrapper yet. Progress reports stages, not a fabricated completion percentage.

## Common entry point and host payloads

```python
from vocal_tools import TTS, run_tts_payload

with TTS(engine="qwen3", device="auto") as tts:
    payload = {
        "engine": "qwen3",
        "text": "새로운 문장입니다.",
        "output_path": "output/voice.wav",
        "reference_audio": "reference.wav",
        "language": "ko",
        "options": {"max_new_tokens": 512},
    }
    result = run_tts_payload(payload, synthesizer=tts)
    # Reuse tts for later requests to retain its model/reference cache.
```

`TTS(engine="edge", voice="ko-KR-SunHiNeural")` also supports the same direct
`synthesize` / `synthesize_async` methods. Engine-specific keyword arguments are
the same as on `EdgeTTS` and `Qwen3TTS`.

For a one-off payload call, omit `synthesizer` and put constructor settings in
`payload["config"]`; the helper closes the instance when done. When supplying a
reusable `TTS` instance, configure it at construction and omit `config`.
`run_tts_payload_async` provides the async counterpart. Payloads use the exact
same `text`, `output_path`, `reference_audio`, `reference_text`, `voice`, and
`options` names as direct calls. Unknown fields are rejected rather than ignored.

All results provide `output_path`, `engine`, `model`, `voice`, `sample_rate`,
`channels`, `duration_sec`, `options_used`, and `metadata`; unavailable fields
are `None`. `to_dict()` converts the output path to a JSON-safe string. Invalid
input uses `InvalidOptionError` / `FileNotFoundError`; missing runtime components
use `TTSDependencyError`; synthesis errors use `TTSError`. A failed synthesis
preserves an existing output file and removes its temporary output.

## CLI

```powershell
vocal-tools tts --engine edge --text "안녕하세요" --output output/hello.mp3
vocal-tools tts --engine qwen3 --text "새로운 문장입니다" --reference-audio reference.wav --reference-text "원본 녹음의 대사입니다" --language ko --output output/clone.wav
```

Use `--model`, `--device`, `--cache-dir`, `--local-files-only`, repeated
`--option KEY=VALUE`, and `--json` as needed. `python -m vocal_tools.cli tts --help`
shows all flags. The CLI calls the same payload helper as host projects.

## Upstream references

- [Edge TTS](https://github.com/rany2/edge-tts)
- [Qwen3-TTS voice cloning](https://github.com/QwenLM/Qwen3-TTS#voice-clone)

The default automated suite uses fake runtimes to exercise contracts, output
preservation, cache reuse, payload validation, and lazy imports without network
or model downloads. Use `examples/tts_usage.py` explicitly for real synthesis.

## Local verification (2026-10-10)

Verified on Windows / Python 3.10 with an RTX 4080 SUPER using edge-tts 7.2.8 and
qwen-tts 0.1.1 in a separate TTS test environment:

- 105 automated tests passed, including the pre-existing separation/RVC tests.
- Edge Korean voice listing and an 8.04-second, 24 kHz mono WAV succeeded.
- Qwen 0.6B Base generated a 6-second WAV from that synthetic reference and its
  transcript; a second call reused the reference prompt cache.
- Reference-only synthesis without a transcript produced a 4.16-second WAV with
  Hugging Face and Transformers offline mode enabled. CLI stdout parsed as JSON.
- Wheel build, installation without dependencies into an empty environment, and
  all public TTS imports succeeded without loading optional runtime modules.

This checks integration and audio file validity, not a quantitative voice
similarity or pronunciation benchmark. The 1.7B variant was not run in this check.
