# Installation

From the project root:

```powershell
cd H:/shs/modules/python/vocal_tools
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip setuptools wheel
python -m pip install -e .
```

This installs the Python package and the console script:

```powershell
vocal-tools --help
```

During development, the module entrypoint works even when the console script has not been regenerated:

```powershell
python -m vocal_tools.cli --help
```

If `vocal-tools` is not found after editing `pyproject.toml`, rerun:

```powershell
python -m pip install -e .
```

## Optional Extras

The package exposes runtime extras in `pyproject.toml`.

```powershell
python -m pip install -e ".[mdx]"
python -m pip install -e ".[gpu]"
python -m pip install -e ".[demucs]"
python -m pip install -e ".[rvc]"
python -m pip install -e ".[audio]"
python -m pip install -e ".[edge-tts]"
python -m pip install -e ".[qwen-tts]"
```

Recommended local order for the current verified environment:

```powershell
python -m pip install -e ".[gpu]"
python -m pip install -e ".[demucs]"
python -m pip install "pip<24.1"
python -m pip install -e ".[rvc]"
```

Notes:

- `gpu` installs `onnxruntime-gpu`.
- `mdx` installs CPU `onnxruntime`.
- `demucs` installs the Demucs package.
- `rvc` installs RVC-related packages such as `fairseq`, `faiss-cpu`, `pyworld`, and `torchcrepe`.
- `audio` installs optional audio utility packages such as `pedalboard` and `pydub`.
- `edge-tts` installs the online Edge speech client; WAV output also requires `ffmpeg` on PATH.
- `qwen-tts` installs the local reference voice runtime. `tts` installs both TTS backends.
- Install TTS extras explicitly; the legacy `all` extra does not include them. See [the TTS guide](../usage/usage_tts.md).
- `vr` and `mdxc` are adapter slots. They currently do not install a concrete bundled backend.
- Demucs models are package-managed; they are not stored as local model files by `vocal_tools`.
- `all` installs MDX CPU runtime, RVC runtime, and optional audio packages. It intentionally does not include `demucs`, because Demucs can bring a larger runtime stack and package-managed model behavior. Install `.[demucs]` explicitly when needed.
- Some RVC dependency sets, especially `fairseq==0.12.2`, are sensitive to modern pip build behavior. In the verified local setup, installing `pip<24.1` before `.[rvc]` avoided resolver/build issues.

## Base Dependency Note

The current base install still includes shared audio and PyTorch dependencies (`torch`, `librosa`, `scipy`, `soundfile`, `numpy`, `ffmpeg-python`, `tqdm`) for backward compatibility with existing direct imports and verified workflows.

That means a service that only wants `import vocal_tools` still installs heavier runtime packages today. Top-level import is lightweight at runtime, but installation is not yet minimal. A future packaging cleanup can move more engine-specific packages into extras after downstream imports are audited.

## Package Data Check

The builtin manifest is package data and should be available after install:

```powershell
python -B -c "from importlib.resources import files; p=files('vocal_tools.manifests').joinpath('builtin_models.json'); print(p.read_text(encoding='utf-8')[:20])"
```

## Lightweight Checks

```powershell
python -B -c "import vocal_tools; print('import ok')"
python -B -c "from vocal_tools import ModelCatalog, RVCVoiceCatalog, run_cover_pipeline; print('api ok')"
python -m vocal_tools.cli --help
python -m unittest discover -s tests
```

See [testing](../development/testing.md) and the [release checklist](../development/release_checklist.md) for release-oriented checks.
