from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

from vocal_tools.core.errors import SeparationError, UnsupportedEngineError
from vocal_tools.core.progress import ProgressCallback, ProgressEvent
from vocal_tools.core.types import OptionSpec, SeparationRequest, SeparationResult
from vocal_tools.engines.base import SeparationEngine
from vocal_tools.engines.external_cli import render_command_template, resolve_backend, runtime_info, tail
from vocal_tools.engines.output_collectors import collect_stem_outputs, list_audio_outputs


class MDXCEngine(SeparationEngine):
    arch = "mdxc"

    def get_option_schema(self, model):
        options = list(model.options)
        known = {option.name for option in options}
        extras = [
            OptionSpec(
                name="backend",
                type="enum",
                default="auto",
                label="Backend",
                choices=["auto", "external_cli", "python"],
                help="Adapter backend. Use external_cli to call a compatible MDXC/Roformer runner.",
                advanced=False,
            ),
            OptionSpec(
                name="command",
                type="list",
                default=None,
                label="Command",
                help="external_cli command template as a list of arguments.",
                advanced=True,
            ),
            OptionSpec(
                name="model_config_path",
                type="string",
                default=None,
                label="Model Config Path",
                help="Optional MDXC/Roformer model config path used by the command template.",
                advanced=True,
            ),
            OptionSpec(
                name="model_path",
                type="string",
                default=None,
                label="Model Path",
                help="Optional MDXC model path used by the command template.",
                advanced=True,
            ),
            OptionSpec(
                name="checkpoint_path",
                type="string",
                default=None,
                label="Checkpoint Path",
                help="Optional MDXC checkpoint path used by the command template.",
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

        output_dir = Path(request.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        options = dict(request.options)

        requested_backend = resolve_backend(options.get("backend"), engine_name="MDXC")
        backend = self._select_backend(requested_backend, options)
        if backend == "python":
            raise UnsupportedEngineError(
                "MDXC python backend is not bundled yet. Install a future `vocal-tools[mdxc]` "
                "backend or run with options {'backend': 'external_cli', 'command': [...]}."
            )
        if backend != "external_cli":
            raise UnsupportedEngineError(
                "MDXC backend could not be selected. Provide an external_cli command or install an MDXC backend."
            )

        requested_device = options.get("device") or request.model.defaults.get("device") or "auto"
        runtime = runtime_info(requested_device)
        return self._separate_external_cli(
            request=request,
            options=options,
            runtime=runtime,
            backend=backend,
            requested_backend=requested_backend,
            progress_callback=progress_callback,
        )

    def _select_backend(self, requested_backend: str, options: Dict[str, Any]) -> str:
        if requested_backend == "auto":
            if options.get("command"):
                return "external_cli"
            raise UnsupportedEngineError(
                "MDXC has no bundled backend yet. Use options "
                "{'backend': 'external_cli', 'command': [...]} or install a future `vocal-tools[mdxc]` backend."
            )
        return requested_backend

    def _separate_external_cli(
        self,
        *,
        request: SeparationRequest,
        options: Dict[str, Any],
        runtime: Dict[str, Any],
        backend: str,
        requested_backend: str,
        progress_callback: Optional[ProgressCallback],
    ) -> SeparationResult:
        input_audio = Path(request.input_audio)
        output_dir = Path(request.output_dir)
        model_path = self._option_path(options, "model_path") or request.model_path
        checkpoint_path = self._option_path(options, "checkpoint_path")
        model_config_path = self._option_path(options, "model_config_path")
        placeholders = self._placeholders(
            request=request,
            options=options,
            runtime=runtime,
            model_path=model_path,
            checkpoint_path=checkpoint_path,
            model_config_path=model_config_path,
        )
        command = render_command_template(
            options.get("command"),
            placeholders,
            engine_name="MDXC",
        )

        self._emit(
            progress_callback,
            stage="start",
            message="Starting MDXC external CLI separation.",
            request=request,
            extra={"backend": backend, "command": command, "runtime": runtime},
        )

        try:
            completed = subprocess.run(
                command,
                cwd=str(Path.cwd()),
                capture_output=True,
                text=True,
                errors="replace",
                check=False,
            )
        except OSError as exc:
            raise SeparationError(f"Could not run MDXC external CLI: {exc}") from exc

        if completed.returncode != 0:
            raise SeparationError(
                "MDXC external CLI failed with exit code "
                f"{completed.returncode}.\n"
                f"Command: {command}\n"
                f"stdout:\n{completed.stdout}\n"
                f"stderr:\n{completed.stderr}"
            )

        stems = collect_stem_outputs(
            output_dir=output_dir,
            stem_names=request.model.output_stems,
            input_stem=input_audio.stem,
        )
        raw_outputs = [str(path) for path in list_audio_outputs(output_dir)]
        if not stems:
            raise SeparationError(
                "MDXC external CLI completed but no expected output stems were found. "
                f"Expected {request.model.output_stems!r} under {output_dir}. "
                f"Found audio files: {raw_outputs}"
            )

        options_used = dict(options)
        options_used["backend"] = backend

        self._emit(
            progress_callback,
            stage="complete",
            message="MDXC external CLI separation complete.",
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
            options_used=options_used,
            metadata={
                "backend": backend,
                "requested_backend": requested_backend,
                "command": command,
                "model_config_path": str(model_config_path) if model_config_path is not None else None,
                "model_path": str(model_path) if model_path is not None else None,
                "checkpoint_path": str(checkpoint_path) if checkpoint_path is not None else None,
                "runtime": runtime,
                "raw_outputs": raw_outputs,
                "stdout": tail(completed.stdout),
                "stderr": tail(completed.stderr),
            },
        )

    def _placeholders(
        self,
        *,
        request: SeparationRequest,
        options: Dict[str, Any],
        runtime: Dict[str, Any],
        model_path: Optional[Path],
        checkpoint_path: Optional[Path],
        model_config_path: Optional[Path],
    ) -> Dict[str, Any]:
        stems = list(request.model.output_stems)
        return {
            "input_audio": Path(request.input_audio),
            "output_dir": Path(request.output_dir),
            "model_config_path": model_config_path,
            "model_path": model_path,
            "checkpoint_path": checkpoint_path,
            "model_id": request.model.id,
            "model_name": request.model.name,
            "input_stem": Path(request.input_audio).stem,
            "primary_stem": stems[0] if stems else None,
            "secondary_stem": stems[1] if len(stems) > 1 else None,
            "device": runtime["selected_device"],
            "selected_device": runtime["selected_device"],
            "requested_device": runtime["requested_device"],
            "backend": options.get("backend"),
        }

    def _option_path(self, options: Dict[str, Any], key: str) -> Optional[Path]:
        value = options.get(key)
        if value in (None, ""):
            return None
        return Path(str(value))

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
