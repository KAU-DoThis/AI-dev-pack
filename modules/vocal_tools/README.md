# vocal_tools

`vocal_tools` is a headless Python audio engine package for source separation, RVC conversion, Edge/Qwen3 text-to-speech, audio mixing, model catalogs, and reusable cover-pipeline helpers.

It is meant to be imported by other Python projects. It does not include a FastAPI server, web UI, database, or job queue.

## Documentation

- Main docs: [docs/README.md](docs/README.md)
- Project summary: [docs/overview/project_summary.md](docs/overview/project_summary.md)
- CLI usage: [docs/guides/cli.md](docs/guides/cli.md)
- Text to speech (Edge / Qwen3): [docs/usage/usage_tts.md](docs/usage/usage_tts.md)
- Release checklist: [docs/development/release_checklist.md](docs/development/release_checklist.md)

## Quick Install

```powershell
python -m pip install -e .
```

## Quick CLI Checks

```powershell
vocal-tools models list --models-dir test_models
vocal-tools voices list --models-dir test_models
python -m vocal_tools.cli --help
```

## Text to Speech

Install the required backend with `pip install -e ".[edge-tts]"` or
`pip install -e ".[qwen-tts]"` (both: `.[tts]`).

```python
from vocal_tools import EdgeTTS, Qwen3TTS

EdgeTTS().synthesize("안녕하세요.", "output/hello.mp3")

with Qwen3TTS() as tts:
    result = tts.synthesize(
        "새로운 문장을 읽어 주세요.", "output/cloned.wav",
        reference_audio="reference.wav", language="ko",
        # Add reference_text="the recording's transcript" when available.
    )
    print(result.output_path)
```

Edge needs internet; Qwen downloads its Base model on first use and then runs
locally. See [the TTS guide](docs/usage/usage_tts.md) for offline models, async calls,
reference transcripts, reusable instances, and task payloads.
