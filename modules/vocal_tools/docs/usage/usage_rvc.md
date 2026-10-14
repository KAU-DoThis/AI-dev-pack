# RVC Usage

RVC conversion is exposed through `RVCConverter`.

```python
from vocal_tools.rvc import RVCConversionOptions, RVCConverter

converter = RVCConverter(models_dir="test_models")

output = converter.convert(
    input_audio="vocals.wav",
    output_audio="output/rvc/iut.wav",
    voice_model="iut",
    options=RVCConversionOptions(
        pitch_change=0,
        f0_method="rmvpe",
    ),
)

print(output)
```

## Model Lookup

With `models_dir="test_models"` and `voice_model="iut"`, the converter looks under:

```text
test_models/iut/
```

It uses the first `*.pth` model and the first `*.index` file if present.

Required support models:

```text
test_models/hubert_base.pt
test_models/rmvpe.pt
```

## Common Options

`RVCConversionOptions` includes:

- `pitch_change`
- `f0_method`
- `index_rate`
- `filter_radius`
- `rms_mix_rate`
- `protect`
- `crepe_hop_length`
- `sid`
- `resample_sr`
- `f0_file`

RVC is not implemented as a `SeparationEngine`; it is a voice conversion component used directly or inside cover pipelines.

For UI model listing, use `RVCVoiceCatalog`; see [usage_rvc_voice_catalog.md](usage_rvc_voice_catalog.md).
