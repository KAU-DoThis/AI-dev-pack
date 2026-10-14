from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from vocal_tools.core.errors import (
    ModelNotDownloadedError,
    SeparationError,
    UnsupportedEngineError,
    VocalToolsError,
)
from vocal_tools.core.progress import ProgressCallback, ProgressEvent
from vocal_tools.core.types import OptionSpec, SeparationRequest, SeparationResult
from vocal_tools.engines.base import SeparationEngine


class MDXOnnxEngine(SeparationEngine):
    arch = "mdx_onnx"

    def get_option_schema(self, model):
        options = list(model.options)
        known = {option.name for option in options}
        extras = [
            OptionSpec(
                name="m_threads",
                type="int",
                default=2,
                label="Worker Threads",
                min=1,
                help="Number of MDX worker threads.",
                advanced=True,
            ),
            OptionSpec(
                name="processor",
                type="int",
                default=None,
                label="Processor",
                help="CUDA device index. Leave unset for automatic selection.",
                advanced=True,
            ),
            OptionSpec(
                name="model_params",
                type="dict",
                default=None,
                label="Model Params",
                help="Inline MDX model_data parameters.",
                advanced=True,
            ),
            OptionSpec(
                name="model_params_path",
                type="string",
                default=None,
                label="Model Params Path",
                help="Optional path to MDX model_data.json.",
                advanced=True,
            ),
            OptionSpec(
                name="exclude_main",
                type="bool",
                default=False,
                label="Exclude Primary Stem",
                advanced=True,
            ),
            OptionSpec(
                name="exclude_inversion",
                type="bool",
                default=False,
                label="Exclude Secondary Stem",
                advanced=True,
            ),
        ]
        options.extend(option for option in extras if option.name not in known)
        return options

    def validate_model(self, model, model_path=None) -> None:
        super().validate_model(model, model_path)
        if model.filename and not model.filename.lower().endswith(".onnx"):
            raise UnsupportedEngineError(f"MDX ONNX model must use an .onnx file: {model.filename}")

    def separate(
        self,
        request: SeparationRequest,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> SeparationResult:
        self.validate_model(request.model, request.model_path)
        if request.model_path is None:
            raise ModelNotDownloadedError(f"Model path is required for {request.model.id!r}.")

        model_path = Path(request.model_path)
        output_dir = Path(request.output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        options = dict(request.options)

        self._emit(
            progress_callback,
            stage="start",
            message="Starting MDX ONNX separation.",
            request=request,
        )

        try:
            from vocal_tools.mdx import (
                _available_ort_providers,
                _cuda_available,
                _ort_providers,
                _select_processor,
                run_mdx,
            )
        except ImportError as exc:
            raise SeparationError(
                "MDX ONNX dependencies are not available. Install the MDX/audio runtime dependencies "
                "before running separation."
            ) from exc

        try:
            model_params, params_info = self._resolve_model_params(request, model_path)
            requested_processor = options.get("processor")
            selected_processor = _select_processor(requested_processor)
            runtime_info = {
                "requested_processor": requested_processor,
                "selected_processor": selected_processor,
                "cuda_available": _cuda_available(),
                "onnxruntime_available_providers": _available_ort_providers(),
                "onnxruntime_selected_providers": list(_ort_providers(selected_processor)),
            }
            primary_stem = options.get("primary_stem") or self._stem_name(request, 0, "primary")
            secondary_stem = options.get("secondary_stem") or self._stem_name(request, 1, "secondary")
            options_used = dict(options)
            options_used["model_params_source"] = params_info["source"]
            if params_info.get("path") is not None:
                options_used["model_params_path"] = params_info["path"]

            self._emit(
                progress_callback,
                stage="running",
                message="Running MDX ONNX model.",
                request=request,
            )
            m_threads = options.get("m_threads", 2)
            if m_threads is None:
                m_threads = 2
            primary_path, secondary_path = run_mdx(
                model_params,
                output_dir,
                model_path,
                request.input_audio,
                exclude_main=bool(options.get("exclude_main", False)),
                exclude_inversion=bool(options.get("exclude_inversion", False)),
                suffix=primary_stem,
                invert_suffix=secondary_stem,
                denoise=bool(options.get("denoise", False)),
                keep_orig=True,
                m_threads=int(m_threads),
                processor=options.get("processor"),
            )
        except VocalToolsError:
            raise
        except Exception as exc:
            raise SeparationError(f"MDX ONNX separation failed: {exc}") from exc

        stems = {}
        if primary_path is not None:
            stems[primary_stem] = Path(primary_path)
        if secondary_path is not None:
            stems[secondary_stem] = Path(secondary_path)

        self._emit(
            progress_callback,
            stage="complete",
            message="MDX ONNX separation complete.",
            request=request,
            current=1,
            total=1,
            extra={"stems": {name: str(path) for name, path in stems.items()}},
        )

        return SeparationResult(
            input_audio=Path(request.input_audio),
            output_dir=output_dir,
            model_id=request.model.id,
            model_name=request.model.name,
            arch=request.model.arch,
            stems=stems,
            options_used=options_used,
            metadata={
                "model_path": str(model_path),
                "model_params": params_info,
                "runtime": runtime_info,
                "raw_outputs": {
                    "primary": str(primary_path) if primary_path is not None else None,
                    "secondary": str(secondary_path) if secondary_path is not None else None,
                },
            },
        )

    def _resolve_model_params(
        self,
        request: SeparationRequest,
        model_path: Path,
    ) -> Tuple[Dict[str, Any], Dict[str, Optional[str]]]:
        overrides = dict(request.option_overrides or {})
        defaults = dict(request.model.defaults or {})

        if overrides.get("model_params") is not None:
            return self._validate_model_params(
                overrides["model_params"],
                {"source": "options.model_params", "path": None},
            )

        if overrides.get("model_params_path"):
            path = Path(str(overrides["model_params_path"]))
            return self._load_model_params_file(path, "options.model_params_path")

        if defaults.get("model_params") is not None:
            return self._validate_model_params(
                defaults["model_params"],
                {"source": "model.defaults.model_params", "path": None},
            )

        if defaults.get("model_params_path"):
            path = Path(str(defaults["model_params_path"]))
            return self._load_model_params_file(path, "model.defaults.model_params_path")

        default_path = model_path.parent / "model_data.json"
        if default_path.exists():
            return self._load_model_params_file(default_path, "model_path.parent/model_data.json")

        raise SeparationError(
            "MDX model parameters were not provided. Set options['model_params'], "
            "options['model_params_path'], model.defaults['model_params'], "
            f"model.defaults['model_params_path'], or place model_data.json next to {model_path}."
        )

    def _load_model_params_file(
        self,
        path: Path,
        source: str,
    ) -> Tuple[Dict[str, Any], Dict[str, Optional[str]]]:
        if not path.exists():
            raise SeparationError(f"MDX model params file not found for {source}: {path}")

        try:
            with path.open("r", encoding="utf-8") as infile:
                data = json.load(infile)
        except json.JSONDecodeError as exc:
            raise SeparationError(
                f"Invalid MDX model params JSON at {path}: {exc.msg} at line {exc.lineno}, column {exc.colno}."
            ) from exc
        except OSError as exc:
            raise SeparationError(f"Could not read MDX model params file {path}: {exc}") from exc

        return self._validate_model_params(data, {"source": source, "path": str(path)})

    def _validate_model_params(
        self,
        data: Any,
        info: Dict[str, Optional[str]],
    ) -> Tuple[Dict[str, Any], Dict[str, Optional[str]]]:
        if not isinstance(data, dict):
            raise SeparationError(
                f"MDX model params from {info['source']} must be a JSON object, got {type(data).__name__}."
            )
        return data, info

    def _stem_name(self, request: SeparationRequest, index: int, fallback: str) -> str:
        try:
            return request.model.output_stems[index]
        except IndexError:
            return fallback

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
