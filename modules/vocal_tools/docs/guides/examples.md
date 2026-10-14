# Examples

The `examples/` directory contains both basic usage snippets and verification scripts. Scripts named `verify_*` can run real inference and should be used only when the required models, runtime dependencies, GPU/CPU policy, and short test audio are ready.

Use the CLI for quick listing smoke checks:

```powershell
python -m vocal_tools.cli models list --models-dir test_models
python -m vocal_tools.cli voices list --models-dir test_models
```

## Basic

| File | Purpose | Command | Required models | Output |
| --- | --- | --- | --- | --- |
| `list_models.py` | Print separation model cards from `ModelCatalog` | `python examples/list_models.py` | Manifest only; defaults to `data/models` | JSON on stdout |
| `list_all_audio_models.py` | Print separation and RVC voice model cards through payload helper | `python examples/list_all_audio_models.py` | `test_models/mdx` manifest; optional RVC voices under `test_models` | JSON on stdout |
| `list_rvc_voice_models.py` | Scan local RVC voices through catalog and payload helper | `python examples/list_rvc_voice_models.py` | RVC voice folders or `.pth` files under `test_models` | JSON on stdout |
| `model_download_status.py` | Print separation model download status | `python examples/model_download_status.py` | `test_models/mdx` | JSON on stdout |

Additional catalog utilities:

| File | Purpose | Command | Required models | Output |
| --- | --- | --- | --- | --- |
| `register_custom_model.py` | Register a local user manifest entry | `python examples/register_custom_model.py` | None for registration; model file needed before inference | User manifest path printed |
| `model_manifest_usage.py` | Demonstrate local remote-manifest loading | `python examples/model_manifest_usage.py` | `test_models/mdx` | `output/examples/remote_models.json` and JSON stdout |

## MDX

| File | Purpose | Command | Required models | Output |
| --- | --- | --- | --- | --- |
| `mdx_separate.py` | Legacy direct `MDXSeparator` example | `python examples/mdx_separate.py` | `data/models/mdx` with MDX `.onnx` files and `model_data.json`; `input/song.mp3` | `output/song` |
| `separate_with_catalog.py` | Preferred `ModelCatalog + Separator` example | `python examples/separate_with_catalog.py` | `data/models`; `input/song.mp3` | `output/song` |
| `verify_mdx_code_api.py` | Compare legacy MDX API and catalog API | `python examples/verify_mdx_code_api.py --models-dir test_models/mdx --input output/verify_mdx/test_tone_10s.wav --output-dir output/verify_mdx` | MDX `.onnx` files and `model_data.json` | `output/verify_mdx/legacy`, `output/verify_mdx/catalog` |
| `verify_mdx_models.py` | Verify builtin MDX model cards and selected models | `python examples/verify_mdx_models.py` | `test_models/mdx` | `output/verify_mdx_models` |
| `verify_register_uvr_voc_ft.py` | Register and verify `UVR-MDX-NET-Voc_FT` as a user model | `python examples/verify_register_uvr_voc_ft.py` | `test_models/mdx/UVR-MDX-NET-Voc_FT.onnx`; short test tone | `output/verify_mdx_models/user_models.json`, `output/verify_mdx_models/uvr_mdxnet_voc_ft` |

## Demucs

| File | Purpose | Command | Required models | Output |
| --- | --- | --- | --- | --- |
| `verify_demucs_engine.py` | Verify Demucs through `ModelCatalog + Separator` | `python examples/verify_demucs_engine.py --model-id htdemucs --device auto` | `demucs` package; Demucs package-managed model availability | `output/verify_demucs` |

Demucs models are package-managed and are intentionally not downloaded into `test_models` by `vocal_tools`.

## RVC

| File | Purpose | Command | Required models | Output |
| --- | --- | --- | --- | --- |
| `rvc_convert.py` | Basic direct `RVCConverter` example | `python examples/rvc_convert.py` | `data/models/hubert_base.pt`, `data/models/rmvpe.pt`, `data/models/iut`, and vocal input | `output/song/song_iut.wav` |
| `verify_rvc_converter.py` | Verify RVC model resolution and conversion on short input | `python examples/verify_rvc_converter.py --models-dir test_models --voice-model iut --input output/verify_pipeline/mdx/test_tone_10s_vocals.wav --output output/verify_rvc/iut_converted.wav` | `test_models/hubert_base.pt`, `test_models/rmvpe.pt`, `test_models/iut` | `output/verify_rvc/iut_converted.wav` |

## TTS

Run `python examples/tts_usage.py --engine edge --output output/hello.mp3` after
installing `.[edge-tts]`. For reference voice synthesis, install `.[qwen-tts]` and
run `python examples/tts_usage.py --engine qwen3 --reference-audio reference.wav --output output/clone.wav`.
These commands perform real synthesis; Qwen may download its model on first use.
See [the TTS guide](../usage/usage_tts.md) for transcripts, async calls, and reusable caches.

## Pipeline

| File | Purpose | Command | Required models | Output |
| --- | --- | --- | --- | --- |
| `verify_mdx_rvc_pipeline.py` | Verify MDX vocals -> RVC conversion without final mix | `python examples/verify_mdx_rvc_pipeline.py --models-dir test_models --mdx-model-id kim_vocal_2 --voice-model iut --input output/verify_mdx/test_tone_10s.wav --output-dir output/verify_pipeline` | MDX model under `test_models/mdx`; RVC assets under `test_models` | `output/verify_pipeline/mdx`, `output/verify_pipeline/rvc` |
| `verify_audio_mix.py` | Verify vocal + instrumental mixing | `python examples/verify_audio_mix.py` | Existing vocal and instrumental WAV inputs | `output/verify_mix/cover_mix.wav` |
| `verify_cover_pipeline.py` | Verify full MDX -> RVC -> Mix pipeline on short input | `python examples/verify_cover_pipeline.py --models-dir test_models --input output/verify_mdx/test_tone_10s.wav --output-dir output/verify_cover_pipeline --mdx-model-id kim_vocal_2 --voice-model iut` | MDX model under `test_models/mdx`; RVC assets under `test_models` | `output/verify_cover_pipeline/mdx`, `output/verify_cover_pipeline/rvc`, `output/verify_cover_pipeline/mix` |

## Task Payload

| File | Purpose | Command | Required models | Output |
| --- | --- | --- | --- | --- |
| `task_payload_usage.py` | Show pure Python payload helper shapes | `python examples/task_payload_usage.py` | None unless uncommenting execution calls | JSON payload examples on stdout |
| `validate_audio_payloads.py` | Demonstrate payload validation and normalized paths | `python examples/validate_audio_payloads.py` | None | Validation output on stdout |
| `shs_gpu_node_usage.py` | Show a host-service-style separation wrapper | `python examples/shs_gpu_node_usage.py` | `data/models`; `input/song.mp3` if execution is used | `output/song` |
| `shs_gpu_node_cover_pipeline_usage.py` | Show a host-service-style cover pipeline wrapper | `python examples/shs_gpu_node_cover_pipeline_usage.py` | `test_models` and short input if execution is used | `output/verify_cover_pipeline` |

The SHS-named examples are not integrations. They are plain Python examples showing how a host service could call the library later.

## Adapter Templates

| File | Purpose | Command | Required models | Output |
| --- | --- | --- | --- | --- |
| `vr_arch_external_cli_template.py` | Template for a VR Arch external CLI adapter command | `python examples/vr_arch_external_cli_template.py` | External VR runner and model if adapted | `output/vr_arch` |
| `mdxc_external_cli_template.py` | Template for an MDXC/Roformer external CLI adapter command | `python examples/mdxc_external_cli_template.py` | External MDXC/Roformer runner and model if adapted | `output/mdxc` |

These templates intentionally require user-provided external commands. No bundled VR Arch or MDXC backend is selected yet.
