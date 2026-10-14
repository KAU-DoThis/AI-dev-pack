# Command-Line Interface

`vocal_tools` provides a thin CLI for local/manual use:

```powershell
vocal-tools --help
python -m vocal_tools.cli --help
```

`vocal-tools` is created by package installation, for example `python -m pip install -e .`. The `python -m vocal_tools.cli` form is useful during development and continues to work even if the console script has not been regenerated yet.

The CLI is intentionally not a server, web UI, database, or queue. Commands build task payloads and call the existing Python helper APIs.

## Text to Speech

```powershell
vocal-tools tts --engine edge --text "안녕하세요" --output output/hello.mp3
vocal-tools tts --engine qwen3 --text "새로운 문장입니다" --reference-audio reference.wav --language ko --output output/clone.wav
```

Use `--reference-text` for the reference recording's transcript when available.
Both engines use the same TTS payload API. See [the TTS guide](../usage/usage_tts.md) for
installation, model caching, output formats, and all supported settings.

## Error Handling

Failures print a short human-readable error to stderr:

```text
error: Input audio not found: input.wav
```

Use `--debug` before the command to print a traceback:

```powershell
vocal-tools --debug models list --models-dir test_models
```

## Option Values

Runtime options use repeated `--option key=value` pairs. Values are parsed as:

| Text | Python value |
| --- | --- |
| `true` / `false` | `bool` |
| `null` / `none` | `None` |
| `1` | `int` |
| `0.25` | `float` |
| any other text | `str` |

Examples:

```powershell
--option denoise=false --option overlap=0.25 --option shifts=1
```

## Models List

List separation models:

```powershell
vocal-tools models list --models-dir test_models
```

JSON output:

```powershell
vocal-tools models list --models-dir test_models --json
```

Filters:

```powershell
vocal-tools models list --models-dir test_models --arch mdx_onnx
vocal-tools models list --models-dir test_models --task vocal_separation
```

Output columns:

- `id`
- `name`
- `arch`
- `downloaded`
- `status`
- `stems`

Manifest-related options:

```powershell
--separation-models-dir PATH
--builtin-manifest-path PATH
--user-manifest-path PATH
--remote-manifest-url URL
--remote-manifest-cache-path PATH
```

## Models Status

Show download status per separation model:

```powershell
vocal-tools models status --models-dir test_models
vocal-tools models status --models-dir test_models --json
```

Output columns:

- `id`
- `status`
- `downloaded`
- `managed_by`
- `path`

## Voices List

List local RVC voice models:

```powershell
vocal-tools voices list --models-dir test_models
vocal-tools voices list --models-dir test_models --json
```

Output columns:

- `id`
- `name`
- `downloaded`
- `has_index`
- `pth_path`

## Separate

Run source separation:

```powershell
vocal-tools separate `
  --models-dir test_models/mdx `
  --model-id kim_vocal_2 `
  --input input.wav `
  --output-dir output/separate `
  --option denoise=false
```

JSON output:

```powershell
vocal-tools separate --models-dir test_models/mdx --model-id kim_vocal_2 --input input.wav --output-dir output/separate --json
```

This command calls `run_audio_separate_payload()`.

## RVC Convert

Run RVC conversion:

```powershell
vocal-tools rvc-convert `
  --models-dir test_models `
  --voice-model iut `
  --input vocals.wav `
  --output output/iut.wav `
  --option pitch_change=0 `
  --option f0_method=rmvpe
```

Optional runtime paths:

```powershell
--hubert-path test_models/hubert_base.pt
--rmvpe-path test_models/rmvpe.pt
--device auto
--is-half auto
```

This command calls `run_audio_rvc_convert_payload()`.

## Cover Pipeline

Run MDX -> RVC -> Mix:

```powershell
vocal-tools cover `
  --models-dir test_models `
  --input input.wav `
  --output-dir output/cover `
  --mdx-model-id kim_vocal_2 `
  --voice-model iut
```

Pipeline option groups:

```powershell
--mdx-option denoise=false
--rvc-option pitch_change=0
--rvc-option f0_method=rmvpe
--mix-option normalize=true
--mix-option peak=0.98
```

Optional preprocessing:

```powershell
--use-dereverb --dereverb-model-id reverb_hq_by_foxjoy
--use-karaoke-split --karaoke-model-id uvr_mdxnet_kara_2
```

Other useful options:

```powershell
--vocal-stem vocals
--instrumental-stem instrumental
--rvc-output-name iut_converted.wav
--mix-output-name cover_mix.wav
--no-keep-intermediate
--json
```

This command calls `run_audio_cover_pipeline_payload()`.
