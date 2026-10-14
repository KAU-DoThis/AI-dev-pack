# Catalog Usage

`ModelCatalog` combines builtin, remote, and user manifests.

Priority:

```text
user > remote > builtin
```

Load the builtin catalog:

```python
from vocal_tools import ModelCatalog

catalog = ModelCatalog(model_dir="test_models/mdx")
```

## Remote Manifest

Remote manifests can be loaded from:

- `https://...`
- `http://...`
- `file://...`
- local JSON path

```python
catalog.load_remote_manifest(
    "manifests/public_models.json",
    cache_path="test_models/mdx/remote_models.cache.json",
)
```

Refresh from the configured source:

```python
catalog.refresh_remote_manifest()
```

Inspect sources:

```python
print(catalog.list_sources())
```

Example shape:

```json
[
  {"source": "builtin", "count": 7, "path": ".../builtin_models.json"},
  {"source": "remote", "count": 10, "path": ".../remote_models.cache.json"},
  {"source": "user", "count": 2, "path": ".../user_models.json"}
]
```

## Model Cards

`list_model_cards()` returns UI-friendly dictionaries:

```python
cards = catalog.list_model_cards()
```

Important fields:

- `id`
- `name`
- `arch`
- `tasks`
- `output_stems`
- `tags`
- `description`
- `source`
- `downloaded`
- `model_path`
- `managed_by`
- `defaults`
- `options`
- `ui`
- `metadata`
- `download`

If a remote manifest overrides a builtin model, card metadata includes collision information.

## Download Status

Use `ModelStore.get_download_status()` for UI and Node model management.

```python
model = catalog.get_model("kim_vocal_2")
status = catalog.store.get_download_status(model)
print(status)
```

Status values:

- `downloaded`
- `missing`
- `managed_by_package`
- `not_downloadable`
- `verification_failed`

`download_model()` downloads a model with `download_url`. `delete_model()` removes a local model file and returns `True` when a file was removed.

```python
path = catalog.store.download_model(model, overwrite=False)
removed = catalog.store.delete_model(model)
```

Package-managed models such as Demucs report `managed_by_package` and are not downloaded or deleted by `vocal_tools`.

## Register Local User Model

```python
catalog.register_local_model(
    model_id="uvr_mdxnet_voc_ft",
    name="UVR MDX-Net Voc FT",
    arch="mdx_onnx",
    filename="UVR-MDX-NET-Voc_FT.onnx",
    tasks=["vocal_separation"],
    output_stems=["vocals", "instrumental"],
    tags=["custom", "mdx", "vocal", "2-stem"],
    defaults={
        "primary_stem": "vocals",
        "secondary_stem": "instrumental",
        "sample_rate": 44100,
        "stereo": True,
    },
    options=[
        {
            "name": "denoise",
            "label": "Denoise",
            "type": "bool",
            "default": False,
        }
    ],
    overwrite=True,
    save=True,
)
```

User models have the highest priority.

## Experimental Adapter Cards

The builtin catalog includes experimental entries for VR Arch and MDXC:

- `vr_arch_vocal_default`
- `mdxc_roformer_default`

These entries have `filename: null`, `download_url: null`, and `defaults.managed_by: "external"`.
They are visible to catalog/UI layers, but no bundled model runner is shipped yet. Use an `external_cli`
command template at separation time to connect a compatible local runner. Their download status is
`managed_externally`.
