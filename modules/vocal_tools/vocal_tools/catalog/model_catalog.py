from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union

from vocal_tools.catalog.remote_manifest import load_manifest_document
from vocal_tools.catalog.model_store import ModelStore
from vocal_tools.core.errors import (
    InvalidManifestError,
    ModelAlreadyExistsError,
    ModelNotFoundError,
)
from vocal_tools.core.types import OptionSpec, SeparationModelSpec


class ModelCatalog:
    def __init__(
        self,
        model_dir,
        builtin_manifest_path=None,
        user_manifest_path=None,
        remote_manifest_url: Optional[str] = None,
        remote_manifest_cache_path=None,
    ):
        self.model_dir = Path(model_dir)
        self.builtin_manifest_path = (
            Path(builtin_manifest_path)
            if builtin_manifest_path is not None
            else self._default_builtin_manifest_path()
        )
        self.user_manifest_path = Path(user_manifest_path) if user_manifest_path is not None else None
        self.remote_manifest_url = remote_manifest_url
        self.remote_manifest_cache_path = (
            Path(remote_manifest_cache_path) if remote_manifest_cache_path is not None else None
        )
        self.store = ModelStore(self.model_dir)
        self._builtin_models: Dict[str, SeparationModelSpec] = {}
        self._remote_models: Dict[str, SeparationModelSpec] = {}
        self._user_models: Dict[str, SeparationModelSpec] = {}
        self._models: Dict[str, SeparationModelSpec] = {}
        self._model_sources: Dict[str, str] = {}
        self._model_collisions: Dict[str, List[Dict[str, Any]]] = {}
        self._remote_manifest_info: Dict[str, Any] = {}
        self._load_models()
        if self.remote_manifest_url is not None:
            self.load_remote_manifest(self.remote_manifest_url, self.remote_manifest_cache_path)

    def list_models(
        self,
        arch: Optional[str] = None,
        task: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> List[SeparationModelSpec]:
        models = list(self._models.values())
        if arch is not None:
            models = [model for model in models if model.arch == arch]
        if task is not None:
            models = [model for model in models if task in model.tasks]
        if tag is not None:
            models = [model for model in models if tag in model.tags]
        return models

    @classmethod
    def with_uvr_remote(
        cls,
        model_dir,
        *,
        builtin_manifest_path=None,
        user_manifest_path=None,
        include_unsupported: bool = False,
        include_vr: bool = False,
        include_mdx: bool = True,
        include_demucs: bool = True,
        source_urls: Optional[dict[str, str]] = None,
        timeout: float = 30.0,
    ) -> "ModelCatalog":
        catalog = cls(
            model_dir=model_dir,
            builtin_manifest_path=builtin_manifest_path,
            user_manifest_path=user_manifest_path,
        )
        return catalog.load_uvr_remote(
            include_unsupported=include_unsupported,
            include_vr=include_vr,
            include_mdx=include_mdx,
            include_demucs=include_demucs,
            source_urls=source_urls,
            timeout=timeout,
        )

    def get_model(self, model_id: str) -> SeparationModelSpec:
        try:
            return self._models[model_id]
        except KeyError as exc:
            raise ModelNotFoundError(f"Model not found in catalog: {model_id}") from exc

    def get_option_schema(self, model_id: str) -> List[OptionSpec]:
        return list(self.get_model(model_id).options)

    def load_remote_manifest(
        self,
        url: str,
        cache_path: Union[str, Path, None] = None,
    ) -> "ModelCatalog":
        data, info = load_manifest_document(url, cache_path, use_cache=True)
        self.remote_manifest_url = url
        self.remote_manifest_cache_path = Path(cache_path) if cache_path is not None else None
        self._remote_manifest_info = dict(info)
        self._remote_models = self._read_manifest_data(data, source=url)
        self._refresh_models()
        return self

    def load_uvr_remote(
        self,
        *,
        include_unsupported: bool = False,
        include_vr: bool = False,
        include_mdx: bool = True,
        include_demucs: bool = True,
        source_urls: Optional[dict[str, str]] = None,
        timeout: float = 30.0,
    ) -> "ModelCatalog":
        from vocal_tools.catalog.uvr_importer import load_uvr_model_entries

        entries = load_uvr_model_entries(
            include_unsupported=include_unsupported,
            include_vr=include_vr,
            include_mdx=include_mdx,
            include_demucs=include_demucs,
            source_urls=source_urls,
            timeout=timeout,
        )
        self._remote_manifest_info = {
            "source_name": "uvr",
            "source_label": "uvr",
            "loaded_from": "uvr_importer",
            "include_unsupported": include_unsupported,
            "include_vr": include_vr,
            "include_mdx": include_mdx,
            "include_demucs": include_demucs,
            "source_urls": dict(source_urls or {}),
        }
        self.remote_manifest_url = None
        self.remote_manifest_cache_path = None
        self._remote_models = self._read_manifest_data({"models": entries}, source="uvr")
        self._refresh_models()
        return self

    def refresh_remote_manifest(
        self,
        url: Optional[str] = None,
        cache_path: Union[str, Path, None] = None,
    ) -> "ModelCatalog":
        resolved_url = url or self.remote_manifest_url
        if resolved_url is None:
            raise InvalidManifestError("No remote manifest URL is configured.")
        resolved_cache = cache_path if cache_path is not None else self.remote_manifest_cache_path
        data, info = load_manifest_document(resolved_url, resolved_cache, use_cache=False)
        self.remote_manifest_url = resolved_url
        self.remote_manifest_cache_path = Path(resolved_cache) if resolved_cache is not None else None
        self._remote_manifest_info = dict(info)
        self._remote_models = self._read_manifest_data(data, source=resolved_url)
        self._refresh_models()
        return self

    def list_sources(self) -> List[Dict[str, Any]]:
        remote_path = None
        if self.remote_manifest_cache_path is not None:
            remote_path = str(self.remote_manifest_cache_path)
        elif self._remote_manifest_info.get("path") is not None:
            remote_path = self._remote_manifest_info.get("path")
        return [
            {
                "source": "builtin",
                "count": len(self._builtin_models),
                "path": str(self.builtin_manifest_path) if self.builtin_manifest_path is not None else None,
            },
            {
                "source": self._remote_manifest_info.get("source_label", "remote"),
                "count": len(self._remote_models),
                "path": remote_path,
                "url": self.remote_manifest_url,
                "source_name": self._remote_manifest_info.get("source_name"),
                "manifest": dict(self._remote_manifest_info),
            },
            {
                "source": "user",
                "count": len(self._user_models),
                "path": str(self.user_manifest_path) if self.user_manifest_path is not None else None,
            },
        ]

    def register_local_model(
        self,
        model: Optional[SeparationModelSpec] = None,
        *,
        model_id: Optional[str] = None,
        name: Optional[str] = None,
        arch: Optional[str] = None,
        filename: Optional[str] = None,
        tasks: Optional[Sequence[str]] = None,
        output_stems: Optional[Sequence[str]] = None,
        tags: Optional[Sequence[str]] = None,
        description: Optional[str] = None,
        download_url: Optional[str] = None,
        sha256: Optional[str] = None,
        file_size: Optional[int] = None,
        defaults: Optional[Dict[str, Any]] = None,
        options: Optional[Sequence[Union[OptionSpec, Dict[str, Any]]]] = None,
        ui: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        overwrite: bool = False,
        save: bool = False,
    ) -> SeparationModelSpec:
        if model is None:
            model = self._build_model_spec(
                model_id=model_id,
                name=name,
                arch=arch,
                filename=filename,
                tasks=tasks,
                output_stems=output_stems,
                tags=tags,
                description=description,
                download_url=download_url,
                sha256=sha256,
                file_size=file_size,
                defaults=defaults,
                options=options,
                ui=ui,
                metadata=metadata,
            )
        else:
            model = self._coerce_model_spec(model, source="register_local_model", index=0)

        if model.id in self._models and not overwrite:
            raise ModelAlreadyExistsError(
                f"Model id already exists: {model.id!r}. Pass overwrite=True to replace it in the user manifest."
            )

        self._user_models[model.id] = model
        self._refresh_models()
        if save:
            self.save_user_manifest()
        return model

    def save_user_manifest(self, path=None) -> Path:
        manifest_path = Path(path) if path is not None else self.user_manifest_path
        if manifest_path is None:
            manifest_path = self.model_dir / "user_models.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)

        records = [model.to_dict() for model in self._user_models.values()]
        with manifest_path.open("w", encoding="utf-8") as outfile:
            json.dump({"models": records}, outfile, indent=2, ensure_ascii=False)
            outfile.write("\n")

        self.user_manifest_path = manifest_path
        return manifest_path

    def list_model_cards(
        self,
        arch: Optional[str] = None,
        task: Optional[str] = None,
        tag: Optional[str] = None,
        include_unsupported: bool = False,
        source: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        cards = [self._model_card(model) for model in self.list_models(arch=arch, task=task, tag=tag)]
        if not include_unsupported:
            cards = [card for card in cards if card.get("metadata", {}).get("runnable") is not False]
        if source is not None:
            cards = [card for card in cards if card.get("source") == source]
        return cards

    def to_model_cards(
        self,
        arch: Optional[str] = None,
        task: Optional[str] = None,
        tag: Optional[str] = None,
        include_unsupported: bool = False,
        source: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        return self.list_model_cards(
            arch=arch,
            task=task,
            tag=tag,
            include_unsupported=include_unsupported,
            source=source,
        )

    def _load_models(self) -> None:
        if self.builtin_manifest_path is None or not self.builtin_manifest_path.exists():
            raise InvalidManifestError(f"Builtin model manifest not found: {self.builtin_manifest_path}")

        self._builtin_models = self._read_manifest(self.builtin_manifest_path)
        self._user_models = {}
        if self.user_manifest_path is not None and self.user_manifest_path.exists():
            self._user_models = self._read_manifest(self.user_manifest_path)
        self._refresh_models()

    def _manifest_paths(self) -> Iterable[Path]:
        if self.builtin_manifest_path is not None and self.builtin_manifest_path.exists():
            yield self.builtin_manifest_path
        if self.remote_manifest_cache_path is not None and self.remote_manifest_cache_path.exists():
            yield self.remote_manifest_cache_path
        if self.user_manifest_path is not None and self.user_manifest_path.exists():
            yield self.user_manifest_path

    def _read_manifest(self, manifest_path: Path) -> Dict[str, SeparationModelSpec]:
        try:
            with manifest_path.open("r", encoding="utf-8") as infile:
                data = json.load(infile)
        except json.JSONDecodeError as exc:
            raise InvalidManifestError(
                f"Invalid JSON in model manifest {manifest_path}: {exc.msg} at line {exc.lineno}, column {exc.colno}."
            ) from exc
        except OSError as exc:
            raise InvalidManifestError(f"Could not read model manifest {manifest_path}: {exc}") from exc

        return self._read_manifest_data(data, source=str(manifest_path))

    def _read_manifest_data(
        self,
        data: Any,
        *,
        source: str,
    ) -> Dict[str, SeparationModelSpec]:
        records = self._manifest_records(data, source)
        models: Dict[str, SeparationModelSpec] = {}
        for index, record in enumerate(records):
            model = self._coerce_model_spec(record, source=source, index=index)
            if model.id in models:
                raise InvalidManifestError(
                    f"Duplicate model id {model.id!r} in manifest {source} at entry {index}."
                )
            models[model.id] = model
        return models

    def _manifest_records(self, data: Any, manifest_path: Union[str, Path]) -> List[Any]:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            records = data.get("models")
            if isinstance(records, list):
                return records
            raise InvalidManifestError(
                f"Model manifest {manifest_path} must be a list or a dict with a 'models' list."
            )
        raise InvalidManifestError(
            f"Model manifest {manifest_path} must contain a JSON list or object, got {type(data).__name__}."
        )

    def _refresh_models(self) -> None:
        models: Dict[str, SeparationModelSpec] = {}
        sources: Dict[str, str] = {}
        collisions: Dict[str, List[Dict[str, Any]]] = {}

        for source_name, source_models in (
            ("builtin", self._builtin_models),
            ("remote", self._remote_models),
            ("user", self._user_models),
        ):
            for model_id, model in source_models.items():
                if model_id in models:
                    collisions.setdefault(model_id, []).append(
                        {
                            "kept_source": source_name,
                            "replaced_source": sources[model_id],
                        }
                    )
                models[model_id] = model
                sources[model_id] = source_name

        self._models = models
        self._model_sources = sources
        self._model_collisions = collisions

    def _build_model_spec(
        self,
        *,
        model_id: Optional[str],
        name: Optional[str],
        arch: Optional[str],
        filename: Optional[str],
        tasks: Optional[Sequence[str]],
        output_stems: Optional[Sequence[str]],
        tags: Optional[Sequence[str]],
        description: Optional[str],
        download_url: Optional[str],
        sha256: Optional[str],
        file_size: Optional[int],
        defaults: Optional[Dict[str, Any]],
        options: Optional[Sequence[Union[OptionSpec, Dict[str, Any]]]],
        ui: Optional[Dict[str, Any]],
        metadata: Optional[Dict[str, Any]],
    ) -> SeparationModelSpec:
        missing = [
            field_name
            for field_name, value in {
                "model_id": model_id,
                "name": name,
                "arch": arch,
            }.items()
            if value in (None, "")
        ]
        if missing:
            raise InvalidManifestError(f"Missing required model registration field(s): {', '.join(missing)}.")

        return SeparationModelSpec(
            id=str(model_id),
            name=str(name),
            arch=str(arch),
            filename=str(filename) if filename is not None else None,
            tasks=list(tasks or []),
            output_stems=list(output_stems or []),
            tags=list(tags or []),
            description=description,
            download_url=download_url,
            sha256=sha256,
            file_size=file_size,
            defaults=dict(defaults or {}),
            options=[OptionSpec.from_dict(option) for option in options or []],
            ui=dict(ui or {}),
            metadata=dict(metadata or {}),
        )

    def _coerce_model_spec(self, record: Any, *, source: str, index: int) -> SeparationModelSpec:
        if isinstance(record, dict):
            missing_keys = [
                key
                for key in ("id", "name", "arch")
                if record.get(key) in (None, "")
            ]
            if missing_keys:
                raise InvalidManifestError(
                    f"Invalid model manifest entry {index} in {source}: "
                    f"missing required field(s): {', '.join(missing_keys)}."
                )
            if "options" in record and not isinstance(record["options"], list):
                raise InvalidManifestError(
                    f"Invalid model manifest entry {index} in {source}: options must be a list."
                )
            if record.get("ui") is not None and not isinstance(record["ui"], dict):
                raise InvalidManifestError(
                    f"Invalid model manifest entry {index} in {source}: ui must be an object."
                )
            if record.get("metadata") is not None and not isinstance(record["metadata"], dict):
                raise InvalidManifestError(
                    f"Invalid model manifest entry {index} in {source}: metadata must be an object."
                )
            for option_index, option in enumerate(record.get("options", [])):
                if isinstance(option, OptionSpec):
                    continue
                if not isinstance(option, dict):
                    raise InvalidManifestError(
                        f"Invalid option {option_index} for model entry {index} in {source}: "
                        f"expected an object, got {type(option).__name__}."
                    )
                missing_option_keys = [
                    key
                    for key in ("name", "type")
                    if option.get(key) in (None, "")
                ]
                if missing_option_keys:
                    raise InvalidManifestError(
                        f"Invalid option {option_index} for model entry {index} in {source}: "
                        f"missing required field(s): {', '.join(missing_option_keys)}."
                    )

        try:
            model = SeparationModelSpec.from_dict(record)
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidManifestError(
                f"Invalid model manifest entry {index} in {source}: {exc}"
            ) from exc

        required = {
            "id": model.id,
            "name": model.name,
            "arch": model.arch,
        }
        missing = [field_name for field_name, value in required.items() if not value]
        if missing:
            raise InvalidManifestError(
                f"Invalid model manifest entry {index} in {source}: missing {', '.join(missing)}."
            )
        return model

    def _model_card(self, model: SeparationModelSpec) -> Dict[str, Any]:
        card = model.to_dict()
        source = self._model_sources.get(model.id, "builtin")
        display_source = self._display_source(model, source)
        download_status = self.store.get_download_status(model)

        card["source"] = display_source
        card["downloaded"] = download_status["downloaded"]
        card["model_path"] = download_status["model_path"]
        card["managed_by"] = download_status["managed_by"]
        card["managed_by_package"] = download_status["download_status"] == "managed_by_package"
        card["download_status"] = download_status["download_status"]
        card["download"] = download_status

        card["ui"] = self._card_ui(model)
        metadata = dict(card.get("metadata") or {})
        metadata.update(
            {
                "source": display_source,
                "source_layer": source,
                "source_priority": self._source_priority(source),
            }
        )
        runnable = metadata.get("runnable")
        if runnable is None:
            runnable = self._model_runnable(model)
            metadata["runnable"] = runnable
        else:
            runnable = self._coerce_runnable(runnable)
            metadata["runnable"] = runnable
        if runnable is False and not metadata.get("unsupported_reason"):
            metadata["unsupported_reason"] = self._unsupported_reason(model)
        if model.id in self._model_collisions:
            metadata["conflicts"] = list(self._model_collisions[model.id])
        card["metadata"] = metadata
        return card

    def _card_ui(self, model: SeparationModelSpec) -> Dict[str, Any]:
        ui = {
            "category": self._default_category(model),
            "recommended": bool(model.ui.get("recommended", False)),
            "sort_order": int(model.ui.get("sort_order", self._default_sort_order(model))),
            "warning": model.ui.get("warning"),
        }
        ui.update(dict(model.ui))
        if "category" not in ui or ui["category"] in (None, ""):
            ui["category"] = self._default_category(model)
        if "sort_order" not in ui or ui["sort_order"] is None:
            ui["sort_order"] = self._default_sort_order(model)
        if "recommended" not in ui:
            ui["recommended"] = False
        if "warning" not in ui:
            ui["warning"] = None
        return ui

    def _default_category(self, model: SeparationModelSpec) -> str:
        if model.arch == "demucs":
            if "6-stem" in model.tags:
                return "Demucs 6-stem"
            if "4-stem" in model.tags:
                return "Demucs 4-stem"
            return "Demucs"
        if model.arch == "mdx_onnx":
            if "dereverb" in model.tags:
                return "MDX dereverb"
            if "karaoke" in model.tags:
                return "MDX karaoke"
            return "MDX vocal separation"
        if model.arch == "vr_arch":
            return "VR Arch"
        if model.arch == "mdxc":
            return "MDXC"
        return model.arch

    def _default_sort_order(self, model: SeparationModelSpec) -> int:
        source_order = {
            "builtin": 100,
            "remote": 200,
            "user": 300,
        }.get(self._model_sources.get(model.id, "builtin"), 900)
        arch_order = {
            "mdx_onnx": 0,
            "demucs": 50,
            "vr_arch": 500,
            "mdxc": 550,
        }.get(model.arch, 800)
        return source_order + arch_order

    def _source_priority(self, source: str) -> int:
        return {
            "builtin": 10,
            "remote": 20,
            "user": 30,
        }.get(source, 0)

    def _display_source(self, model: SeparationModelSpec, source: str) -> str:
        metadata_source = model.metadata.get("source")
        if source == "remote" and metadata_source == "uvr":
            return "uvr"
        return source

    def _model_runnable(self, model: SeparationModelSpec) -> bool:
        runnable = model.metadata.get("runnable")
        if runnable is not None:
            return self._coerce_runnable(runnable)
        return model.arch in {"mdx_onnx", "demucs"}

    def _unsupported_reason(self, model: SeparationModelSpec) -> Optional[str]:
        if self._model_runnable(model):
            return None
        return "engine_not_implemented"

    def _coerce_runnable(self, value: Any) -> bool:
        if isinstance(value, str):
            return value.strip().lower() not in {"0", "false", "no", "off"}
        return bool(value)

    def _default_builtin_manifest_path(self) -> Path:
        return Path(__file__).resolve().parent.parent / "manifests" / "builtin_models.json"
