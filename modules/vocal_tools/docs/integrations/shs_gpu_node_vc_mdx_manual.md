# SHS GPU Node VC/MDX Integration Manual

Date: 2026-06-15

This document explains how to use `vocal_tools` from SHS GPU Node to add RVC voice conversion and MDX source separation.

`vocal_tools` is not a server. SHS GPU Node should own the service layer, including FastAPI, queues, databases, authentication, file-upload APIs, and progress WebSockets. `vocal_tools` should be used as a headless Python engine library called from within GPU Node.

## 1. Current Project Status

The project has reached the point where it can be imported and used as a library by GPU Node.

| Area | Current status |
| --- | --- |
| MDX ONNX | `ModelCatalog + Separator + MDXOnnxEngine` path implemented |
| RVC voice conversion | `RVCConverter` and payload helper implemented |
| Cover pipeline | `MDX -> RVC -> Mix` pipeline implemented |
| Audio mixing | Sample-rate/channel alignment, gain, normalization, and WAV output implemented |
| Model catalog | Builtin, user, and remote manifest loading implemented |
| RVC voice catalog | Scanning local `.pth`/`.index` voice models implemented |
| Task payload helpers | JSON-safe functions for host services such as GPU Node implemented |
| CLI | `vocal-tools` console command implemented |
| Tests | Import/catalog/options/payload-validation smoke tests available |
| Demucs | `DemucsEngine` implemented |
| VR Arch | External CLI adapter only; no bundled backend yet |
| MDXC/Roformer | External CLI adapter only; no bundled backend yet |
| Direct SHS GPU Node integration | Intentionally not included; the host should import and connect the library |

Values verified in the workspace at the time of writing:

- `python -m unittest discover -s tests`: 8 tests passed
- `python -m vocal_tools.cli --help`: succeeded
- `models list --models-dir test_models`: 7 builtin separation models found
- `voices list --models-dir test_models`: `iut` RVC voice model found
- Git status: `test_new2.py` was still untracked

The following three core features are ready to be connected to GPU Node:

1. MDX source separation
2. RVC voice conversion
3. A cover pipeline that extracts vocals with MDX, converts them with RVC, and mixes them with the instrumental

The following are not included in this project yet:

- GPU Node API routes
- Job queue integration
- Cancellation handling
- Job/result database persistence
- SHS AI Tools UI integration
- Public model download service
- Remote RVC voice-model installation flow

## 2. Recommended Architecture

On the SHS GPU Node side, install `vocal_tools` as an engine library and call its payload helpers from task handlers.

```text
SHS AI Tools / client
  -> SHS GPU Node API
    -> queue / worker / job store
      -> vocal_tools task helper
        -> MDX ONNX / RVC / mix runtime
          -> output artifacts
```

Keep service code out of `vocal_tools` for the following reasons:

- GPU Node already has or is expected to have job routing, queues, authentication, and progress transport.
- The audio engine should be reusable by other hosts.
- Model paths, output paths, and artifact-retention policies vary by deployment.
- Queue workers are the safer place to manage cancellation and timeouts for long inference jobs.

The service boundary to implement in GPU Node is:

| GPU Node responsibility | `vocal_tools` responsibility |
| --- | --- |
| API routes | Provide Python functions |
| Generate job IDs | Run separation/conversion |
| Enqueue/dequeue jobs | Query the model catalog |
| Relay progress over WebSocket/SSE | Emit progress-callback events |
| Handle cancellation/timeouts | Process a single function call |
| Upload/download files | Create artifacts at the output path |
| Persist to a database | Return JSON-safe results |

## 3. Installation

Install this repository in the GPU Node Python environment as an editable package.

```powershell
cd H:/shs/modules/python/vocal_tools
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

To install into another project's virtual environment, specify the package path directly.

```powershell
python -m pip install -e H:/shs/modules/python/vocal_tools
```

MDX ONNX inference on a GPU requires `onnxruntime-gpu`.

```powershell
python -m pip install -e "H:/shs/modules/python/vocal_tools[gpu]"
```

RVC requires the RVC extra. In the locally verified setup, installing an older pip version before the extra was safer because of `fairseq==0.12.2`.

```powershell
python -m pip install "pip<24.1"
python -m pip install -e "H:/shs/modules/python/vocal_tools[rvc]"
```

Install the audio extra as well if the cover pipeline needs its optional mixing packages.

```powershell
python -m pip install -e "H:/shs/modules/python/vocal_tools[audio]"
```

For a production GPU Node, also verify the following:

- The `ffmpeg` CLI is available on `PATH`.
- A CUDA-enabled build of PyTorch is installed.
- `onnxruntime-gpu` detects a working CUDA provider.
- GPU Node worker processes can access the model files and output directory.

## 4. Model Directory Layout

The simplest layout is similar to the current `test_models` directory:

```text
models/
  hubert_base.pt
  rmvpe.pt
  iut/
    iut.pth
    iut.index
  mdx/
    Kim_Vocal_2.onnx
    UVR_MDXNET_KARA_2.onnx
    Reverb_HQ_By_FoxJoy.onnx
    model_data.json
```

Recommended environment variables:

```powershell
$env:VOCAL_TOOLS_MODELS_DIR="H:/shs/models/vocal_tools"
$env:VOCAL_TOOLS_OUTPUT_ROOT="H:/shs/output/audio_jobs"
```

MDX usually uses `.onnx` files under `models_dir/mdx`.

RVC looks for the following files under the `models_dir` root:

```text
models/hubert_base.pt
models/rmvpe.pt
models/<voice_model>/*.pth
models/<voice_model>/*.index
```

For example, passing `"voice_model": "iut"` in the payload resolves these files:

```text
models/iut/iut.pth
models/iut/iut.index
```

To place a voice model deeper, such as `models/rvc/iut`, pass a relative path such as `"voice_model": "rvc/iut"` or provide an absolute path.

## 5. Task Types to Add to GPU Node

Start with these four tasks:

| Example task name | Purpose | Function |
| --- | --- | --- |
| `audio.models.list` | List separation/RVC models | `list_audio_models_payload` |
| `audio.separate` | Run MDX or Demucs separation | `run_audio_separate_payload` |
| `audio.rvc.convert` | Run standalone RVC conversion | `run_audio_rvc_convert_payload` |
| `audio.cover` | Run MDX -> RVC -> Mix | `run_audio_cover_pipeline_payload` |

Task names can follow GPU Node's existing naming conventions. The important part is to preserve the payload helpers' input/output boundary.

## 6. Shared Handler Skeleton

Add a wrapper like the following to the GPU Node worker:

```python
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from vocal_tools import (
    list_audio_models_payload,
    list_rvc_voice_models_payload,
    run_audio_cover_pipeline_payload,
    run_audio_rvc_convert_payload,
    run_audio_separate_payload,
)


MODELS_DIR = Path(os.environ["VOCAL_TOOLS_MODELS_DIR"])
OUTPUT_ROOT = Path(os.environ["VOCAL_TOOLS_OUTPUT_ROOT"])


def _job_output_dir(job_id: str, kind: str) -> Path:
    path = OUTPUT_ROOT / kind / job_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _progress(job_id: str):
    def relay(event: dict[str, Any]) -> None:
        # Connect this to GPU Node's existing progress bus, WebSocket, SSE, or database updates.
        publish_job_progress(job_id, event)

    return relay


def handle_audio_task(job_id: str, task_name: str, payload: dict[str, Any]) -> dict[str, Any]:
    payload = dict(payload)
    payload.setdefault("models_dir", str(MODELS_DIR))

    if task_name == "audio.models.list":
        return list_audio_models_payload(payload)

    if task_name == "audio.voices.list":
        return list_rvc_voice_models_payload({"models_dir": payload["models_dir"]})

    if task_name == "audio.separate":
        payload.setdefault("output_dir", str(_job_output_dir(job_id, "separate")))
        return run_audio_separate_payload(payload, progress_callback=_progress(job_id))

    if task_name == "audio.rvc.convert":
        output_dir = _job_output_dir(job_id, "rvc")
        payload.setdefault("output_audio", str(output_dir / "converted.wav"))
        return run_audio_rvc_convert_payload(payload, progress_callback=_progress(job_id))

    if task_name == "audio.cover":
        payload.setdefault("output_dir", str(_job_output_dir(job_id, "cover")))
        return run_audio_cover_pipeline_payload(payload, progress_callback=_progress(job_id))

    raise ValueError(f"Unsupported audio task: {task_name}")
```

Replace `publish_job_progress()` in this example with GPU Node's existing progress-publishing function.

## 7. Model List API

Use `list_audio_models_payload()` when GPU Node needs to return a model list to the UI.

```python
from vocal_tools import list_audio_models_payload

result = list_audio_models_payload(
    {
        "models_dir": "H:/shs/models/vocal_tools",
        "include_separation_models": True,
        "include_voice_models": True,
    }
)
```

The return value is a JSON-safe dictionary.

```json
{
  "separation_models": [],
  "voice_models": [],
  "sources": []
}
```

The UI commonly uses these fields:

| Field | Meaning |
| --- | --- |
| `id` | Model ID to include in a task payload |
| `name` | Display name |
| `arch` | Architecture such as `mdx_onnx`, `demucs`, `vr_arch`, or `mdxc` |
| `tasks` | Supported tasks such as `vocal_separation`, `dereverb`, or `karaoke` |
| `output_stems` | Names of the output stems |
| `downloaded` | Whether the model can run locally now |
| `download_status` | Status such as `downloaded`, `missing`, or `managed_by_package` |
| `ui.category` | UI grouping |
| `options` | Option schema for generating UI controls |

To list only RVC voices, use:

```python
from vocal_tools import list_rvc_voice_models_payload

result = list_rvc_voice_models_payload({"models_dir": "H:/shs/models/vocal_tools"})
```

The main fields in an RVC voice card are:

| Field | Meaning |
| --- | --- |
| `id` | Default ID to pass as `voice_model` |
| `path` | Voice-model directory |
| `pth_path` | Selected `.pth` file |
| `index_path` | Selected `.index` file, or null if absent |
| `has_index` | Whether an index file exists |
| `downloaded` | Whether a `.pth` file exists |

## 8. Add MDX Source Separation

Call `run_audio_separate_payload()` to run an MDX separation job from GPU Node.

```python
from vocal_tools import run_audio_separate_payload

payload = {
    "models_dir": "H:/shs/models/vocal_tools/mdx",
    "input_audio": "H:/shs/input/song.wav",
    "output_dir": "H:/shs/output/audio_jobs/separate/job-001",
    "model_id": "kim_vocal_2",
    "options": {
        "denoise": False,
        "processor": 0
    },
}

result = run_audio_separate_payload(payload, progress_callback=relay_progress)
```

Important notes:

- For standalone MDX, passing `models/mdx` as `models_dir` is the clearest option.
- Use a model ID from the builtin manifest as `model_id`.
- `processor` is the CUDA device index; `0` selects the first GPU.
- `denoise=True` may take longer depending on the model and environment.
- Place `model_data.json` beside the `.onnx` file or set `model_params_path`.

The default MDX model IDs are:

| Model ID | Purpose | Output stems |
| --- | --- | --- |
| `kim_vocal_2` | Vocal/instrumental separation | `vocals`, `instrumental` |
| `uvr_mdxnet_kara_2` | Karaoke/lead-vocal removal | `instrumental`, `vocals` |
| `reverb_hq_by_foxjoy` | Dereverberation | `dry_vocals`, `reverb` |

On success, the result has a shape similar to this:

```json
{
  "input_audio": ".../song.wav",
  "output_dir": ".../job-001",
  "model_id": "kim_vocal_2",
  "model_name": "Kim Vocal 2",
  "arch": "mdx_onnx",
  "stems": {
    "vocals": ".../job-001/song_(vocals).wav",
    "instrumental": ".../job-001/song_(instrumental).wav"
  },
  "options_used": {},
  "metadata": {}
}
```

## 9. Add RVC Voice Conversion

Standalone RVC conversion takes an already-separated vocal file as input.

```python
from vocal_tools import run_audio_rvc_convert_payload

payload = {
    "models_dir": "H:/shs/models/vocal_tools",
    "input_audio": "H:/shs/output/audio_jobs/separate/job-001/vocals.wav",
    "output_audio": "H:/shs/output/audio_jobs/rvc/job-002/converted.wav",
    "voice_model": "iut",
    "device": "cuda:0",
    "is_half": "auto",
    "options": {
        "pitch_change": 0,
        "f0_method": "rmvpe",
        "index_rate": 0.5,
        "filter_radius": 3,
        "rms_mix_rate": 0.25,
        "protect": 0.33
    },
}

result = run_audio_rvc_convert_payload(payload, progress_callback=relay_progress)
```

Required files:

```text
models/hubert_base.pt
models/rmvpe.pt
models/iut/iut.pth
models/iut/iut.index
```

The `index` file is optional, but it can improve timbre-retrieval quality.

RVC option meanings:

| Option | Default | Description |
| --- | --- | --- |
| `pitch_change` | `0` | Pitch shift in semitones |
| `f0_method` | `rmvpe` | Pitch-extraction method |
| `index_rate` | `0.5` | Contribution from index retrieval |
| `filter_radius` | `3` | F0 smoothing radius |
| `rms_mix_rate` | `0.25` | Mix ratio between input and output RMS |
| `protect` | `0.33` | Protection for unvoiced sounds and consonants |
| `crepe_hop_length` | `128` | F0 hop length for CREPE-based methods |
| `sid` | `0` | Speaker ID |
| `resample_sr` | `0` | Output resample rate; `0` uses the model default |

Explicitly setting `device="cuda:0"` makes GPU Node operations easier to trace. `auto` is also supported, but when workers are pinned to specific GPUs, set the device in the task payload.

## 10. Add the Cover Pipeline

Use the cover pipeline when a user provides only an original track and a voice model and wants a cover result.

```python
from vocal_tools import run_audio_cover_pipeline_payload

payload = {
    "models_dir": "H:/shs/models/vocal_tools",
    "input_audio": "H:/shs/input/song.wav",
    "output_dir": "H:/shs/output/audio_jobs/cover/job-003",
    "mdx_model_id": "kim_vocal_2",
    "voice_model": "iut",
    "mdx_options": {
        "denoise": False,
        "processor": 0
    },
    "rvc_options": {
        "pitch_change": 0,
        "f0_method": "rmvpe",
        "index_rate": 0.5,
        "protect": 0.33
    },
    "mix_options": {
        "vocal_gain_db": 0.0,
        "instrumental_gain_db": 0.0,
        "normalize": True,
        "peak": 0.98
    },
    "keep_intermediate": True
}

result = run_audio_cover_pipeline_payload(payload, progress_callback=relay_progress)
```

The pipeline runs these steps:

```text
1. Separate vocals and instrumental with MDX.
2. Optionally run an additional MDX dereverberation pass.
3. Optionally run a karaoke/main-vocal split pass.
4. Convert the vocals with the selected RVC voice model.
5. Mix the converted vocals with the instrumental.
6. Return a JSON-safe result.
```

Optional settings:

| Option | Default | Description |
| --- | --- | --- |
| `vocal_stem` | `vocals` | Stem to use as RVC input |
| `instrumental_stem` | `instrumental` | Instrumental stem to use in the final mix |
| `rvc_output_name` | Auto-generated from voice | Converted vocal filename |
| `mix_output_name` | `cover_mix.wav` | Final mix filename |
| `keep_intermediate` | `True` | Preserve intermediate artifacts |
| `use_dereverb` | `False` | Run an additional dereverberation pass |
| `dereverb_model_id` | `reverb_hq_by_foxjoy` | Dereverberation model ID |
| `use_karaoke_split` | `False` | Run an additional main/backing split pass |
| `karaoke_model_id` | `uvr_mdxnet_kara_2` | Karaoke model ID |

For the first GPU Node integration, start with optional preprocessing disabled.

```json
{
  "use_dereverb": false,
  "use_karaoke_split": false
}
```

It is easier to debug if you enable these options only after the basic pipeline is stable and you have tested quality with different tracks and models.

## 11. Connect Progress Events

Every payload helper accepts a `progress_callback`.

```python
def relay_progress(event: dict) -> None:
    print(event)

result = run_audio_cover_pipeline_payload(payload, progress_callback=relay_progress)
```

Common stages include:

| Stage | Meaning |
| --- | --- |
| `task_start` | Helper task started |
| `separate_progress` | Internal separation event |
| `pipeline_progress` | Internal cover-pipeline event |
| `mdx_start` | MDX started in the cover pipeline |
| `mdx_progress` | MDX progress |
| `mdx_complete` | MDX completed |
| `rvc_start` | RVC started |
| `rvc_complete` | RVC completed |
| `mix_start` | Mixing started |
| `mix_complete` | Mixing completed |
| `task_complete` | Helper task completed |

GPU Node can relay these events directly to its job progress channel.

```python
def relay_progress(job_id: str):
    def relay(event: dict) -> None:
        job_store.append_event(job_id, event)
        websocket_manager.publish(job_id, event)

    return relay
```

Fine-grained MDX/RVC percentages are currently limited. Initially, a stage-based progress indicator is the most practical UI approach.

## 12. Cancellation and Timeouts

A single `vocal_tools` function may perform a long inference, so GPU Node's worker layer should handle cancellation.

Recommended approach:

1. Run each job in a worker process or cancellable subprocess.
2. Let the GPU Node queue manage timeouts.
3. Stop the worker process when a cancellation request arrives.
4. Clean up the output directory or mark its artifacts as partial.

Do not rely on forcibly stopping a Python thread. It is difficult to stop safely when PyTorch, ONNX Runtime, and FFmpeg subprocesses are involved.

## 13. Error-Handling Policy

GPU Node can classify exceptions as follows:

| Exception/situation | Example HTTP/job status | User-facing message |
| --- | --- | --- |
| Payload-validation `ValueError` | failed / bad_request | A request field is invalid |
| Input file missing | failed / bad_request | Input audio was not found |
| Model file missing | failed / model_missing | The model file is not installed |
| CUDA unavailable | failed / runtime_unavailable | Check the GPU runtime |
| Exception during inference | failed / inference_error | An error occurred during processing |
| Timeout | cancelled or failed / timeout | Processing exceeded the time limit |
| Cancellation request | cancelled | The user cancelled the job |

Example handler:

```python
def run_job(job_id: str, task_name: str, payload: dict) -> dict:
    try:
        result = handle_audio_task(job_id, task_name, payload)
    except ValueError as exc:
        return fail_job(job_id, code="bad_request", message=str(exc))
    except FileNotFoundError as exc:
        return fail_job(job_id, code="file_not_found", message=str(exc))
    except Exception as exc:
        return fail_job(job_id, code="inference_error", message=str(exc))

    return complete_job(job_id, result)
```

## 14. Example API Design

If GPU Node uses FastAPI, its routes could look roughly like this:

```python
from fastapi import APIRouter

router = APIRouter(prefix="/audio")


@router.get("/models")
def list_models():
    return list_audio_models_payload(
        {
            "models_dir": str(MODELS_DIR),
            "include_separation_models": True,
            "include_voice_models": True,
        }
    )


@router.post("/separate")
def create_separation_job(payload: dict):
    job_id = enqueue_job("audio.separate", payload)
    return {"job_id": job_id}


@router.post("/rvc")
def create_rvc_job(payload: dict):
    job_id = enqueue_job("audio.rvc.convert", payload)
    return {"job_id": job_id}


@router.post("/cover")
def create_cover_job(payload: dict):
    job_id = enqueue_job("audio.cover", payload)
    return {"job_id": job_id}
```

In the worker, dispatch tasks through `handle_audio_task()` from section 6.

## 15. Verify with the CLI First

Run CLI smoke checks in the same environment before integrating with GPU Node.

```powershell
python -m vocal_tools.cli models list --models-dir H:/shs/models/vocal_tools
python -m vocal_tools.cli voices list --models-dir H:/shs/models/vocal_tools
```

Standalone MDX check:

```powershell
python -m vocal_tools.cli separate `
  --models-dir H:/shs/models/vocal_tools/mdx `
  --model-id kim_vocal_2 `
  --input H:/shs/input/song.wav `
  --output-dir H:/shs/output/check_mdx `
  --option denoise=false `
  --option processor=0
```

Standalone RVC check:

```powershell
python -m vocal_tools.cli rvc-convert `
  --models-dir H:/shs/models/vocal_tools `
  --voice-model iut `
  --input H:/shs/output/check_mdx/vocals.wav `
  --output H:/shs/output/check_rvc/converted.wav `
  --device cuda:0 `
  --option pitch_change=0 `
  --option f0_method=rmvpe
```

Cover-pipeline check:

```powershell
python -m vocal_tools.cli cover `
  --models-dir H:/shs/models/vocal_tools `
  --input H:/shs/input/song.wav `
  --output-dir H:/shs/output/check_cover `
  --mdx-model-id kim_vocal_2 `
  --voice-model iut `
  --mdx-option denoise=false `
  --mdx-option processor=0 `
  --rvc-option pitch_change=0 `
  --rvc-option f0_method=rmvpe `
  --mix-option normalize=true `
  --mix-option peak=0.98
```

Once the CLI succeeds, use the same payload in the GPU Node worker.

## 16. Operations Checklist

Before integrating with GPU Node, verify:

- `python -m unittest discover -s tests` passes.
- `python -m vocal_tools.cli models list --models-dir <models_dir>` succeeds.
- `python -m vocal_tools.cli voices list --models-dir <models_dir>` succeeds.
- `ffmpeg -version` succeeds.
- PyTorch CUDA availability is confirmed.
- The ONNX Runtime CUDA provider is available.
- MDX is verified with an audio clip no longer than 10 seconds.
- RVC is verified with vocals that have already been separated.
- The cover pipeline is verified.
- A maximum duration policy is defined for long audio.
- Output-retention and cleanup policies are defined.
- A cancellation approach is defined.
- Per-GPU worker concurrency is defined.

Check PyTorch CUDA:

```powershell
python -c "import torch; print(torch.__version__); print(torch.cuda.is_available()); print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else None)"
```

Check ONNX Runtime providers:

```powershell
python -c "import onnxruntime as ort; print(ort.get_available_providers())"
```

MDX ONNX GPU inference requires `CUDAExecutionProvider` to be listed.

## 17. Recommended Initial Implementation Order

1. Install `vocal_tools` as an editable package in the GPU Node virtual environment.
2. Organize models as `models/hubert_base.pt`, `models/rmvpe.pt`, `models/<voice>`, and `models/mdx`.
3. Add the `audio.models.list` and `audio.voices.list` routes.
4. Verify MDX, RVC, and the cover pipeline with short audio through the CLI.
5. Add a dispatch wrapper such as `handle_audio_task()` to the GPU Node worker.
6. Connect the `audio.separate` job.
7. Connect the `audio.rvc.convert` job.
8. Connect the `audio.cover` job.
9. Relay progress events to the existing job progress channel.
10. Connect timeout, cancellation, and output-cleanup policies.
11. Connect model listing and job-creation payloads in the SHS AI Tools UI.

## 18. Important Considerations

- `models_dir` has different meanings for MDX and RVC.
  - Standalone MDX: `models/mdx`
  - RVC/cover: root `models`
- The `models_dir` in a `cover` payload must be the `models` root; the pipeline uses `models/mdx` internally.
- The default RVC support-model filenames are `hubert_base.pt` and `rmvpe.pt`.
- An RVC voice directory must contain a `.pth` file; an `.index` file is optional.
- `processor` is the MDX ONNX CUDA device index, while RVC's `device` is a PyTorch device string such as `cuda:0`.
- MDXC/Roformer is different from MDX ONNX. Only an external CLI adapter is currently available; there is no bundled runtime.
- The default smoke tests do not run inference. Verify real GPU inference separately with the CLI or an `examples/verify_*` script.
