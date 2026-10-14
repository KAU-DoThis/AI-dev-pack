from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Union


Number = Union[int, float]


@dataclass(frozen=True)
class OptionSpec:
    name: str
    type: str
    default: Any = None
    label: Optional[str] = None
    group: Optional[str] = None
    unit: Optional[str] = None
    step: Optional[Number] = None
    placeholder: Optional[str] = None
    min: Optional[Number] = None
    max: Optional[Number] = None
    choices: Optional[List[Any]] = None
    help: Optional[str] = None
    advanced: bool = False
    visible_when: Optional[Dict[str, Any]] = None
    disabled_when: Optional[Dict[str, Any]] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OptionSpec":
        if isinstance(data, cls):
            return data
        if not isinstance(data, dict):
            raise TypeError(f"OptionSpec data must be a dict, got {type(data).__name__}.")
        return cls(
            name=data["name"],
            label=data.get("label"),
            group=data.get("group"),
            unit=data.get("unit"),
            step=data.get("step"),
            placeholder=data.get("placeholder"),
            type=data["type"],
            default=data.get("default"),
            min=data.get("min"),
            max=data.get("max"),
            choices=list(data["choices"]) if data.get("choices") is not None else None,
            help=data.get("help"),
            advanced=bool(data.get("advanced", False)),
            visible_when=dict(data["visible_when"]) if data.get("visible_when") is not None else None,
            disabled_when=dict(data["disabled_when"]) if data.get("disabled_when") is not None else None,
        )

    def to_dict(self) -> Dict[str, Any]:
        return _to_jsonable(asdict(self))

    def as_dict(self) -> Dict[str, Any]:
        return self.to_dict()


@dataclass(frozen=True)
class SeparationModelSpec:
    id: str
    name: str
    arch: str
    filename: Optional[str] = None
    index_filename: Optional[str] = None
    tasks: List[str] = field(default_factory=list)
    output_stems: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    description: Optional[str] = None
    download_url: Optional[str] = None
    index_download_url: Optional[str] = None
    sha256: Optional[str] = None
    index_sha256: Optional[str] = None
    file_size: Optional[int] = None
    index_file_size: Optional[int] = None
    defaults: Dict[str, Any] = field(default_factory=dict)
    options: List[OptionSpec] = field(default_factory=list)
    ui: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SeparationModelSpec":
        if isinstance(data, cls):
            return data
        if not isinstance(data, dict):
            raise TypeError(f"SeparationModelSpec data must be a dict, got {type(data).__name__}.")
        return cls(
            id=data["id"],
            name=data["name"],
            arch=data["arch"],
            filename=data.get("filename"),
            index_filename=data.get("index_filename"),
            tasks=list(data.get("tasks", [])),
            output_stems=list(data.get("output_stems", [])),
            tags=list(data.get("tags", [])),
            description=data.get("description"),
            download_url=data.get("download_url"),
            index_download_url=data.get("index_download_url"),
            sha256=data.get("sha256"),
            index_sha256=data.get("index_sha256"),
            file_size=data.get("file_size"),
            index_file_size=data.get("index_file_size"),
            defaults=dict(data.get("defaults", {})),
            options=[OptionSpec.from_dict(option) for option in data.get("options", [])],
            ui=dict(data.get("ui", {})),
            metadata=dict(data.get("metadata", {})),
        )

    def to_dict(self) -> Dict[str, Any]:
        return _to_jsonable(asdict(self))

    def as_dict(self) -> Dict[str, Any]:
        return self.to_dict()


@dataclass(frozen=True)
class SeparationRequest:
    input_audio: Path
    output_dir: Path
    model: SeparationModelSpec
    model_path: Optional[Path]
    options: Dict[str, Any] = field(default_factory=dict)
    option_overrides: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SeparationResult:
    input_audio: Path
    output_dir: Path
    model_id: str
    model_name: str
    arch: str
    stems: Dict[str, Path] = field(default_factory=dict)
    options_used: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "input_audio": str(self.input_audio),
            "output_dir": str(self.output_dir),
            "model_id": self.model_id,
            "model_name": self.model_name,
            "arch": self.arch,
            "stems": {name: str(path) for name, path in self.stems.items()},
            "options_used": _to_jsonable(self.options_used),
            "metadata": _to_jsonable(self.metadata),
        }

    def as_dict(self) -> Dict[str, Any]:
        return self.to_dict()


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
