# Testing

`vocal_tools` keeps a small standard-library smoke test suite under `tests/`.

Run from the project root:

```powershell
python -m unittest discover -s tests
```

When using the local virtual environment directly:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

## What Smoke Tests Cover

- Top-level `import vocal_tools`
- Selected public API lazy imports
- Top-level import does not load heavy runtime modules such as `torch`, `librosa`, `onnxruntime`, `fairseq`, `numpy`, `scipy`, or `soundfile`
- Builtin catalog loading through `ModelCatalog`
- Presence of representative builtin model ids: `kim_vocal_2`, `htdemucs`, `htdemucs_6s`
- CLI option parsing utility
- Payload validation success and failure cases
- TTS lazy imports, shared result/progress contracts, async calls, atomic output,
  reference prompt caching, offline model resolution, and payload/CLI parity

## What Smoke Tests Do Not Cover

The default smoke tests do not run real inference. They intentionally skip:

- MDX ONNX inference
- Demucs inference
- RVC conversion
- Full cover pipeline inference
- External VR Arch and MDXC adapter execution
- Online Edge TTS requests and real Qwen3-TTS model downloads/inference

Those checks require model files, runtime dependencies, short audio fixtures, and sometimes CUDA. Run them separately before a release candidate when the environment is ready.

## Lightweight Manual Checks

```powershell
python -B -c "import vocal_tools; print('import ok')"
python -B -c "from vocal_tools import ModelCatalog, RVCVoiceCatalog, run_cover_pipeline; print('api ok')"
python -B -c "from importlib.resources import files; p=files('vocal_tools.manifests').joinpath('builtin_models.json'); print(p.read_text(encoding='utf-8')[:20])"
python -m vocal_tools.cli --help
python -m vocal_tools.cli models list --models-dir test_models --json
python -m unittest discover -s tests
```

## Inference Regression Checks

Use the scripts documented in [the examples guide](../guides/examples.md) for real inference regression checks. Keep inputs short for release verification:

- `examples/verify_mdx_models.py`
- `examples/verify_demucs_engine.py`
- `examples/verify_rvc_converter.py`
- `examples/verify_cover_pipeline.py`

These are not part of the default smoke suite because they can be slow and environment-dependent.
