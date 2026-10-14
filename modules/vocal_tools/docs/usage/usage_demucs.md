# Demucs Usage

Demucs models are package-managed. `vocal_tools` stores a catalog card but does not store the Demucs model file itself.

```python
from vocal_tools import ModelCatalog, Separator

catalog = ModelCatalog(model_dir="test_models")
separator = Separator(catalog=catalog)

result = separator.separate(
    input_audio="input.wav",
    output_dir="output/demucs",
    model_id="htdemucs",
    options={
        "device": "cuda",
        "shifts": 1,
        "overlap": 0.25,
    },
)

print(result.to_dict())
```

## Builtin Demucs Models

| Model id | Stems | Managed by |
| --- | --- | --- |
| `htdemucs` | 4 stems: drums, bass, other, vocals | `demucs` package |
| `htdemucs_6s` | 6 stems: drums, bass, other, vocals, guitar, piano | `demucs` package |

Model cards include:

- `managed_by`: `demucs`
- `managed_by_package`: `true`
- `download_status`: `managed_by_package`
- tags such as `4-stem`, `6-stem`, and `package-managed`

## Two Stems

Demucs `two_stems` can be used to request one source plus its inverse.

```python
result = separator.separate(
    input_audio="input.wav",
    output_dir="output/demucs_two_stems",
    model_id="htdemucs",
    options={
        "two_stems": "vocals",
        "device": "cuda",
    },
)
```

The result stems naturally include names such as:

```text
vocals
no_vocals
```

The raw Demucs command and collected output files are stored in `result.metadata`.
