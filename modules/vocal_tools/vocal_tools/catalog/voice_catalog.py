from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from vocal_tools.core.errors import ModelNotFoundError


_IGNORED_DIR_NAMES = {"mdx", "__pycache__"}


@dataclass(frozen=True)
class RVCVoiceModelCard:
    id: str
    name: str
    path: str
    pth_path: str
    index_path: Optional[str] = None
    has_index: bool = False
    file_size: Optional[int] = None
    index_size: Optional[int] = None
    downloaded: bool = True
    tags: List[str] = field(default_factory=lambda: ["rvc"])
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return _to_jsonable(asdict(self))

    def as_dict(self) -> dict[str, Any]:
        return self.to_dict()

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]


class RVCVoiceCatalog:
    def __init__(self, models_dir: str | Path):
        self.models_dir = Path(models_dir)
        self._voice_models: Dict[str, RVCVoiceModelCard] = {}
        self.scan()

    def scan(self) -> list[RVCVoiceModelCard]:
        self._voice_models = {}
        if not self.models_dir.exists():
            return []

        cards: list[RVCVoiceModelCard] = []
        for directory in sorted(self.models_dir.iterdir(), key=lambda item: item.name.lower()):
            if directory.is_dir():
                card = self._card_from_directory(directory)
                if card is not None:
                    cards.append(card)

        for pth_file in sorted(self.models_dir.glob("*.pth"), key=lambda item: item.name.lower()):
            card = self._card_from_pth_file(pth_file)
            if card is not None:
                cards.append(card)

        for card in cards:
            self._voice_models[card.id] = card
        return list(self._voice_models.values())

    def list_voice_models(self) -> list[RVCVoiceModelCard]:
        return list(self._voice_models.values())

    def get_voice_model(self, voice_model: str) -> RVCVoiceModelCard:
        key = str(voice_model)
        if key in self._voice_models:
            return self._voice_models[key]

        candidate = Path(key)
        if not candidate.is_absolute():
            candidate = self.models_dir / candidate
        if candidate.is_dir():
            card = self._card_from_directory(candidate)
            if card is not None:
                return card
        if candidate.is_file() and candidate.suffix.lower() == ".pth":
            card = self._card_from_pth_file(candidate)
            if card is not None:
                return card

        raise ModelNotFoundError(f"RVC voice model not found: {voice_model}")

    def _card_from_directory(self, directory: Path) -> Optional[RVCVoiceModelCard]:
        if directory.name.lower() in _IGNORED_DIR_NAMES or directory.name.startswith("."):
            return None
        pth_files = sorted(directory.glob("*.pth"), key=lambda item: item.name.lower())
        if not pth_files:
            return None
        index_files = sorted(directory.glob("*.index"), key=lambda item: item.name.lower())
        return self._build_card(
            model_id=directory.name,
            name=directory.name,
            model_root=directory,
            pth_files=pth_files,
            index_files=index_files,
        )

    def _card_from_pth_file(self, pth_file: Path) -> Optional[RVCVoiceModelCard]:
        if pth_file.name.lower() in {"hubert_base.pth", "rmvpe.pth"}:
            return None
        index_files = sorted(pth_file.parent.glob(f"{pth_file.stem}*.index"), key=lambda item: item.name.lower())
        return self._build_card(
            model_id=pth_file.stem,
            name=pth_file.stem,
            model_root=pth_file.parent,
            pth_files=[pth_file],
            index_files=index_files,
        )

    def _build_card(
        self,
        *,
        model_id: str,
        name: str,
        model_root: Path,
        pth_files: list[Path],
        index_files: list[Path],
    ) -> RVCVoiceModelCard:
        pth_path = pth_files[0]
        index_path = index_files[0] if index_files else None
        metadata = {
            "pth_candidates": [str(path) for path in pth_files],
            "index_candidates": [str(path) for path in index_files],
            "selected_pth": str(pth_path),
            "selected_index": str(index_path) if index_path is not None else None,
            "scan_root": str(self.models_dir),
        }
        if len(pth_files) > 1:
            metadata["warning"] = "Multiple .pth files found; using the first sorted file."

        return RVCVoiceModelCard(
            id=model_id,
            name=name,
            path=str(model_root),
            pth_path=str(pth_path),
            index_path=str(index_path) if index_path is not None else None,
            has_index=index_path is not None,
            file_size=_file_size(pth_path),
            index_size=_file_size(index_path),
            downloaded=pth_path.exists(),
            tags=["rvc"],
            metadata=metadata,
        )


def _file_size(path: Optional[Path]) -> Optional[int]:
    if path is None or not path.exists():
        return None
    try:
        return path.stat().st_size
    except OSError:
        return None


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
