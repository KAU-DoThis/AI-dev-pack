from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from vocal_tools.core.errors import SeparationError, UnsupportedEngineError
from vocal_tools.core.progress import ProgressCallback, ProgressEvent
from vocal_tools.core.types import OptionSpec, SeparationRequest, SeparationResult
from vocal_tools.engines.base import SeparationEngine


class DemucsEngine(SeparationEngine):
    arch = "demucs"

    def get_option_schema(self, model):
        options = list(model.options)
        known = {option.name for option in options}
        extras = [
            OptionSpec(
                name="model_name",
                type="string",
                default=None,
                label="Model Name",
                help="Demucs model name passed to the CLI.",
                advanced=True,
            ),
            OptionSpec(
                name="output_format",
                type="enum",
                default="wav",
                label="Output Format",
                choices=["wav", "mp3", "flac"],
                advanced=False,
            ),
            OptionSpec(
                name="segment",
                type="float",
                default=None,
                label="Segment",
                min=1,
                help="Optional segment length in seconds.",
                advanced=True,
            ),
        ]
        options.extend(option for option in extras if option.name not in known)
        return options

    def separate(
        self,
        request: SeparationRequest,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> SeparationResult:
        self.validate_model(request.model, request.model_path)

        input_audio = Path(request.input_audio)
        output_dir = Path(request.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        options = dict(request.options)

        model_name = (
            options.get("model_name")
            or request.model.defaults.get("model_name")
            or request.model.id
        )
        requested_device = (
            options.get("device")
            or request.model.defaults.get("device")
            or "auto"
        )
        shifts = options.get("shifts", request.model.defaults.get("shifts", 1))
        overlap = options.get("overlap", request.model.defaults.get("overlap", 0.25))
        segment = options.get("segment", request.model.defaults.get("segment"))
        two_stems = options.get("two_stems", request.model.defaults.get("two_stems"))
        output_format = (
            options.get("output_format")
            or request.model.defaults.get("output_format")
            or "wav"
        )

        output_format = str(output_format).lower()
        if output_format not in {"wav", "mp3", "flac"}:
            raise SeparationError(f"Unsupported Demucs output_format: {output_format!r}")

        self._emit(
            progress_callback,
            stage="verify",
            message="Verifying Demucs runtime.",
            request=request,
        )

        runtime_info = self._runtime_info(str(requested_device))
        selected_device = runtime_info["selected_device"]
        module_name = self._resolve_entrypoint()

        command = self._build_command(
            module_name=module_name,
            model_name=str(model_name),
            output_dir=output_dir,
            selected_device=selected_device,
            shifts=shifts,
            overlap=overlap,
            segment=segment,
            two_stems=two_stems,
            output_format=output_format,
            input_audio=input_audio,
        )

        self._emit(
            progress_callback,
            stage="start",
            message="Starting Demucs separation.",
            request=request,
            extra={
                "model_name": str(model_name),
                "selected_device": selected_device,
                "command": command,
            },
        )

        try:
            self._emit(
                progress_callback,
                stage="running",
                message="Running Demucs CLI.",
                request=request,
            )
            completed = subprocess.run(
                command,
                cwd=str(Path.cwd()),
                capture_output=True,
                text=True,
                errors="replace",
                check=False,
            )
        except OSError as exc:
            raise SeparationError(f"Could not run Demucs CLI: {exc}") from exc

        if completed.returncode != 0:
            raise SeparationError(
                "Demucs separation failed with exit code "
                f"{completed.returncode}.\n"
                f"Command: {command}\n"
                f"stdout:\n{completed.stdout}\n"
                f"stderr:\n{completed.stderr}"
            )

        stems, raw_outputs = self._collect_outputs(
            output_dir=output_dir,
            model_name=str(model_name),
            input_audio=input_audio,
            output_format=output_format,
            expected_stems=request.model.output_stems,
            two_stems=two_stems,
        )
        if not stems:
            raise SeparationError(
                "Demucs completed but no output stems were found. "
                f"Searched under {output_dir} for .{output_format} files."
            )

        self._emit(
            progress_callback,
            stage="complete",
            message="Demucs separation complete.",
            request=request,
            current=1,
            total=1,
            extra={"stems": {name: str(path) for name, path in stems.items()}},
        )

        return SeparationResult(
            input_audio=input_audio,
            output_dir=output_dir,
            model_id=request.model.id,
            model_name=request.model.name,
            arch=request.model.arch,
            stems=stems,
            options_used=dict(options),
            metadata={
                "model_name": str(model_name),
                "two_stems": str(two_stems) if two_stems else None,
                "command": command,
                "runtime": runtime_info,
                "raw_outputs": raw_outputs,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            },
        )

    def _runtime_info(self, requested_device: str) -> Dict[str, Any]:
        requested = (requested_device or "auto").lower()
        try:
            import torch
        except ImportError as exc:
            raise SeparationError(
                "PyTorch is not available. Install Demucs runtime dependencies before running Demucs."
            ) from exc

        cuda_available = bool(torch.cuda.is_available())
        cuda_device_name = None
        if cuda_available:
            try:
                cuda_device_name = torch.cuda.get_device_name(0)
            except Exception:
                cuda_device_name = None

        if requested == "auto":
            selected = "cuda" if cuda_available else "cpu"
        elif requested in {"cuda", "cpu"}:
            selected = requested
        else:
            raise SeparationError(f"Unsupported Demucs device: {requested_device!r}")

        if selected == "cuda" and not cuda_available:
            raise SeparationError("Demucs requested CUDA, but torch.cuda.is_available() is False.")

        return {
            "requested_device": requested_device,
            "selected_device": selected,
            "torch_version": getattr(torch, "__version__", None),
            "cuda_available": cuda_available,
            "cuda_device_name": cuda_device_name,
        }

    def _resolve_entrypoint(self) -> str:
        failures = []
        for module_name in ("vocal_tools.engines._demucs_cli", "demucs", "demucs.separate"):
            command = [sys.executable, "-B", "-m", module_name, "--help"]
            try:
                completed = subprocess.run(
                    command,
                    capture_output=True,
                    text=True,
                    errors="replace",
                    check=False,
                    timeout=30,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                failures.append(f"{module_name}: {exc}")
                continue
            if completed.returncode == 0:
                return module_name
            failures.append(
                f"{module_name}: exit {completed.returncode}; "
                f"stdout={self._tail(completed.stdout)!r}; stderr={self._tail(completed.stderr)!r}"
            )
        raise UnsupportedEngineError(
            "Demucs CLI module is not available. Install with `pip install vocal-tools[demucs]` "
            "or `pip install demucs`. Tried: " + " | ".join(failures)
        )

    def _build_command(
        self,
        *,
        module_name: str,
        model_name: str,
        output_dir: Path,
        selected_device: str,
        shifts: Any,
        overlap: Any,
        segment: Any,
        two_stems: Any,
        output_format: str,
        input_audio: Path,
    ) -> list[str]:
        command = [
            sys.executable,
            "-m",
            module_name,
            "-n",
            model_name,
            "--out",
            str(output_dir),
            "--device",
            selected_device,
            "--shifts",
            str(int(shifts if shifts is not None else 1)),
            "--overlap",
            str(float(overlap if overlap is not None else 0.25)),
        ]
        if segment is not None:
            command.extend(["--segment", str(float(segment))])
        if two_stems:
            command.extend(["--two-stems", str(two_stems)])
        if output_format == "mp3":
            command.append("--mp3")
        elif output_format == "flac":
            command.append("--flac")
        command.append(str(input_audio))
        return command

    def _collect_outputs(
        self,
        *,
        output_dir: Path,
        model_name: str,
        input_audio: Path,
        output_format: str,
        expected_stems: Iterable[str],
        two_stems: Any,
    ) -> tuple[Dict[str, Path], Dict[str, Any]]:
        suffix = f".{output_format.lower()}"
        search_roots = self._candidate_output_roots(output_dir, model_name, input_audio)
        audio_files = []
        for root in search_roots:
            audio_files.extend(self._audio_files(root, suffix))
            if audio_files:
                break
        if not audio_files:
            audio_files = self._audio_files(output_dir, suffix)

        by_stem: Dict[str, Path] = {}
        for path in audio_files:
            by_stem[path.stem.lower()] = path

        stems: Dict[str, Path] = {}
        if two_stems:
            selected = str(two_stems)
            inverse = f"no_{selected}"
            for preferred in (selected, inverse):
                path = by_stem.get(preferred.lower())
                if path is not None:
                    stems[preferred] = path
            for path in audio_files:
                stems.setdefault(path.stem, path)
        else:
            for stem in expected_stems:
                path = by_stem.get(str(stem).lower())
                if path is not None:
                    stems[str(stem)] = path
            if not stems:
                stems = {path.stem: path for path in audio_files}

        raw_outputs = {
            "two_stems": str(two_stems) if two_stems else None,
            "expected_inverse_stem": f"no_{two_stems}" if two_stems else None,
            "search_roots": [str(root) for root in search_roots],
            "files": {path.stem: str(path) for path in audio_files},
        }
        return stems, raw_outputs

    def _candidate_output_roots(self, output_dir: Path, model_name: str, input_audio: Path) -> list[Path]:
        track = input_audio.stem
        candidates = [
            output_dir / model_name / track,
            output_dir / track,
        ]
        model_root = output_dir / model_name
        if model_root.exists():
            candidates.extend(path for path in model_root.glob("*/") if path.is_dir() and path.name == track)

        roots = []
        seen = set()
        for path in candidates:
            key = str(path).lower()
            if key not in seen:
                roots.append(path)
                seen.add(key)
        return roots

    def _audio_files(self, root: Path, suffix: str) -> list[Path]:
        if not root.exists():
            return []
        return sorted(path for path in root.rglob(f"*{suffix}") if path.is_file())

    def _tail(self, text: str, limit: int = 1200) -> str:
        if len(text) <= limit:
            return text
        return text[-limit:]

    def _emit(
        self,
        progress_callback: Optional[ProgressCallback],
        *,
        stage: str,
        message: str,
        request: SeparationRequest,
        current: Optional[float] = None,
        total: Optional[float] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        if progress_callback is None:
            return
        progress_callback(
            ProgressEvent(
                stage=stage,
                message=message,
                current=current,
                total=total,
                model_id=request.model.id,
                engine=self.arch,
                extra=extra or {},
            )
        )
