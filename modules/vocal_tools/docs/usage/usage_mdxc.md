# MDXC Adapter Usage

MDXC/Roformer support is currently an adapter surface. `Separator` can route `arch="mdxc"` models, but
`vocal_tools` does not bundle an MDXC or Roformer runner yet.

## Builtin Model Card

The builtin manifest exposes:

- `mdxc_roformer_default`

This card lets catalog/UI layers show the planned capability and option schema before a bundled backend is
selected.

## external_cli Backend

Use `backend="external_cli"` and pass a command template as a list of arguments:

```python
from vocal_tools import ModelCatalog, Separator

catalog = ModelCatalog(model_dir="models")
separator = Separator(catalog=catalog)

result = separator.separate(
    input_audio="input.wav",
    output_dir="output/mdxc",
    model_id="mdxc_roformer_default",
    options={
        "backend": "external_cli",
        "model_config_path": "models/mdxc/config.yaml",
        "checkpoint_path": "models/mdxc/checkpoint.ckpt",
        "command": [
            "python",
            "-m",
            "my_mdxc_runner",
            "--input",
            "{input_audio}",
            "--output-dir",
            "{output_dir}",
            "--config",
            "{model_config_path}",
            "--checkpoint",
            "{checkpoint_path}",
            "--device",
            "{device}",
        ],
    },
)
```

Supported command placeholders include:

- `{input_audio}`
- `{output_dir}`
- `{model_config_path}`
- `{model_path}`
- `{checkpoint_path}`
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
- No MDXC model files are downloaded by `ModelStore`.
- Long inference verification is intentionally deferred to integration testing.
