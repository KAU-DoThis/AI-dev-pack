# Architecture

`vocal_tools` is a Python library layer. It exposes reusable engine APIs and JSON-safe task helpers while keeping service concerns outside the package.

## Package Layout

```text
vocal_tools/
  __init__.py
  separator.py
  mdx.py
  rvc.py
  audio/
    mix.py
  tts/
    __init__.py
    edge.py
    qwen.py
    _common.py
  catalog/
    model_catalog.py
    model_store.py
    remote_manifest.py
  core/
    errors.py
    progress.py
    types.py
  engines/
    base.py
    external_cli.py
    output_collectors.py
    mdx_onnx.py
    demucs.py
    vr_arch.py
    mdxc.py
  manifests/
    builtin_models.json
  pipelines/
    cover.py
  tasks/
    audio_tasks.py
    tts_tasks.py
```

## Responsibility Split

| Layer | Responsibility |
| --- | --- |
| SHS AI Tools | UI, model browser, user workflow, job creation |
| SHS GPU Node | Payload handling, GPU execution, progress relay |
| `vocal_tools` | Catalogs, engines, conversion, mixing, pipeline execution |

## Core Types

| Type | Purpose |
| --- | --- |
| `OptionSpec` | UI-friendly option schema |
| `SeparationModelSpec` | Model card and engine metadata |
| `SeparationRequest` | Engine input |
| `SeparationResult` | Engine output with stem paths |
| `ProgressEvent` | Engine progress callback payload |

`OptionSpec` includes UI helper fields such as `group`, `unit`, `step`, `placeholder`, `visible_when`, and `disabled_when`.

## Catalog Sources

`ModelCatalog` supports three manifest sources:

```text
builtin -> remote -> user
```

Effective priority:

```text
user > remote > builtin
```

If the same `model_id` exists in multiple sources, the higher-priority source wins. `list_model_cards()` includes `source` and collision metadata so UI layers can show where a card came from.

## Engine Dispatch

`Separator` looks up the model by id, reads `model.arch`, and dispatches to an engine:

```python
{
    "mdx_onnx": MDXOnnxEngine(),
    "demucs": DemucsEngine(),
    "vr_arch": VRArchEngine(),
    "mdxc": MDXCEngine(),
}
```

`vr_arch` and `mdxc` are adapter engines. They support:

- `backend="auto"`: selects `external_cli` when a command template is supplied.
- `backend="external_cli"`: runs a command template and collects expected output stems.
- `backend="python"`: reserved for future bundled runners and currently raises `UnsupportedEngineError`.

No VR Arch or MDXC third-party package is imported at `import vocal_tools` time.

## Lazy Public API

`vocal_tools.__init__` uses lazy exports. `import vocal_tools` stays light, and heavy MDX, Demucs, or RVC dependencies are imported when the relevant API is actually used.

VR Arch and MDXC follow the same boundary: backend checks happen only inside `separate()`.
