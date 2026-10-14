# Task Payload Helpers

Task helpers are pure Python functions. They do not start a server, create a queue, or write to a database.

## Model Listing

```python
from vocal_tools import list_audio_models_payload

result = list_audio_models_payload(
    {
        "models_dir": "test_models",
        "include_separation_models": True,
        "include_voice_models": True
    }
)

print(result["separation_models"])
print(result["voice_models"])
print(result["sources"])
```

`list_audio_models_payload()` uses `models_dir/mdx` for separation models when that subdirectory exists. Pass `separation_models_dir` to override this.

## RVC Voice Listing

```python
from vocal_tools import list_rvc_voice_models_payload

result = list_rvc_voice_models_payload({"models_dir": "test_models"})
```

## Validation

Validation helpers normalize Path fields to strings and require options fields to be dicts.

```python
from vocal_tools import validate_audio_separate_payload

payload = validate_audio_separate_payload(
    {
        "models_dir": "test_models/mdx",
        "input_audio": "input.wav",
        "output_dir": "output/separate",
        "model_id": "kim_vocal_2",
        "options": {"denoise": False}
    }
)
```

Available validators:

- `validate_audio_separate_payload()`
- `validate_audio_rvc_convert_payload()`
- `validate_audio_cover_pipeline_payload()`

They raise `ValueError` for missing fields or invalid shapes.
