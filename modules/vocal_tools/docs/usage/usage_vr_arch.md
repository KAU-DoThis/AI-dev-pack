# VR Arch Adapter Usage

VR Arch support is currently an adapter surface. `Separator` can route `arch="vr_arch"` models, but
`vocal_tools` does not bundle a VR Architecture runner yet.

## Builtin Model Card

The builtin manifest exposes:

- `vr_arch_vocal_default`

This card is useful for catalog/UI discovery and for keeping the final `Separator` API stable while the
backend is selected later.

## external_cli Backend

Use `backend="external_cli"` and pass a command template as a list of arguments:

```python
from vocal_tools import ModelCatalog, Separator

catalog = ModelCatalog(model_dir="models")
separator = Separator(catalog=catalog)

result = separator.separate(
    input_audio="input.wav",
    output_dir="output/vr_arch",
    model_id="vr_arch_vocal_default",
    options={
        "backend": "external_cli",
        "command": [
            "python",
            "-m",
            "my_vr_runner",
            "--input",
            "{input_audio}",
            "--output-dir",
            "{output_dir}",
            "--device",
            "{device}",
        ],
    },
)
```

Supported command placeholders include:

- `{input_audio}`
- `{output_dir}`
- `{model_path}`
- `{model_id}`
- `{model_name}`
- `{input_stem}`
- `{primary_stem}`
- `{secondary_stem}`
- `{device}` / `{selected_device}`
- `{requested_device}`

The adapter searches the output directory recursively for expected stems from the model card, preferring
`.wav`, then `.flac`, then `.mp3`.

## Current Limits

- `backend="python"` raises `UnsupportedEngineError`.
- No VR model files are downloaded by `ModelStore`.
- Long inference verification is intentionally deferred to integration testing.
