from __future__ import annotations

from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence

from vocal_tools.catalog import ModelCatalog, ResolvedModel, resolve_model
from vocal_tools.core.errors import InvalidOptionError, UnsupportedEngineError
from vocal_tools.core.progress import ProgressCallback
from vocal_tools.core.types import OptionSpec, SeparationModelSpec, SeparationRequest, SeparationResult
from vocal_tools.engines import DemucsEngine, MDXCEngine, MDXOnnxEngine, VRArchEngine
from vocal_tools.engines.base import SeparationEngine


class Separator:
    def __init__(
        self,
        catalog: Optional[ModelCatalog] = None,
        model_dir=None,
        engines: Optional[Mapping[str, SeparationEngine]] = None,
    ):
        if catalog is None:
            if model_dir is None:
                raise ValueError("Either catalog or model_dir must be provided.")
            catalog = ModelCatalog(model_dir=model_dir)

        self.catalog = catalog
        self.engines: Dict[str, SeparationEngine] = {
            "mdx_onnx": MDXOnnxEngine(),
            "demucs": DemucsEngine(),
            "vr_arch": VRArchEngine(),
            "mdxc": MDXCEngine(),
        }
        if engines is not None:
            self.engines.update(dict(engines))

    def separate(
        self,
        input_audio,
        output_dir,
        model_id: str | None = "kim_vocal_2",
        options: Optional[Dict[str, object]] = None,
        progress_callback: Optional[ProgressCallback] = None,
        *,
        model: str | Path | None = None,
        models_dir: str | Path | None = None,
        model_dirs: Sequence[str | Path] | None = None,
        download_dir: str | Path | None = None,
        auto_download: bool = True,
        verify: bool = True,
    ) -> SeparationResult:
        input_audio = Path(input_audio)
        output_dir = Path(output_dir)
        if not input_audio.exists():
            raise FileNotFoundError(f"Input audio not found: {input_audio}")

        selected_model = model if model is not None else model_id
        if selected_model in (None, ""):
            selected_model = "kim_vocal_2"

        resolved = resolve_model(
            selected_model,
            catalog=self.catalog,
            models_dir=models_dir,
            model_dirs=model_dirs,
            download_dir=download_dir,
            auto_download=auto_download,
            verify=verify,
        )
        model_spec = self._model_spec_for_resolved(resolved)
        engine = self._get_engine(model_spec.arch)
        option_schema = engine.get_option_schema(model_spec)
        options_used = self._merge_options(model_spec.defaults, option_schema, options or {})
        self._validate_options(model_spec.defaults, option_schema, options_used, options or {})

        model_path = resolved.files.get("model")
        request = SeparationRequest(
            input_audio=input_audio,
            output_dir=output_dir,
            model=model_spec,
            model_path=model_path,
            options=options_used,
            option_overrides=dict(options or {}),
        )
        return engine.separate(request, progress_callback=progress_callback)

    def _get_engine(self, arch: str) -> SeparationEngine:
        try:
            return self.engines[arch]
        except KeyError as exc:
            raise UnsupportedEngineError(f"Unsupported separation engine: {arch}") from exc

    def _model_spec_for_resolved(self, resolved: ResolvedModel) -> SeparationModelSpec:
        catalog_model = resolved.metadata.get("catalog_model")
        if catalog_model is not None:
            return SeparationModelSpec.from_dict(catalog_model)

        if resolved.id is not None:
            return self.catalog.get_model(resolved.id)

        model_path = resolved.files.get("model")
        name = model_path.stem if model_path is not None else str(resolved.id or "model")
        arch = resolved.arch or self._infer_arch_from_model_path(model_path)
        return SeparationModelSpec(
            id=self._direct_model_id(model_path),
            name=name,
            arch=arch,
            filename=model_path.name if model_path is not None else None,
            tasks=[resolved.task] if resolved.task else [],
            output_stems=["vocals", "instrumental"] if arch == "mdx_onnx" else [],
            tags=[arch],
            description="Direct model file.",
            defaults={},
            options=[],
            metadata=dict(resolved.metadata),
        )

    def _infer_arch_from_model_path(self, model_path: Optional[Path]) -> str:
        if model_path is not None and model_path.suffix.lower() == ".onnx":
            return "mdx_onnx"
        raise UnsupportedEngineError(f"Could not infer separation engine for model path: {model_path}")

    def _direct_model_id(self, model_path: Optional[Path]) -> str:
        if model_path is None:
            return "direct_model"
        safe = "".join(char if char.isalnum() or char in ("-", "_") else "_" for char in model_path.stem.lower())
        return f"direct_{safe or 'model'}"

    def _merge_options(
        self,
        defaults: Dict[str, object],
        option_schema: list[OptionSpec],
        overrides: Dict[str, object],
    ) -> Dict[str, object]:
        merged = dict(defaults)
        for option in option_schema:
            if option.name not in merged:
                merged[option.name] = option.default
        merged.update(overrides)
        return merged

    def _validate_options(
        self,
        defaults: Dict[str, object],
        option_schema: list[OptionSpec],
        options_used: Dict[str, object],
        overrides: Dict[str, object],
    ) -> None:
        specs = {option.name: option for option in option_schema}
        allowed = set(defaults) | set(specs)
        unknown = sorted(name for name in overrides if name not in allowed)
        if unknown:
            raise InvalidOptionError(f"Unknown option(s): {', '.join(unknown)}")

        for name, spec in specs.items():
            if name in options_used:
                self._validate_option_value(spec, options_used[name])

    def _validate_option_value(self, spec: OptionSpec, value: object) -> None:
        if value is None:
            return

        if spec.type == "bool":
            valid = isinstance(value, bool)
        elif spec.type == "int":
            valid = isinstance(value, int) and not isinstance(value, bool)
        elif spec.type == "float":
            valid = isinstance(value, (int, float)) and not isinstance(value, bool)
        elif spec.type == "string":
            valid = isinstance(value, str)
        elif spec.type == "enum":
            valid = spec.choices is None or value in spec.choices
        elif spec.type == "dict":
            valid = isinstance(value, dict)
        elif spec.type == "list":
            valid = isinstance(value, list)
        else:
            raise InvalidOptionError(f"Unsupported option type for {spec.name!r}: {spec.type!r}")

        if not valid:
            raise InvalidOptionError(
                f"Invalid value for option {spec.name!r}: expected {spec.type}, got {type(value).__name__}."
            )

        if spec.choices is not None and value not in spec.choices:
            raise InvalidOptionError(f"Invalid value for option {spec.name!r}: {value!r}")
        if spec.min is not None and value < spec.min:
            raise InvalidOptionError(f"Option {spec.name!r} must be >= {spec.min}.")
        if spec.max is not None and value > spec.max:
            raise InvalidOptionError(f"Option {spec.name!r} must be <= {spec.max}.")
