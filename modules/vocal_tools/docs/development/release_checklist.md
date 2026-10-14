# Release Checklist

Use this checklist before tagging or handing `vocal_tools` to another project.

## Packaging

- Confirm package name is `vocal-tools`.
- Confirm import package is `vocal_tools`.
- Confirm editable install works:

  ```powershell
  python -m pip install -e .
  python -m pip show vocal-tools
  ```

- Confirm console script works after install:

  ```powershell
  vocal-tools --help
  vocal-tools models list --models-dir test_models --json
  ```

- Confirm module CLI still works during development:

  ```powershell
  python -m vocal_tools.cli --help
  ```

- Confirm package data is accessible:

  ```powershell
  python -B -c "from importlib.resources import files; p=files('vocal_tools.manifests').joinpath('builtin_models.json'); print(p.read_text(encoding='utf-8')[:20])"
  ```

## Documentation

- Confirm these docs exist:
  - `docs/README.md`
  - `docs/guides/installation.md`
  - `docs/reference/public_api.md`
  - `docs/guides/cli.md`
  - `docs/development/testing.md`
  - `docs/development/release_checklist.md`
  - `docs/overview/current_status.md`
  - `docs/overview/roadmap.md`

## Smoke Tests

- Run standard-library smoke tests:

  ```powershell
  python -m unittest discover -s tests
  ```

- Run lightweight manual checks:

  ```powershell
  python -B -c "import vocal_tools; print('import ok')"
  python -B -c "from vocal_tools import ModelCatalog, RVCVoiceCatalog, run_cover_pipeline; print('api ok')"
  python -m vocal_tools.cli models status --models-dir test_models --json
  python -m vocal_tools.cli voices list --models-dir test_models --json
  ```

## Inference Checks

Keep release inference checks short. Do not use long production tracks for routine packaging verification.

- MDX quick inference:
  - Use `examples/verify_mdx_models.py` with a short test tone.
- Demucs quick inference:
  - Use `examples/verify_demucs_engine.py` with `--device auto` and a short input.
- RVC quick inference:
  - Use `examples/verify_rvc_converter.py` with a short vocal input.
- Cover pipeline quick inference:
  - Use `examples/verify_cover_pipeline.py` with a short input.

## Dependency Extras

- Check base install.
- Check `.[mdx]` for CPU ONNX Runtime.
- Check `.[gpu]` for ONNX Runtime GPU.
- Check `.[demucs]` separately; Demucs is intentionally not part of `all`.
- Check `.[rvc]` with the documented `pip<24.1` workaround when needed.
- Check `.[audio]` for optional audio utilities.
- Confirm `.[vr]` and `.[mdxc]` remain adapter placeholders until concrete backends are selected.
- Confirm `.[all]` contains MDX CPU, RVC, and optional audio dependencies, but not Demucs.

## Platform Notes

- Windows:
  - Verify `vocal-tools.exe` exists in the environment Scripts directory after install.
  - Verify PowerShell examples use backticks or one-line commands.
  - Confirm CUDA-specific checks only run on machines with a compatible CUDA PyTorch install.

- Linux:
  - Verify `vocal-tools` is on `PATH` after activation.
  - Confirm `ffmpeg` CLI availability for RVC audio loading.
  - Confirm CUDA checks only run where GPU runtime is installed.

## Model Files

- Do not package model binaries inside the wheel or sdist.
- `test_models/` is for local development and verification only.
- Production model placement should be handled by the host project or deployment environment.
- `vocal_tools/manifests/builtin_models.json` is package data and should be included.
