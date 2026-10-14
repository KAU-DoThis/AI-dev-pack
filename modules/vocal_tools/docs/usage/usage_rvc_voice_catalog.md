# RVC Voice Catalog Usage

`RVCVoiceCatalog` scans local RVC voice models without importing the heavy RVC conversion runtime.

```python
from vocal_tools import RVCVoiceCatalog

catalog = RVCVoiceCatalog("test_models")
cards = catalog.list_voice_models()

for card in cards:
    print(card.to_dict())
```

## Scan Rules

- Directories directly under `models_dir` are voice model candidates.
- A directory is recognized when it contains at least one `*.pth`.
- The first sorted `*.pth` is selected as the default model file.
- The first sorted `*.index` is included when present.
- `mdx/`, `hubert_base.pt`, and `rmvpe.pt` are not treated as voice models.
- Direct `*.pth` files under `models_dir` are also supported.
- Multiple candidates are listed in `metadata`.

Example card:

```json
{
  "id": "iut",
  "name": "iut",
  "path": "test_models/iut",
  "pth_path": "test_models/iut/iut.pth",
  "index_path": "test_models/iut/iut.index",
  "has_index": true,
  "file_size": 57589001,
  "index_size": 31588619,
  "downloaded": true,
  "tags": ["rvc"],
  "metadata": {}
}
```

## Payload Helper

```python
from vocal_tools import list_rvc_voice_models_payload

result = list_rvc_voice_models_payload({"models_dir": "test_models"})
print(result["voice_models"])
```
