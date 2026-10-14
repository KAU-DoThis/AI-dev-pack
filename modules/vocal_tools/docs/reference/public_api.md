# Public API

The stable top-level import surface is available from `vocal_tools`. The package keeps `import vocal_tools` lightweight; engine-heavy dependencies are loaded when the corresponding runtime path is used.

## Catalog

```python
from vocal_tools import ModelCatalog, ModelStore, RVCVoiceCatalog
```

| Name | Purpose |
| --- | --- |
| `ModelCatalog` | Loads builtin, user, and remote separation model manifests |
| `ModelStore` | Resolves, verifies, downloads, and reports model file status |
| `RVCVoiceCatalog` | Scans local RVC voice model directories and `.pth` files |

## Core Types

```python
from vocal_tools import (
    OptionSpec,
    SeparationModelSpec,
    SeparationRequest,
    SeparationResult,
    ProgressEvent,
)
```

These dataclasses define model metadata, separation requests/results, option schemas, and progress events.

## Separation

```python
from vocal_tools import Separator
```

`Separator` routes a catalog model to the correct engine by `arch`.

Supported arch values in the current manifest set:

| Arch | Status |
| --- | --- |
| `mdx_onnx` | Implemented |
| `demucs` | Implemented through Demucs CLI/runtime |
| `vr_arch` | Adapter slot with `external_cli` support |
| `mdxc` | Adapter slot with `external_cli` support |

## RVC

```python
from vocal_tools import RVCConverter, RVCConversionOptions, RVCVoiceModelPaths
```

| Name | Purpose |
| --- | --- |
| `RVCConverter` | Loads Hubert/RVC models and performs conversion |
| `RVCConversionOptions` | Conversion option dataclass |
| `RVCVoiceModelPaths` | Resolved `.pth` and optional `.index` paths |

## Text to speech

```python
from vocal_tools import TTS, EdgeTTS, Qwen3TTS, TTSResult, TTSError, TTSDependencyError
from vocal_tools import run_tts_payload, run_tts_payload_async, validate_tts_payload
```

Both engines expose `synthesize(text, output_path, ...)` and `synthesize_async`,
return `TTSResult`, and emit `ProgressEvent`. `TTS(engine="edge" | "qwen3", **config)`
is the common entry point. See [the TTS guide](../usage/usage_tts.md) for installation,
reference audio, reusable instances, payloads, and CLI examples.

## Mixing

```python
from vocal_tools import AudioMixOptions, AudioMixResult, mix_vocal_with_instrumental
```

`mix_vocal_with_instrumental()` aligns sample rate/channels, applies gains, normalizes, and writes a WAV mix.

## Pipelines

```python
from vocal_tools import CoverPipelineOptions, CoverPipelineResult, run_cover_pipeline
```

`run_cover_pipeline()` performs the headless MDX -> RVC -> Mix flow. Optional dereverb and karaoke/main split passes are controlled through `CoverPipelineOptions`.

## Task Helpers

```python
from vocal_tools import (
    run_audio_separate_payload,
    run_audio_rvc_convert_payload,
    run_audio_cover_pipeline_payload,
    list_audio_models_payload,
    list_rvc_voice_models_payload,
)
```

Task helpers accept plain dictionaries and return JSON-safe dictionaries. They are the recommended boundary for host services.

## Validation

```python
from vocal_tools import (
    validate_audio_separate_payload,
    validate_audio_rvc_convert_payload,
    validate_audio_cover_pipeline_payload,
)
```

Validation helpers normalize path-like values to strings and raise `ValueError` for missing or malformed payload fields.

## Core Utility API

The CLI option parser is exported from `vocal_tools.core`:

```python
from vocal_tools.core import parse_key_value_options
```

It parses repeated `key=value` strings into a dictionary with booleans, `None`, ints, and floats coerced.

## Compatibility Imports

These older direct MDX names remain exported to avoid breaking existing users:

```python
from vocal_tools import MDXModelNames, MDXSeparationResult, MDXSeparator
```

New code should prefer `ModelCatalog + Separator`, because it works across MDX, Demucs, VR Arch adapter slots, and MDXC adapter slots.

`vocal_tools.rvc_catalog` is also a compatibility module:

```python
from vocal_tools.rvc_catalog import RVCVoiceCatalog
```

Internally, the implementation lives in `vocal_tools.catalog.voice_catalog`.

## Internal Modules

The following modules are implementation details and should not be treated as stable public API:

- `vocal_tools.engines.*`
- `vocal_tools.infer_pack.*`
- `vocal_tools.vc_infer_pipeline`
- `vocal_tools.rmvpe`
