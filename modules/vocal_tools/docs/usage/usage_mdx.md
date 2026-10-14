# MDX Usage

MDX ONNX models are used through `ModelCatalog` and `Separator`.

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

## Builtin MDX Models

| Model id | Task | Output stems |
| --- | --- | --- |
| `kim_vocal_2` | Vocal separation | `vocals`, `instrumental` |
| `uvr_mdxnet_kara_2` | Karaoke / lead vocal removal | `instrumental`, `vocals` |
| `reverb_hq_by_foxjoy` | Dereverb | `dry_vocals`, `reverb` |

## Useful Options

Common options exposed by model cards and engine defaults:

- `denoise`
- `m_threads`
- `processor`
- `model_params`
- `model_params_path`
- `exclude_main`
- `exclude_inversion`

The option schema is available from:

```python
schema = catalog.get_option_schema("kim_vocal_2")
```
