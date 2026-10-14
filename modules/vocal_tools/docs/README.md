# vocal_tools Documentation

`vocal_tools` is a headless Python audio engine library for model catalogs,
source separation, RVC conversion, text-to-speech, audio mixing, and reusable
cover-pipeline helpers. Host applications own their API server, UI, database,
job queue, and deployment-specific integration.

## Documents

### Project Overview

- [Project summary](overview/project_summary.md)
- [Current implementation status](overview/current_status.md)
- [Architecture](overview/architecture.md)
- [Roadmap](overview/roadmap.md)

### Guides

- [Installation and optional dependencies](guides/installation.md)
- [Command-line interface](guides/cli.md)
- [Examples](guides/examples.md)

### Usage

- [Model catalog](usage/usage_catalog.md)
- [Cover pipeline](usage/usage_cover_pipeline.md)
- [Demucs](usage/usage_demucs.md)
- [MDX](usage/usage_mdx.md)
- [MDXC adapter](usage/usage_mdxc.md)
- [RVC conversion](usage/usage_rvc.md)
- [RVC voice catalog](usage/usage_rvc_voice_catalog.md)
- [Text-to-speech](usage/usage_tts.md)
- [VR Arch adapter](usage/usage_vr_arch.md)

### Reference

- [Public API](reference/public_api.md)
- [Model directory layout](reference/model_layout.md)
- [Model manifest format](reference/model_manifest_format.md)
- [Task payload helpers](reference/task_payload_helpers.md)

### Development

- [Testing](development/testing.md)
- [Release checklist](development/release_checklist.md)

### Integrations

- [SHS GPU Node integration boundary](integrations/shs_gpu_node_integration.md)
- [SHS GPU Node RVC/MDX integration manual](integrations/shs_gpu_node_vc_mdx_manual.md)

## Quick Example

```python
from vocal_tools import ModelCatalog, Separator

catalog = ModelCatalog(model_dir="test_models/mdx")
separator = Separator(catalog=catalog)

result = separator.separate(
    input_audio="input.wav",
    output_dir="output/mdx",
    model_id="kim_vocal_2",
    options={"denoise": False},
)

print(result.to_dict())
```

## CLI Example

After `python -m pip install -e .`, use the console script:

```powershell
vocal-tools models list --models-dir test_models
vocal-tools voices list --models-dir test_models
```

During development, the module entrypoint also works:

```powershell
python -m vocal_tools.cli --help
```

## Current Scope

Implemented:

- MDX ONNX separation through `ModelCatalog`, `Separator`, and `MDXOnnxEngine`
- Demucs separation through `DemucsEngine`
- RVC conversion through `RVCConverter`
- Edge / Qwen3-TTS synthesis through `TTS`, `EdgeTTS`, and `Qwen3TTS`
- Audio mixing through `mix_vocal_with_instrumental()`
- Cover pipeline through `run_cover_pipeline()`
- JSON-safe task helper functions under `vocal_tools.tasks`
- Thin `vocal-tools` CLI wrapper around task helpers
- Standard-library smoke tests under `tests/`
- Builtin, user, and remote manifest loading
- Model download status APIs through `ModelStore`
- RVC voice model scanning through `RVCVoiceCatalog`

Not implemented yet:

- VR Arch runtime
- MDXC runtime
- Service layer, API server, web UI, database, or queue

## Import Boundary

`import vocal_tools` is intentionally lightweight. Heavy MDX, Demucs, and RVC runtime packages are imported when the corresponding engine or conversion path is executed.
