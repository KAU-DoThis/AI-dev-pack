# SHS GPU Node Integration

This document describes the import boundary a host service such as SHS GPU Node can use later. The `vocal_tools` package itself does not connect to SHS GPU Node or SHS AI Tools.

SHS GPU Node should import `vocal_tools` and call library functions. It should own job routing, queueing, progress relay, cancellation policy, and persistence.

`vocal_tools` does not provide FastAPI, a web UI, a database, or a queue.

## Payload Helpers

The task helper functions are pure Python functions:

```python
from vocal_tools import (
    list_audio_models_payload,
    list_rvc_voice_models_payload,
    run_audio_cover_pipeline_payload,
    run_audio_rvc_convert_payload,
    run_audio_separate_payload,
)
```

### Model List Request

```python
payload = {
    "models_dir": "test_models",
    "include_separation_models": True,
    "include_voice_models": True,
}

result = list_audio_models_payload(payload)
```

The result contains:

- `separation_models`
- `voice_models`
- `sources`

For RVC voices only:

```python
result = list_rvc_voice_models_payload({"models_dir": "test_models"})
```

### Separation

```python
payload = {
    "models_dir": "test_models/mdx",
    "input_audio": "input.wav",
    "output_dir": "output/separate",
    "model_id": "kim_vocal_2",
    "options": {"denoise": False},
}

result = run_audio_separate_payload(payload)
```

### RVC

```python
payload = {
    "models_dir": "test_models",
    "input_audio": "vocals.wav",
    "output_audio": "output/rvc/iut.wav",
    "voice_model": "iut",
    "options": {
        "pitch_change": 0,
        "f0_method": "rmvpe",
    },
}

result = run_audio_rvc_convert_payload(payload)
```

### Cover Pipeline

```python
payload = {
    "models_dir": "test_models",
    "input_audio": "input.wav",
    "output_dir": "output/cover",
    "mdx_model_id": "kim_vocal_2",
    "voice_model": "iut",
    "mdx_options": {"denoise": False},
    "rvc_options": {
        "pitch_change": 0,
        "f0_method": "rmvpe",
    },
    "mix_options": {
        "normalize": True,
        "peak": 0.98,
    },
}

result = run_audio_cover_pipeline_payload(payload)
```

## Progress

Each helper accepts `progress_callback`.

```python
def relay(event: dict):
    send_to_host(event)

result = run_audio_separate_payload(payload, progress_callback=relay)
```

The callback receives JSON-safe dictionaries.

## CLI Boundary

The `vocal-tools` console script is for local/manual use and smoke checks. Host services should call the Python APIs or payload helpers directly instead of shelling out to the CLI unless they explicitly want process isolation.

## Validation

Use validation helpers before dispatch when the host wants a separate validation phase.

```python
from vocal_tools import validate_audio_cover_pipeline_payload

payload = validate_audio_cover_pipeline_payload(payload)
```

Validation normalizes Path-like fields to strings and raises `ValueError` for missing fields or non-dict option blocks.

## Recommended Host Boundary

SHS GPU Node can add:

- input validation
- max duration policy
- cancellation
- queue integration
- progress streaming
- artifact persistence

Those concerns should stay outside `vocal_tools`.
