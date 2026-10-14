# Model Manifest Format

`vocal_tools` reads model metadata from manifest JSON files. The manifest is a list of model entries or an object with a `models` list.

Supported separation model sources:

- `builtin`: packaged with `vocal_tools`
- `remote`: loaded from a URL, `file://` URL, or local JSON file
- `user`: local user manifest

Priority:

```text
user > remote > builtin
```

If the same `id` appears in more than one source, the highest-priority entry wins. `list_model_cards()` includes source and conflict metadata so UI layers can show where a model card came from.

## MDX Entry

```json
{
  "id": "kim_vocal_2",
  "name": "Kim Vocal 2",
  "arch": "mdx_onnx",
  "filename": "Kim_Vocal_2.onnx",
  "tasks": ["vocal_separation"],
  "output_stems": ["vocals", "instrumental"],
  "tags": ["mdx", "vocal", "2-stem"],
  "description": "Default MDX vocal separation model.",
  "download_url": null,
  "sha256": null,
  "file_size": null,
  "defaults": {
    "primary_stem": "vocals",
    "secondary_stem": "instrumental",
    "sample_rate": 44100,
    "stereo": true
  },
  "options": [],
  "ui": {
    "category": "MDX",
    "recommended": true,
    "sort_order": 100
  },
  "metadata": {}
}
```

## Demucs Package-Managed Entry

```json
{
  "id": "htdemucs",
  "name": "HTDemucs 4 Stems",
  "arch": "demucs",
  "filename": null,
  "tasks": ["music_source_separation"],
  "output_stems": ["drums", "bass", "other", "vocals"],
  "tags": ["demucs", "4-stem", "package-managed"],
  "description": "Demucs package-managed 4-stem model.",
  "defaults": {
    "model_name": "htdemucs",
    "device": "auto",
    "output_format": "wav",
    "managed_by": "demucs"
  },
  "options": [],
  "ui": {
    "category": "Demucs 4-stem",
    "recommended": true,
    "sort_order": 200,
    "warning": "Managed by the demucs package cache."
  },
  "metadata": {
    "managed_by_package": true
  }
}
```

`filename: null` plus `defaults.managed_by` means `vocal_tools` does not download or verify a local file. A package or external runtime owns model availability.

## VR Arch Adapter Entry

```json
{
  "id": "vr_arch_vocal_default",
  "name": "VR Arch Vocal Default",
  "arch": "vr_arch",
  "filename": null,
  "tasks": ["vocal_separation"],
  "output_stems": ["vocals", "instrumental"],
  "tags": ["vr_arch", "vocal", "2-stem"],
  "download_url": null,
  "defaults": {
    "managed_by": "external",
    "backend": "auto",
    "device": "auto"
  },
  "options": [],
  "ui": {
    "category": "VR Arch",
    "recommended": false,
    "warning": "Backend/model runner is not bundled yet."
  },
  "metadata": {
    "status": "experimental"
  }
}
```

Use `backend="external_cli"` plus `options.command` to call a compatible runner. `python` backend support is reserved for a future concrete implementation.

## MDXC Adapter Entry

```json
{
  "id": "mdxc_roformer_default",
  "name": "MDXC / Roformer Default",
  "arch": "mdxc",
  "filename": null,
  "tasks": ["music_source_separation"],
  "output_stems": ["vocals", "instrumental"],
  "tags": ["mdxc", "roformer", "experimental"],
  "download_url": null,
  "defaults": {
    "managed_by": "external",
    "backend": "auto",
    "device": "auto"
  },
  "options": [],
  "ui": {
    "category": "MDXC",
    "recommended": false,
    "warning": "Backend/model runner is not bundled yet."
  },
  "metadata": {
    "status": "experimental"
  }
}
```

MDXC runners often require `model_config_path`, `model_path`, or `checkpoint_path`. The adapter accepts these as separation options for command templates.

## RVC Voice Model Entry

RVC voice models can be discovered from the file system or resolved from a model manifest. Manifest entries use `arch: "rvc_voice"` and can describe the voice checkpoint plus an optional feature index:

```json
{
  "id": "iut",
  "name": "iut",
  "arch": "rvc_voice",
  "filename": "iut/iut.pth",
  "index_filename": "iut/iut.index",
  "tasks": ["voice_conversion"],
  "tags": ["rvc"],
  "description": "RVC voice model.",
  "download_url": null,
  "index_download_url": null,
  "sha256": null,
  "index_sha256": null,
  "file_size": null,
  "index_file_size": null,
  "ui": {
    "category": "RVC voice",
    "recommended": false,
    "sort_order": 1000
  },
  "metadata": {}
}
```

`filename` is the required `.pth` checkpoint path relative to the configured model directory. `index_filename` is the optional `.index` path relative to the same root. Both paths must be relative; absolute paths and paths containing `..` are rejected by the resolver/store layer.

`download_url` is used to download the `.pth` file when it is missing and `auto_download=True`. `index_download_url` is used to download the `.index` file when `index_filename` is present. Local filesystem paths and `file://` URLs are supported by the store, in addition to regular URLs supported by Python's URL opener.

The index file is optional by default. If `metadata.index_required` is true, a missing index raises an install/resolve error. If both `index_filename` and `index_download_url` are provided, the resolver treats the index artifact as required for that download operation so checksum/size failures are surfaced clearly.

Checksum and size fields are artifact-specific:

| Artifact | Filename field | URL field | SHA-256 field | Size field |
| --- | --- | --- | --- | --- |
| RVC checkpoint | `filename` | `download_url` | `sha256` | `file_size` |
| RVC index | `index_filename` | `index_download_url` | `index_sha256` | `index_file_size` |

## Download Fields

| Field | Meaning |
| --- | --- |
| `download_url` | URL or local path used by `ModelStore.download_model()` |
| `sha256` | Optional expected SHA-256 for verification |
| `file_size` | Optional expected byte size |
| `filename` | Relative path under `model_dir` |
| `managed_by` | Runtime/package owner, usually in `defaults` |
| `filename: null` | No local model file managed by `vocal_tools` |

## UI Metadata

`ui` is intentionally open-ended but commonly contains:

```json
{
  "category": "MDX",
  "recommended": true,
  "sort_order": 100,
  "warning": null
}
```

`metadata` can hold source-specific details, license information, candidates, warnings, and collision data.
