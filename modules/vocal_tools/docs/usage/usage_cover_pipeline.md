# Cover Pipeline Usage

The cover pipeline combines:

```text
MDX vocal extraction
-> optional dereverb
-> optional main/backing split
-> RVC conversion
-> mix converted vocal with instrumental
```

Default behavior remains:

```text
MDX -> RVC -> Mix
```

## Basic Usage

```python
from vocal_tools import AudioMixOptions, CoverPipelineOptions, run_cover_pipeline

result = run_cover_pipeline(
    input_audio="input.wav",
    output_dir="output/cover",
    models_dir="test_models",
    options=CoverPipelineOptions(
        mdx_model_id="kim_vocal_2",
        voice_model="iut",
        mdx_options={"denoise": False},
        rvc_options={
            "pitch_change": 0,
            "f0_method": "rmvpe",
        },
        mix_options=AudioMixOptions(
            normalize=True,
            peak=0.98,
        ),
    ),
)

print(result.to_dict())
```

## Extended Options

`CoverPipelineOptions` includes:

- `vocal_stem`
- `instrumental_stem`
- `rvc_output_name`
- `mix_output_name`
- `keep_intermediate`
- `use_dereverb`
- `dereverb_model_id`
- `dereverb_options`
- `use_karaoke_split`
- `karaoke_model_id`
- `karaoke_options`

Default optional preprocessing models:

```python
dereverb_model_id = "reverb_hq_by_foxjoy"
karaoke_model_id = "uvr_mdxnet_kara_2"
```

Optional preprocessing is structural in this version. Final quality tuning and detailed vocal/backing policies remain roadmap work.
