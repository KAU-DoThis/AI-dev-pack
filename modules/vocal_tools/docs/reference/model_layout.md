# Model Layout

The current local test layout is:

```text
test_models/
  hubert_base.pt
  rmvpe.pt
  public_models.json
  iut/
    iut.pth
    iut.index
  mdx/
    Kim_Vocal_2.onnx
    Reverb_HQ_By_FoxJoy.onnx
    UVR_MDXNET_KARA_2.onnx
    UVR-MDX-NET-Voc_FT.onnx
    model_data.json
```

## MDX

For MDX, pass the MDX model directory:

```python
from vocal_tools import ModelCatalog

catalog = ModelCatalog(model_dir="test_models/mdx")
```

Manifest filenames are resolved relative to `model_dir`.

```json
{
  "id": "kim_vocal_2",
  "filename": "Kim_Vocal_2.onnx"
}
```

This resolves to:

```text
test_models/mdx/Kim_Vocal_2.onnx
```

`MDXOnnxEngine` resolves `model_data.json` from explicit options, model defaults, or the model file directory.

## RVC

For RVC, pass the parent models directory:

```python
from vocal_tools.rvc import RVCConverter

converter = RVCConverter(models_dir="test_models")
```

Default lookup:

```text
test_models/hubert_base.pt
test_models/rmvpe.pt
test_models/<voice_model>/*.pth
test_models/<voice_model>/*.index
```

## Demucs

Demucs builtin cards use:

```json
{
  "filename": null,
  "defaults": {
    "managed_by": "demucs"
  }
}
```

This means the model is managed by the Demucs package and cache. `ModelStore.ensure_model()` returns `None` for package-managed models.

## RVC Voice Catalog

`RVCVoiceCatalog(models_dir="test_models")` scans voice models directly under `models_dir`.

```text
test_models/
  iut/
    iut.pth
    iut.index
```

The directory is recognized because it contains a `*.pth` file. `*.index` is optional.

Ignored as voice models:

```text
test_models/hubert_base.pt
test_models/rmvpe.pt
test_models/mdx/
```

Multiple `*.pth` files are allowed. The first sorted file is selected and all candidates are recorded in card metadata.

## Download Status

Model status can be queried without running inference:

```python
from vocal_tools import ModelCatalog

catalog = ModelCatalog(model_dir="test_models/mdx")
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
