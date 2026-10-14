from __future__ import annotations

from dataclasses import replace
import hashlib
import urllib.parse
import urllib.request
from pathlib import Path, PureWindowsPath
from typing import Any, Iterable, Mapping, Optional

from vocal_tools.core.errors import (
    ModelResolveError,
    ModelNotDownloadedError,
    ModelVerificationError,
)
from vocal_tools.core.progress import ProgressCallback, ProgressEvent
from vocal_tools.core.types import SeparationModelSpec


class ModelStore:
    def __init__(self, model_dir):
        self.model_dir = Path(model_dir)

    def resolve_model_path(self, model: SeparationModelSpec) -> Optional[Path]:
        if not model.filename:
            return None
        return self.resolve_artifact_path(model.filename, model.id)

    def resolve_artifact_path(self, filename: str, model_id: str) -> Path:
        return self.model_dir / self._safe_relative_path(filename, model_id)

    def get_model_path(self, model: SeparationModelSpec) -> Path:
        path = self.resolve_model_path(model)
        if path is None:
            raise ModelNotDownloadedError(f"Model {model.id!r} does not define a filename.")
        return path

    def get_existing_model_path(self, model: SeparationModelSpec) -> Path:
        path = self.get_model_path(model)
        if not path.exists():
            raise ModelNotDownloadedError(f"Model {model.id!r} is not downloaded: {path}")
        self._verify_or_raise(model, path)
        return path

    def is_downloaded(self, model: SeparationModelSpec) -> bool | str:
        if self.is_runtime_managed(model):
            return self._runtime_managed_status(model)
        path = self.resolve_model_path(model)
        if path is None or not path.exists():
            return False
        return self.verify_model(model, path)

    def package_manager(self, model: SeparationModelSpec) -> Optional[str]:
        managed_by = model.defaults.get("managed_by")
        return str(managed_by) if managed_by else None

    def is_package_managed(self, model: SeparationModelSpec) -> bool:
        managed_by = self.package_manager(model)
        return model.filename is None and managed_by is not None and managed_by != "external"

    def is_runtime_managed(self, model: SeparationModelSpec) -> bool:
        return model.filename is None and self.package_manager(model) is not None

    def ensure_model(
        self,
        model: SeparationModelSpec,
        progress_callback: Optional[ProgressCallback] = None,
    ) -> Optional[Path]:
        if self.is_runtime_managed(model):
            manager = self.package_manager(model)
            status = self._runtime_managed_status(model)
            self._emit(
                progress_callback,
                "verify",
                f"Using runtime-managed model ({manager}).",
                model,
                extra={"managed_by": manager, "download_status": status},
            )
            self._emit(
                progress_callback,
                "complete",
                "Runtime-managed model is ready.",
                model,
                extra={"managed_by": manager, "download_status": status},
            )
            return None

        path = self.resolve_model_path(model)
        if path is None:
            raise ModelNotDownloadedError(
                f"Model {model.id!r} does not define a filename or package manager."
            )

        if path.exists():
            self._emit(progress_callback, "verify", "Verifying local model file.", model)
            self._verify_or_raise(model, path)
            self._emit(progress_callback, "complete", "Local model file is ready.", model)
            return path

        if not model.download_url:
            raise ModelNotDownloadedError(
                f"Model {model.id!r} is not downloaded and no download_url is configured: {path}"
            )

        return self.download_model(model, progress_callback=progress_callback, overwrite=False)

    def ensure_model_files(
        self,
        model: SeparationModelSpec,
        artifacts: Iterable[Mapping[str, Any]],
        progress_callback: Optional[ProgressCallback] = None,
        *,
        verify: bool = True,
    ) -> dict[str, Path]:
        files: dict[str, Path] = {}
        for artifact in artifacts:
            key = str(artifact.get("key") or "model")
            filename = artifact.get("filename")
            required = bool(artifact.get("required", True))
            if not filename:
                if required:
                    raise ModelNotDownloadedError(
                        f"Model {model.id!r} does not define a filename for artifact {key!r}."
                    )
                continue

            path = self.resolve_artifact_path(str(filename), model.id)
            artifact_model = self._artifact_model(model, artifact, verify=verify)
            if path.exists():
                if verify:
                    self._verify_or_raise(artifact_model, path)
                files[key] = path
                continue

            if not artifact.get("download_url"):
                if required:
                    raise ModelNotDownloadedError(
                        f"Model {model.id!r} artifact {key!r} is not downloaded "
                        f"and no download_url is configured: {path}"
                    )
                continue

            downloaded = self.ensure_model(artifact_model, progress_callback=progress_callback)
            if downloaded is None:
                if required:
                    raise ModelNotDownloadedError(
                        f"Model {model.id!r} artifact {key!r} did not resolve to a file."
                    )
                continue
            files[key] = downloaded

        return files

    def download_model(
        self,
        model: SeparationModelSpec,
        progress_callback: Optional[ProgressCallback] = None,
        overwrite: bool = False,
    ) -> Path:
        if self.is_runtime_managed(model):
            raise ModelNotDownloadedError(
                f"Model {model.id!r} is managed by runtime {self.package_manager(model)!r}; "
                "vocal_tools does not download it."
            )

        path = self.resolve_model_path(model)
        if path is None:
            raise ModelNotDownloadedError(f"Model {model.id!r} does not define a filename.")

        if path.exists() and not overwrite:
            self._emit(progress_callback, "verify", "Verifying existing model file.", model)
            self._verify_or_raise(model, path)
            self._emit(progress_callback, "complete", "Existing model file is ready.", model)
            return path

        if not model.download_url:
            raise ModelNotDownloadedError(
                f"Model {model.id!r} has no download_url and cannot be downloaded."
            )

        path.parent.mkdir(parents=True, exist_ok=True)
        partial_path = path.with_name(f"{path.name}.part")
        self._emit(progress_callback, "download", "Downloading model file.", model)

        try:
            self._download(model.download_url, partial_path, model, progress_callback)
            if partial_path.exists():
                partial_path.replace(path)
            self._verify_or_raise(model, path)
        except Exception:
            if partial_path.exists():
                partial_path.unlink()
            if path.exists() and not self.verify_model(model, path):
                path.unlink()
            raise

        self._emit(progress_callback, "complete", "Downloaded model file is ready.", model)
        return path

    def delete_model(self, model: SeparationModelSpec) -> bool:
        if self.is_runtime_managed(model):
            return False
        path = self.resolve_model_path(model)
        if path is None or not path.exists():
            return False
        path.unlink()
        return True

    def verify_model(self, model: SeparationModelSpec, path: Optional[Path] = None) -> bool:
        model_path = Path(path) if path is not None else self.resolve_model_path(model)
        if model_path is None or not model_path.exists():
            return False

        try:
            if model.file_size is not None and model_path.stat().st_size != model.file_size:
                return False
            if model.sha256 is not None and self._sha256(model_path) != model.sha256.lower():
                return False
        except OSError:
            return False

        return True

    def get_download_status(self, model: SeparationModelSpec) -> dict[str, Any]:
        path = self.resolve_model_path(model)
        managed_by = self.package_manager(model)
        file_size = self._actual_file_size(path)
        verified = self.verify_model(model, path) if path is not None and path.exists() else False

        if self.is_runtime_managed(model):
            downloaded: bool | str = True
            status = self._runtime_managed_status(model)
            verified = True
        elif path is not None and path.exists() and verified:
            downloaded = True
            status = "downloaded"
        elif path is not None and path.exists() and not verified:
            downloaded = False
            status = "verification_failed"
        elif model.download_url:
            downloaded = False
            status = "missing"
        else:
            downloaded = False
            status = "not_downloadable"

        return {
            "model_id": model.id,
            "filename": model.filename,
            "managed_by": managed_by,
            "downloaded": downloaded,
            "download_status": status,
            "model_path": str(path) if path is not None else None,
            "file_size": file_size,
            "expected_file_size": model.file_size,
            "sha256": model.sha256,
            "verified": verified,
            "download_url": model.download_url,
        }

    def _runtime_managed_status(self, model: SeparationModelSpec) -> str:
        if self.package_manager(model) == "external":
            return "managed_externally"
        return "managed_by_package"

    def _download(
        self,
        url: str,
        partial_path: Path,
        model: SeparationModelSpec,
        progress_callback: Optional[ProgressCallback],
    ) -> None:
        source_path = Path(url)
        if source_path.exists():
            self._copy_local(source_path, partial_path, model, progress_callback)
            return

        parsed = urllib.parse.urlparse(url)
        if parsed.scheme.lower() == "file":
            source_path = Path(urllib.request.url2pathname(parsed.path))
            self._copy_local(source_path, partial_path, model, progress_callback)
            return

        if parsed.scheme == "" and source_path.exists():
            self._copy_local(source_path, partial_path, model, progress_callback)
            return

        downloaded = 0
        total = model.file_size
        with urllib.request.urlopen(url) as response:
            header_length = response.headers.get("Content-Length")
            if total is None and header_length:
                total = int(header_length)
            with partial_path.open("wb") as outfile:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    outfile.write(chunk)
                    downloaded += len(chunk)
                    self._emit(
                        progress_callback,
                        "download",
                        "Downloading model file.",
                        model,
                        current=downloaded,
                        total=total,
                    )

    def _copy_local(
        self,
        source_path: Path,
        partial_path: Path,
        model: SeparationModelSpec,
        progress_callback: Optional[ProgressCallback],
    ) -> None:
        if not source_path.exists():
            raise ModelNotDownloadedError(f"Download source file does not exist: {source_path}")
        copied = 0
        total = source_path.stat().st_size
        with source_path.open("rb") as infile, partial_path.open("wb") as outfile:
            for chunk in iter(lambda: infile.read(1024 * 1024), b""):
                outfile.write(chunk)
                copied += len(chunk)
                self._emit(
                    progress_callback,
                    "download",
                    "Copying local model file.",
                    model,
                    current=copied,
                    total=total,
                )

    def _actual_file_size(self, path: Optional[Path]) -> Optional[int]:
        if path is None or not path.exists():
            return None
        try:
            return path.stat().st_size
        except OSError:
            return None

    def _safe_relative_path(self, filename: str, model_id: str) -> Path:
        path = Path(filename)
        windows_path = PureWindowsPath(filename)
        if path.is_absolute() or windows_path.is_absolute() or ".." in path.parts or ".." in windows_path.parts:
            raise ModelResolveError(f"Unsafe model filename for {model_id!r}: {filename}")
        return path

    def _artifact_model(
        self,
        model: SeparationModelSpec,
        artifact: Mapping[str, Any],
        *,
        verify: bool,
    ) -> SeparationModelSpec:
        return replace(
            model,
            filename=str(artifact.get("filename")),
            download_url=artifact.get("download_url"),
            sha256=artifact.get("sha256") if verify else None,
            file_size=artifact.get("file_size") if verify else None,
        )

    def _sha256(self, path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as infile:
            for chunk in iter(lambda: infile.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _verify_or_raise(self, model: SeparationModelSpec, path: Path) -> None:
        if not path.exists():
            raise ModelNotDownloadedError(f"Model {model.id!r} is not downloaded: {path}")

        if model.file_size is not None:
            actual_size = path.stat().st_size
            if actual_size != model.file_size:
                raise ModelVerificationError(
                    f"Model file size mismatch for {model.id!r}: expected {model.file_size}, got {actual_size}."
                )

        if model.sha256 is not None:
            actual_sha256 = self._sha256(path)
            expected_sha256 = model.sha256.lower()
            if actual_sha256 != expected_sha256:
                raise ModelVerificationError(
                    f"Model sha256 mismatch for {model.id!r}: expected {expected_sha256}, got {actual_sha256}."
                )

    def _emit(
        self,
        progress_callback: Optional[ProgressCallback],
        stage: str,
        message: str,
        model: SeparationModelSpec,
        current: Optional[float] = None,
        total: Optional[float] = None,
        extra: Optional[dict] = None,
    ) -> None:
        if progress_callback is None:
            return
        progress_callback(
            ProgressEvent(
                stage=stage,
                message=message,
                current=current,
                total=total,
                model_id=model.id,
                engine=model.arch,
                extra=extra or {},
            )
        )
