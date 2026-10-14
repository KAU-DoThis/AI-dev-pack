from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path, PureWindowsPath
from typing import Any, Optional, Sequence

from vocal_tools.catalog.model_catalog import ModelCatalog
from vocal_tools.catalog.model_store import ModelStore
from vocal_tools.core.errors import (
    ModelNotDownloadedError,
    ModelNotInstalledError,
    ModelPathNotFoundError,
    ModelResolveError,
)
from vocal_tools.core.types import SeparationModelSpec


UNSUPPORTED_DOWNLOAD_ARCHES = {
    "bandit",
    "mdx23c",
    "mdxc",
    "roformer",
    "scnet",
    "unknown",
    "vr_arch",
}


@dataclass(frozen=True)
class ResolvedModel:
    id: Optional[str]
    arch: Optional[str]
    task: Optional[str]
    source: str
    files: dict[str, Path]
    metadata: dict[str, Any]


def resolve_model(
    model: str | Path,
    *,
    arch: str | None = None,
    task: str | None = None,
    catalog: ModelCatalog | None = None,
    model_dirs: Sequence[str | Path] | None = None,
    models_dir: str | Path | None = None,
    download_dir: str | Path | None = None,
    auto_download: bool = False,
    verify: bool = True,
    allow_external: bool = True,
) -> ResolvedModel:
    """Resolve a model reference to local files, optionally downloading catalog models."""

    model_path = Path(model)
    model_text = str(model)
    search_dirs = _normalize_model_dirs(model_dirs, models_dir, catalog)
    download_root = _download_root(
        download_dir=download_dir,
        models_dir=models_dir,
        model_dirs=model_dirs,
        catalog=catalog,
    )

    if model_path.is_file():
        return _resolve_file(model_path, arch=arch, task=task, source="file")

    if model_path.is_dir():
        return _resolve_directory(model_path, arch=arch, task=task, source="directory")

    if _looks_like_path(model_text, model_path):
        raise ModelPathNotFoundError(f"Model path not found: {model_path}")

    local_model = _resolve_from_model_dirs(
        model_text,
        arch=arch,
        task=task,
        model_dirs=search_dirs,
    )
    if local_model is not None:
        return local_model

    if catalog is None:
        raise ModelResolveError(
            f"Model {model_text!r} was not found in local model directories and no catalog was provided."
        )

    catalog_model = catalog.get_model(model_text)
    _validate_catalog_model(catalog_model, arch=arch, task=task)
    return _resolve_catalog_model(
        catalog_model,
        task=task,
        model_dirs=search_dirs,
        download_root=download_root,
        auto_download=auto_download,
        verify=verify,
        allow_external=allow_external,
    )


def _resolve_file(
    path: Path,
    *,
    arch: str | None,
    task: str | None,
    source: str,
    model_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> ResolvedModel:
    suffix = path.suffix.lower()
    resolved_arch = _infer_arch_for_file(path, arch)

    if _is_mdx_arch(resolved_arch):
        if suffix != ".onnx":
            raise ModelResolveError(f"MDX model must be an .onnx file: {path}")
        return ResolvedModel(
            id=model_id,
            arch=resolved_arch,
            task=task,
            source=source,
            files={"model": path},
            metadata=dict(metadata or {}),
        )

    if _is_rvc_arch(resolved_arch):
        if suffix != ".pth":
            raise ModelResolveError(f"RVC voice model must be a .pth file: {path}")
        return ResolvedModel(
            id=model_id,
            arch=resolved_arch,
            task=task,
            source=source,
            files=_rvc_files(path, path.parent),
            metadata=dict(metadata or {}),
        )

    return ResolvedModel(
        id=model_id,
        arch=resolved_arch,
        task=task,
        source=source,
        files={"model": path},
        metadata=dict(metadata or {}),
    )


def _resolve_directory(
    path: Path,
    *,
    arch: str | None,
    task: str | None,
    source: str,
    model_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> ResolvedModel:
    resolved_arch = _infer_arch_for_directory(path, arch)

    if _is_mdx_arch(resolved_arch):
        model_path = _first_file(path, "*.onnx")
        if model_path is None:
            raise ModelPathNotFoundError(f"No MDX .onnx model found in: {path}")
        return ResolvedModel(
            id=model_id,
            arch=resolved_arch,
            task=task,
            source=source,
            files={"model": model_path},
            metadata=dict(metadata or {}),
        )

    if _is_rvc_arch(resolved_arch):
        model_path = _first_file(path, "*.pth")
        if model_path is None:
            raise ModelPathNotFoundError(f"No RVC .pth model found in: {path}")
        return ResolvedModel(
            id=model_id,
            arch=resolved_arch,
            task=task,
            source=source,
            files=_rvc_files(model_path, path),
            metadata=dict(metadata or {}),
        )

    raise ModelResolveError(f"No supported model file found in directory: {path}")


def _resolve_from_model_dirs(
    model_name: str,
    *,
    arch: str | None,
    task: str | None,
    model_dirs: Sequence[Path],
) -> ResolvedModel | None:
    for candidate in _local_candidates(model_name, arch=arch, model_dirs=model_dirs):
        if candidate.is_file():
            return _resolve_file(candidate, arch=arch, task=task, source="local")
        if candidate.is_dir():
            return _resolve_directory(candidate, arch=arch, task=task, source="local")
    return None


def _resolve_catalog_model(
    model: SeparationModelSpec,
    *,
    task: str | None,
    model_dirs: Sequence[Path],
    download_root: Path | None,
    auto_download: bool,
    verify: bool,
    allow_external: bool,
) -> ResolvedModel:
    resolved_task = _resolved_task(model, task)
    metadata = _catalog_metadata(model)

    if _is_rvc_arch(model.arch):
        return _resolve_catalog_rvc_voice_model(
            model,
            task=resolved_task,
            metadata=metadata,
            model_dirs=model_dirs,
            download_root=download_root,
            auto_download=auto_download,
            verify=verify,
        )

    if model.filename:
        path = _find_catalog_file(model, model_dirs)
        if path is not None:
            return _resolve_file(
                path,
                arch=model.arch,
                task=resolved_task,
                source="catalog",
                model_id=model.id,
                metadata=metadata,
            )
        if auto_download:
            downloaded_path = _download_catalog_model(
                model,
                download_root=download_root,
                verify=verify,
            )
            return _resolve_file(
                downloaded_path,
                arch=model.arch,
                task=resolved_task,
                source="downloaded",
                model_id=model.id,
                metadata=metadata,
            )
        raise ModelNotInstalledError(_not_installed_message(model, model_dirs, auto_download))

    managed_by = model.defaults.get("managed_by")
    if managed_by:
        if not allow_external:
            raise ModelResolveError(
                f"Model {model.id!r} is managed by {managed_by!r}, but external models are disabled."
            )
        return ResolvedModel(
            id=model.id,
            arch=model.arch,
            task=resolved_task,
            source="external",
            files={},
            metadata=metadata,
        )

    raise ModelNotInstalledError(
        f"Model {model.id!r} does not define a local filename or external package manager."
    )


def _resolve_catalog_rvc_voice_model(
    model: SeparationModelSpec,
    *,
    task: str | None,
    metadata: dict[str, Any],
    model_dirs: Sequence[Path],
    download_root: Path | None,
    auto_download: bool,
    verify: bool,
) -> ResolvedModel:
    model_path = _find_catalog_artifact(model, model_dirs, "filename")
    index_path = _find_catalog_artifact(model, model_dirs, "index_filename")
    if index_path is None and model_path is not None and not model.index_filename:
        index_path = _first_file(model_path.parent, "*.index")

    index_required = _catalog_rvc_index_required(model)
    if model_path is not None:
        if index_path is None and index_required:
            if auto_download:
                index_path = _download_catalog_rvc_index(
                    model,
                    download_root=download_root,
                    verify=verify,
                )
                files = {"model": model_path, "index": index_path}
                return ResolvedModel(
                    id=model.id,
                    arch=model.arch,
                    task=task,
                    source="downloaded",
                    files=files,
                    metadata=metadata,
                )
            raise ModelNotInstalledError(
                f"RVC voice model {model.id!r} is installed, but required index file "
                f"{model.index_filename!r} is missing. "
                "Pass auto_download=True to download it when index_download_url is configured."
            )

        files = {"model": model_path}
        if index_path is not None:
            files["index"] = index_path
        return ResolvedModel(
            id=model.id,
            arch=model.arch,
            task=task,
            source="catalog",
            files=files,
            metadata=metadata,
        )

    if auto_download:
        files = _download_catalog_rvc_voice_model(
            model,
            download_root=download_root,
            verify=verify,
        )
        return ResolvedModel(
            id=model.id,
            arch=model.arch,
            task=task,
            source="downloaded",
            files=files,
            metadata=metadata,
        )

    raise ModelNotInstalledError(_not_installed_message(model, model_dirs, auto_download))


def _download_catalog_model(
    model: SeparationModelSpec,
    *,
    download_root: Path | None,
    verify: bool,
) -> Path:
    _ensure_download_supported(model)
    if download_root is None:
        raise ModelNotInstalledError(
            f"Model {model.id!r} is not installed and no download directory could be determined."
        )

    store_model = model if verify else replace(model, sha256=None, file_size=None)
    store = ModelStore(download_root)
    try:
        path = store.ensure_model(store_model)
    except ModelNotDownloadedError as exc:
        raise ModelNotInstalledError(str(exc)) from exc

    if path is None:
        raise ModelResolveError(f"Model {model.id!r} did not resolve to a downloaded file.")
    return path


def _download_catalog_rvc_voice_model(
    model: SeparationModelSpec,
    *,
    download_root: Path | None,
    verify: bool,
) -> dict[str, Path]:
    _ensure_download_supported(model)
    if download_root is None:
        raise ModelNotInstalledError(
            f"Model {model.id!r} is not installed and no download directory could be determined."
        )

    store = ModelStore(download_root)
    try:
        files = store.ensure_model_files(
            model,
            _rvc_catalog_artifacts(model),
            verify=verify,
        )
    except ModelNotDownloadedError as exc:
        raise ModelNotInstalledError(str(exc)) from exc

    if "model" not in files:
        raise ModelResolveError(f"RVC voice model {model.id!r} did not resolve to a .pth file.")
    return files


def _download_catalog_rvc_index(
    model: SeparationModelSpec,
    *,
    download_root: Path | None,
    verify: bool,
) -> Path:
    _ensure_download_allowed(model)
    if download_root is None:
        raise ModelNotInstalledError(
            f"Model {model.id!r} index is not installed and no download directory could be determined."
        )
    if not model.index_filename:
        raise ModelNotInstalledError(f"Model {model.id!r} does not define an index_filename.")
    if not model.index_download_url:
        raise ModelNotInstalledError(
            f"Model {model.id!r} index is not installed and has no index_download_url configured."
        )

    store = ModelStore(download_root)
    try:
        files = store.ensure_model_files(
            model,
            [_rvc_index_artifact(model, required=True)],
            verify=verify,
        )
    except ModelNotDownloadedError as exc:
        raise ModelNotInstalledError(str(exc)) from exc

    try:
        return files["index"]
    except KeyError as exc:
        raise ModelResolveError(f"RVC voice model {model.id!r} did not resolve to an index file.") from exc


def _ensure_download_supported(model: SeparationModelSpec) -> None:
    _ensure_download_allowed(model)
    if not model.filename:
        raise ModelNotInstalledError(f"Model {model.id!r} does not define a downloadable filename.")

    if not model.download_url:
        raise ModelNotInstalledError(
            f"Model {model.id!r} is not installed and has no download_url configured."
        )


def _ensure_download_allowed(model: SeparationModelSpec) -> None:
    runnable = _coerce_runnable(model.metadata.get("runnable"))
    if runnable is False or _arch_key(model.arch) in UNSUPPORTED_DOWNLOAD_ARCHES:
        raise ModelResolveError(
            f"Model {model.id!r} cannot be downloaded for execution: "
            f"arch={model.arch!r}, unsupported_reason={model.metadata.get('unsupported_reason')!r}."
        )


def _validate_catalog_model(
    model: SeparationModelSpec,
    *,
    arch: str | None,
    task: str | None,
) -> None:
    if arch is not None and model.arch != arch:
        raise ModelResolveError(
            f"Catalog model {model.id!r} has arch {model.arch!r}, not requested arch {arch!r}."
        )
    if task is not None and task not in model.tasks:
        raise ModelResolveError(
            f"Catalog model {model.id!r} does not support requested task {task!r}."
        )


def _find_catalog_file(model: SeparationModelSpec, model_dirs: Sequence[Path]) -> Path | None:
    return _find_catalog_artifact(model, model_dirs, "filename")


def _find_catalog_artifact(
    model: SeparationModelSpec,
    model_dirs: Sequence[Path],
    field_name: str,
) -> Path | None:
    if not getattr(model, field_name, None):
        return None

    for candidate in _catalog_artifact_candidates(model, model_dirs, field_name):
        if candidate.is_file():
            return candidate
    return None


def _catalog_file_candidates(model: SeparationModelSpec, model_dirs: Sequence[Path]) -> list[Path]:
    return _catalog_artifact_candidates(model, model_dirs, "filename")


def _catalog_artifact_candidates(
    model: SeparationModelSpec,
    model_dirs: Sequence[Path],
    field_name: str,
) -> list[Path]:
    filename = _safe_catalog_artifact_filename(model, field_name)
    arch_dir = _arch_directory(model.arch)
    candidates: list[Path] = []

    for model_dir in model_dirs:
        candidates.append(model_dir / filename)
        if arch_dir is not None:
            candidates.append(model_dir / arch_dir / filename)
        candidates.append(model_dir / model.id / filename)
        if arch_dir is not None:
            candidates.append(model_dir / arch_dir / model.id / filename)

    return _dedupe_paths(candidates)


def _safe_catalog_filename(model: SeparationModelSpec) -> Path:
    return _safe_catalog_artifact_filename(model, "filename")


def _safe_catalog_artifact_filename(model: SeparationModelSpec, field_name: str) -> Path:
    raw_filename = getattr(model, field_name, None)
    filename = Path(raw_filename or "")
    windows_filename = PureWindowsPath(str(raw_filename or ""))
    if (
        filename.is_absolute()
        or windows_filename.is_absolute()
        or ".." in filename.parts
        or ".." in windows_filename.parts
    ):
        label = "filename" if field_name == "filename" else field_name
        raise ModelResolveError(f"Unsafe model {label} for {model.id!r}: {raw_filename}")
    return filename


def _rvc_catalog_artifacts(model: SeparationModelSpec) -> list[dict[str, Any]]:
    artifacts: list[dict[str, Any]] = [
        {
            "key": "model",
            "filename": model.filename,
            "download_url": model.download_url,
            "sha256": model.sha256,
            "file_size": model.file_size,
            "required": True,
        }
    ]
    if model.index_filename:
        artifacts.append(_rvc_index_artifact(model, required=_catalog_rvc_index_required(model)))
    return artifacts


def _rvc_index_artifact(model: SeparationModelSpec, *, required: bool) -> dict[str, Any]:
    return {
        "key": "index",
        "filename": model.index_filename,
        "download_url": model.index_download_url,
        "sha256": model.index_sha256,
        "file_size": model.index_file_size,
        "required": required,
    }


def _catalog_rvc_index_required(model: SeparationModelSpec) -> bool:
    if _coerce_runnable(model.metadata.get("index_required")) is True:
        return True
    return bool(model.index_filename and model.index_download_url)


def _local_candidates(
    model_name: str,
    *,
    arch: str | None,
    model_dirs: Sequence[Path],
) -> list[Path]:
    if _is_rvc_arch(arch):
        return _rvc_local_candidates(model_name, model_dirs)
    if _is_mdx_arch(arch):
        return _mdx_local_candidates(model_name, model_dirs)
    return _generic_local_candidates(model_name, model_dirs)


def _mdx_local_candidates(model_name: str, model_dirs: Sequence[Path]) -> list[Path]:
    name = Path(model_name).name
    stem = Path(name).stem
    candidates: list[Path] = []

    for model_dir in model_dirs:
        candidates.extend(
            [
                model_dir / name,
                model_dir / "mdx" / name,
                model_dir / stem / name,
                model_dir / "mdx" / stem / name,
            ]
        )
    return _dedupe_paths(candidates)


def _rvc_local_candidates(model_name: str, model_dirs: Sequence[Path]) -> list[Path]:
    name = Path(model_name).name
    path_name = Path(name)
    candidates: list[Path] = []

    for model_dir in model_dirs:
        candidates.extend(
            [
                model_dir / name,
                model_dir / "rvc" / name,
            ]
        )
        if path_name.suffix.lower() != ".pth":
            candidates.extend(
                [
                    model_dir / f"{name}.pth",
                    model_dir / "rvc" / f"{name}.pth",
                ]
            )
    return _dedupe_paths(candidates)


def _generic_local_candidates(model_name: str, model_dirs: Sequence[Path]) -> list[Path]:
    name = Path(model_name).name
    candidates: list[Path] = []

    for model_dir in model_dirs:
        candidates.extend(
            [
                model_dir / name,
                model_dir / "mdx" / name,
                model_dir / "rvc" / name,
            ]
        )
    return _dedupe_paths(candidates)


def _normalize_model_dirs(
    model_dirs: Sequence[str | Path] | None,
    models_dir: str | Path | None,
    catalog: ModelCatalog | None,
) -> list[Path]:
    paths: list[Path] = []
    if model_dirs is not None:
        paths.extend(Path(path) for path in model_dirs)
    if models_dir is not None:
        paths.append(Path(models_dir))
    if catalog is not None:
        paths.append(Path(catalog.model_dir))
    return _dedupe_paths(paths)


def _download_root(
    *,
    download_dir: str | Path | None,
    models_dir: str | Path | None,
    model_dirs: Sequence[str | Path] | None,
    catalog: ModelCatalog | None,
) -> Path | None:
    if download_dir is not None:
        return Path(download_dir)
    if models_dir is not None:
        return Path(models_dir)
    if model_dirs:
        return Path(model_dirs[0])
    if catalog is not None:
        return Path(catalog.store.model_dir)
    return None


def _looks_like_path(model_text: str, model_path: Path) -> bool:
    if model_path.is_absolute():
        return True
    if "/" in model_text or "\\" in model_text:
        return True
    return bool(PureWindowsPath(model_text).drive)


def _infer_arch_for_file(path: Path, arch: str | None) -> str | None:
    if arch is not None:
        return arch
    suffix = path.suffix.lower()
    if suffix == ".onnx":
        return "mdx_onnx"
    if suffix == ".pth":
        return "rvc_voice"
    return None


def _infer_arch_for_directory(path: Path, arch: str | None) -> str | None:
    if arch is not None:
        return arch
    if _first_file(path, "*.onnx") is not None:
        return "mdx_onnx"
    if _first_file(path, "*.pth") is not None:
        return "rvc_voice"
    return None


def _is_mdx_arch(arch: str | None) -> bool:
    return _arch_key(arch) in {"mdx", "mdx_onnx", "onnx"}


def _is_rvc_arch(arch: str | None) -> bool:
    return _arch_key(arch) in {"rvc", "rvc_voice", "voice_conversion"}


def _arch_key(arch: str | None) -> str | None:
    return arch.lower() if isinstance(arch, str) else None


def _coerce_runnable(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def _arch_directory(arch: str | None) -> str | None:
    if _is_mdx_arch(arch):
        return "mdx"
    if _is_rvc_arch(arch):
        return "rvc"
    return _arch_key(arch)


def _first_file(directory: Path, pattern: str) -> Path | None:
    files = sorted(path for path in directory.glob(pattern) if path.is_file())
    return files[0] if files else None


def _rvc_files(model_path: Path, search_dir: Path) -> dict[str, Path]:
    files = {"model": model_path}
    index_path = _first_file(search_dir, "*.index")
    if index_path is not None:
        files["index"] = index_path
    return files


def _resolved_task(model: SeparationModelSpec, task: str | None) -> str | None:
    if task is not None:
        return task
    if len(model.tasks) == 1:
        return model.tasks[0]
    return None


def _catalog_metadata(model: SeparationModelSpec) -> dict[str, Any]:
    metadata = dict(model.metadata)
    metadata.update(
        {
            "catalog_model": model.to_dict(),
            "managed_by": model.defaults.get("managed_by"),
        }
    )
    return metadata


def _not_installed_message(
    model: SeparationModelSpec,
    model_dirs: Sequence[Path],
    auto_download: bool,
) -> str:
    searched = ", ".join(str(path) for path in model_dirs) or "(no model_dirs)"
    hint = (
        "auto_download=True was requested, but this model could not be downloaded. "
        if auto_download
        else "Pass auto_download=True to download it when a download_url is configured. "
    )
    return (
        f"Model {model.id!r} is not installed in local model directories: {searched}. "
        f"{hint}"
    )


def _dedupe_paths(paths: Sequence[Path]) -> list[Path]:
    deduped: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path)
    return deduped


__all__ = ["ResolvedModel", "resolve_model"]
