from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Optional, Tuple


@dataclass(frozen=True)
class ProgressEvent:
    stage: str
    message: Optional[str] = None
    current: Optional[float] = None
    total: Optional[float] = None
    model_id: Optional[str] = None
    engine: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return _to_jsonable(asdict(self))

    def as_dict(self) -> Dict[str, Any]:
        return self.to_dict()

    def __iter__(self) -> Iterator[Tuple[str, Any]]:
        return iter(self.to_dict().items())


ProgressCallback = Callable[[ProgressEvent], None]


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return _to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_jsonable(item) for item in value]
    return value
