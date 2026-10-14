from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable, Optional

from vocal_tools.core.errors import InvalidManifestError


DEFAULT_UVR_SOURCES = {
    "download_checks": (
        "https://raw.githubusercontent.com/Anjok07/ultimatevocalremovergui/master/"
        "gui_data/download_checks.json"
    ),
    "mdx_model_data": (
        "https://raw.githubusercontent.com/TRvlvr/model_repo/main/model_data/mdx_model_data.json"
    ),
    "vr_model_data": (
        "https://raw.githubusercontent.com/TRvlvr/model_repo/main/model_data/vr_model_data.json"
    ),
    "demucs_model_names": (
        "https://raw.githubusercontent.com/TRvlvr/model_repo/main/model_data/"
        "demucs_model_name_mapper.json"
    ),
    "normal_repo": "https://github.com/TRvlvr/model_repo/releases/download/all_public_uvr_models/",
}

RUNNABLE_ARCHES = {"mdx_onnx", "demucs"}
UNSUPPORTED_ARCHES = {
    "bandit",
    "mdx23c",
    "mdxc",
    "roformer",
    "scnet",
    "unknown",
    "vr_arch",
}


def load_uvr_model_entries(
    *,
    include_unsupported: bool = False,
    include_vr: bool = False,
    include_mdx: bool = True,
    include_demucs: bool = True,
    source_urls: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> list[dict]:
    importer = UVRModelImporter(
        include_unsupported=include_unsupported,
        include_vr=include_vr,
        include_mdx=include_mdx,
        include_demucs=include_demucs,
        source_urls=source_urls,
        timeout=timeout,
    )
    return importer.load()


class UVRModelImporter:
    def __init__(
        self,
        *,
        include_unsupported: bool = False,
        include_vr: bool = False,
        include_mdx: bool = True,
        include_demucs: bool = True,
        source_urls: dict[str, str] | None = None,
        timeout: float = 30.0,
    ):
        self.include_unsupported = bool(include_unsupported)
        self.include_vr = bool(include_vr)
        self.include_mdx = bool(include_mdx)
        self.include_demucs = bool(include_demucs)
        self.source_urls = dict(DEFAULT_UVR_SOURCES)
        if source_urls:
            self.source_urls.update(source_urls)
        self.timeout = float(timeout)

    def load(self) -> list[dict]:
        return self.normalize(self.fetch())

    def fetch(self) -> dict[str, Any]:
        raw: dict[str, Any] = {}
        if self.include_mdx:
            raw["mdx_model_data"] = self._load_json_source("mdx_model_data")
        if self.include_vr:
            raw["vr_model_data"] = self._load_json_source("vr_model_data")
        if self.include_demucs:
            raw["demucs_model_names"] = self._load_json_source("demucs_model_names")

        download_checks_url = self.source_urls.get("download_checks")
        if download_checks_url:
            raw["download_checks"] = self._load_json(download_checks_url)
        return raw

    def normalize(self, raw_data: Any) -> list[dict]:
        raw_sources = raw_data if isinstance(raw_data, dict) else {"mdx_model_data": raw_data}
        download_index = _download_index(raw_sources.get("download_checks"))
        entries: list[dict] = []

        if self.include_mdx:
            entries.extend(
                self._normalize_records(
                    raw_sources.get("mdx_model_data"),
                    family="mdx",
                    download_index=download_index,
                )
            )

        if self.include_vr:
            entries.extend(
                self._normalize_records(
                    raw_sources.get("vr_model_data"),
                    family="vr",
                    download_index=download_index,
                )
            )

        if self.include_demucs:
            entries.extend(
                self._normalize_records(
                    raw_sources.get("demucs_model_names"),
                    family="demucs",
                    download_index=download_index,
                )
            )

        if not self.include_unsupported:
            entries = [entry for entry in entries if entry["metadata"].get("runnable") is not False]
        return _dedupe_entries(entries)

    def _normalize_records(
        self,
        data: Any,
        *,
        family: str,
        download_index: dict[str, dict[str, Any]],
    ) -> list[dict]:
        entries: list[dict] = []
        for record in _iter_records(data):
            entry = self._normalize_record(record, family=family, download_index=download_index)
            if entry is not None:
                entries.append(entry)
        return entries

    def _normalize_record(
        self,
        record: dict[str, Any],
        *,
        family: str,
        download_index: dict[str, dict[str, Any]],
    ) -> dict | None:
        filename = _record_filename(record, family=family)
        name = _record_name(record, filename=filename)
        if not name:
            return None

        arch = _detect_arch(record, family=family, filename=filename, name=name)
        runnable = arch in RUNNABLE_ARCHES
        unsupported_reason = None if runnable else "engine_not_implemented"

        download_info = _lookup_download_info(download_index, filename=filename, name=name)
        download_url = _record_download_url(record) or download_info.get("download_url")
        if download_url is None and filename is not None and arch != "demucs":
            download_url = _join_url(self.source_urls.get("normal_repo"), filename)

        downloadable = bool(download_url)
        if "downloadable" in record:
            downloadable = bool(record.get("downloadable"))
        elif "available" in record:
            downloadable = bool(record.get("available"))

        defaults = _defaults_for_arch(arch, name=name)
        tags = _tags_for_arch(arch)
        metadata = {
            "source": "uvr",
            "uvr_name": record.get("uvr_name") or record.get("original_name") or filename or name,
            "runnable": runnable,
            "downloadable": downloadable,
            "unsupported_reason": unsupported_reason,
            "detected_arch": arch,
        }

        return {
            "id": _uvr_id(filename or name),
            "name": _display_name(name),
            "arch": arch,
            "filename": filename if arch != "demucs" else None,
            "tasks": _tasks_for_arch(arch),
            "output_stems": _output_stems_for_arch(arch, name=name),
            "tags": tags,
            "description": "Imported from UVR metadata.",
            "download_url": download_url,
            "sha256": _record_sha256(record) or download_info.get("sha256"),
            "file_size": _record_file_size(record) or download_info.get("file_size"),
            "defaults": defaults,
            "options": [],
            "ui": {
                "category": _category_for_arch(arch),
                "recommended": False,
                "sort_order": _sort_order_for_arch(arch),
            },
            "metadata": metadata,
        }

    def _load_json_source(self, key: str) -> Any:
        url = self.source_urls.get(key)
        if not url:
            return []
        return self._load_json(url)

    def _load_json(self, url: str) -> Any:
        try:
            return _load_json_url(url, timeout=self.timeout)
        except OSError as exc:
            raise InvalidManifestError(f"Could not read UVR source {url}: {exc}") from exc


def _load_json_url(url: str, *, timeout: float) -> Any:
    parsed = urllib.parse.urlparse(url)
    scheme = parsed.scheme.lower()

    if scheme in ("http", "https"):
        with urllib.request.urlopen(url, timeout=timeout) as response:
            text = response.read().decode("utf-8")
        return json.loads(text)

    if scheme == "file":
        path = Path(urllib.request.url2pathname(parsed.path))
        return json.loads(path.read_text(encoding="utf-8"))

    path = Path(url)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))

    raise InvalidManifestError(f"UVR source is not a readable URL or local JSON file: {url}")


def _iter_records(data: Any) -> Iterable[dict[str, Any]]:
    if data is None:
        return

    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                yield dict(item)
            elif isinstance(item, str):
                yield {"name": item, "filename": item}
        return

    if isinstance(data, dict):
        for key in ("models", "model_data", "data", "model_names"):
            value = data.get(key)
            if isinstance(value, (dict, list)):
                yield from _iter_records(value)
                return

        for key, value in data.items():
            if isinstance(value, dict):
                record = dict(value)
                record.setdefault("name", key)
                if _has_known_model_suffix(key):
                    record.setdefault("filename", key)
                yield record
            elif isinstance(value, str):
                yield {"name": key, "filename": value}
            else:
                yield {"name": key}


def _download_index(data: Any) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for record in _iter_records(data):
        filename = _record_filename(record, family="unknown")
        name = _record_name(record, filename=filename)
        if filename is None and not name:
            continue
        info = {
            "download_url": _record_download_url(record),
            "sha256": _record_sha256(record),
            "file_size": _record_file_size(record),
        }
        for key in {filename, name, Path(filename).stem if filename else None}:
            if key:
                index[_normalize_lookup_key(key)] = info
    return index


def _lookup_download_info(
    index: dict[str, dict[str, Any]],
    *,
    filename: str | None,
    name: str,
) -> dict[str, Any]:
    for key in (filename, name, Path(filename).stem if filename else None):
        if key is None:
            continue
        info = index.get(_normalize_lookup_key(key))
        if info is not None:
            return dict(info)
    return {}


def _record_filename(record: dict[str, Any], *, family: str) -> str | None:
    keys = (
        "filename",
        "file_name",
        "model_filename",
        "model_file",
        "model",
        "download_name",
        "name",
    )
    for key in keys:
        value = record.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        candidate = Path(value.strip()).name
        if _has_known_model_suffix(candidate):
            return candidate

    name = str(record.get("name") or record.get("model_name") or "").strip()
    if not name:
        return None
    if family == "mdx" and not Path(name).suffix:
        return f"{name}.onnx"
    if family == "vr" and not Path(name).suffix:
        return f"{name}.pth"
    return Path(name).name if _has_known_model_suffix(name) else None


def _record_name(record: dict[str, Any], *, filename: str | None) -> str:
    for key in ("display_name", "model_name", "name", "title"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return Path(value.strip()).stem
    if filename:
        return Path(filename).stem
    return ""


def _record_download_url(record: dict[str, Any]) -> str | None:
    for key in ("download_url", "url", "link", "download_link"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _record_sha256(record: dict[str, Any]) -> str | None:
    for key in ("sha256", "sha_256", "checksum"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return None


def _record_file_size(record: dict[str, Any]) -> int | None:
    for key in ("file_size", "size", "bytes"):
        value = record.get(key)
        if value in (None, ""):
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


def _detect_arch(
    record: dict[str, Any],
    *,
    family: str,
    filename: str | None,
    name: str,
) -> str:
    explicit = record.get("arch") or record.get("architecture") or record.get("type") or record.get("model_type")
    text = " ".join(str(item) for item in (explicit, family, filename, name) if item).lower()

    if "mdx23c" in text or "mdx-23c" in text or "mdx 23c" in text:
        return "mdx23c"
    if "roformer" in text:
        return "roformer"
    if "scnet" in text:
        return "scnet"
    if "bandit" in text:
        return "bandit"
    if "mdxc" in text:
        return "mdxc"
    if "demucs" in text:
        return "demucs"
    if family == "vr" or re.search(r"\bvr\b|vr_arch|vr architecture", text):
        return "vr_arch"
    if "mdx" in text or "onnx" in text or (filename and filename.lower().endswith(".onnx")):
        return "mdx_onnx"
    return "unknown"


def _defaults_for_arch(arch: str, *, name: str) -> dict[str, Any]:
    if arch == "demucs":
        return {
            "model_name": name,
            "device": "auto",
            "output_format": "wav",
            "managed_by": "demucs",
        }
    if arch == "mdx_onnx":
        return {
            "primary_stem": "vocals",
            "secondary_stem": "instrumental",
            "sample_rate": 44100,
            "stereo": True,
        }
    return {}


def _tasks_for_arch(arch: str) -> list[str]:
    if arch == "demucs":
        return ["music_source_separation"]
    return ["vocal_separation"]


def _output_stems_for_arch(arch: str, *, name: str) -> list[str]:
    if arch == "demucs":
        lower_name = name.lower()
        if "6" in lower_name or "6s" in lower_name:
            return ["drums", "bass", "other", "vocals", "guitar", "piano"]
        return ["drums", "bass", "other", "vocals"]
    return ["vocals", "instrumental"]


def _tags_for_arch(arch: str) -> list[str]:
    if arch == "mdx_onnx":
        return ["uvr", "mdx", "onnx"]
    if arch == "demucs":
        return ["uvr", "demucs", "package-managed"]
    if arch in UNSUPPORTED_ARCHES:
        return ["uvr", arch, "unsupported"]
    return ["uvr", "unsupported"]


def _category_for_arch(arch: str) -> str:
    if arch == "mdx_onnx":
        return "UVR MDX"
    if arch == "demucs":
        return "UVR Demucs"
    if arch == "vr_arch":
        return "UVR VR Arch"
    if arch == "mdxc":
        return "UVR MDXC"
    return f"UVR {arch}"


def _sort_order_for_arch(arch: str) -> int:
    if arch == "mdx_onnx":
        return 1000
    if arch == "demucs":
        return 1050
    return 1900


def _display_name(name: str) -> str:
    return name.replace("_", " ").strip() or name


def _uvr_id(name: str) -> str:
    slug_source = Path(name).stem
    slug = re.sub(r"[^a-z0-9]+", "_", slug_source.lower())
    slug = re.sub(r"_+", "_", slug).strip("_")
    if not slug:
        slug = "model"
    if not slug.startswith("uvr_"):
        slug = f"uvr_{slug}"
    return slug


def _dedupe_entries(entries: list[dict]) -> list[dict]:
    counts: dict[str, int] = {}
    deduped: list[dict] = []
    for entry in entries:
        base_id = entry["id"]
        count = counts.get(base_id, 0) + 1
        counts[base_id] = count
        if count > 1:
            entry = dict(entry)
            entry["id"] = f"{base_id}_{count}"
        deduped.append(entry)
    return deduped


def _join_url(base_url: str | None, filename: str) -> str | None:
    if not base_url:
        return None
    base = base_url if base_url.endswith("/") else f"{base_url}/"
    return urllib.parse.urljoin(base, urllib.parse.quote(filename))


def _has_known_model_suffix(value: str) -> bool:
    return Path(value).suffix.lower() in {".ckpt", ".onnx", ".pth", ".th"}


def _normalize_lookup_key(value: str) -> str:
    return Path(value).name.lower()


__all__ = [
    "DEFAULT_UVR_SOURCES",
    "RUNNABLE_ARCHES",
    "UNSUPPORTED_ARCHES",
    "UVRModelImporter",
    "load_uvr_model_entries",
]
