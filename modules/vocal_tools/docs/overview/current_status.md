# Current Status

This project is being shaped into a reusable UVR-like headless audio engine library. Host services can import it, but this repository does not connect directly to SHS GPU Node or SHS AI Tools.

## Working Areas

| Area | Status |
| --- | --- |
| MDX ONNX | Implemented through `MDXOnnxEngine` |
| Demucs | Implemented through `DemucsEngine` |
| RVC | Implemented through `RVCConverter` |
| Edge TTS | Online MP3/WAV synthesis, voice listing, sync and async APIs |
| Qwen3-TTS | Base model reference voice synthesis, lazy loading and reusable reference cache |
| Cover pipeline | Implemented as `MDX -> RVC -> Mix` |
| Audio mix | Implemented with sample-rate and channel alignment |
| Model catalog | Builtin, user, and remote manifest sources |
| Model store | Local files and package-managed models |
| Task helpers | Pure payload functions for host services |
| CLI | Thin `vocal-tools` command wrapper around task helpers |
| Packaging | Editable install and console script verified locally |
| Smoke tests | Standard-library import/catalog/options/validation tests |
| VR Arch | Adapter structure with `external_cli` backend |
| MDXC | Adapter structure with `external_cli` backend |

## Verified Earlier

Earlier local verification completed:

- MDX GPU inference
- Demucs CUDA inference
- RVC conversion
- MDX -> RVC -> Mix cover pipeline
- Demucs `htdemucs`, `htdemucs_6s`, and `two_stems=vocals`

This documentation pass does not repeat long inference tests.

Latest packaging smoke checks cover editable install, console script help, package-data access to `builtin_models.json`, CLI list/status commands, and `python -m unittest discover -s tests`.

## Current Public Surface

The top-level `vocal_tools` package exports stable categories for:

- Catalog APIs
- Core types
- Separation
- RVC
- Mixing
- Cover pipeline
- Task helpers
- Payload validation

See [the public API reference](../reference/public_api.md) for the grouped list.

The older direct MDX API exports (`MDXSeparator`, `MDXModelNames`, `MDXSeparationResult`) remain available for compatibility. New code should prefer `ModelCatalog + Separator`.

## Adapter Areas

| Engine | File | Status |
| --- | --- | --- |
| VR Arch | `vocal_tools/engines/vr_arch.py` | Adapter implemented; bundled backend not selected |
| MDXC | `vocal_tools/engines/mdxc.py` | Adapter implemented; bundled backend not selected |

VR Arch and MDXC can be routed through `Separator` and can call an external runner with `backend="external_cli"`.
The in-process `python` backend intentionally raises `UnsupportedEngineError` until a concrete backend is selected.

## Current Design Boundary

`vocal_tools` is not a service. It intentionally excludes:

- FastAPI
- Web UI
- Database
- Job queue
- SHS GPU Node hardcoded paths
- SHS AI Tools hardcoded paths

Host applications should import `vocal_tools` and decide how to expose jobs, progress, auth, persistence, and UI.

## Structure Notes

- `vocal_tools/rvc_catalog.py` is a compatibility shim that re-exports `catalog.voice_catalog`.
- `examples/` contains both basic usage scripts and real inference verification scripts. See the [examples guide](../guides/examples.md) before running a `verify_*` script.
- Base dependencies are still conservative for backward compatibility; future packaging work can move more runtime-specific dependencies into extras.
- `test_models/` is a development/verification fixture directory, not package data.
