from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

from vocal_tools.core.errors import ModelNotDownloadedError, UnsupportedEngineError
from vocal_tools.core.progress import ProgressCallback
from vocal_tools.core.types import OptionSpec, SeparationModelSpec, SeparationRequest, SeparationResult


class SeparationEngine(ABC):
    arch: str

    def get_option_schema(self, model: SeparationModelSpec) -> list[OptionSpec]:
        return list(model.options)

    def validate_model(self, model: SeparationModelSpec, model_path: Optional[Path] = None) -> None:
        if model.arch != self.arch:
            raise UnsupportedEngineError(
                f"Model {model.id!r} uses arch {model.arch!r}, not {self.arch!r}."
            )
        if model_path is not None and not Path(model_path).exists():
            raise ModelNotDownloadedError(f"Model file is missing: {model_path}")

    @abstractmethod
    def separate(
        self,
        request: SeparationRequest,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> SeparationResult:
        raise NotImplementedError
