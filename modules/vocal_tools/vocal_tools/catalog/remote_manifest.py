from __future__ import annotations

import json
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from vocal_tools.core.errors import InvalidManifestError


def load_manifest_document(
    url: str,
    cache_path: Optional[str | Path] = None,
    *,
    use_cache: bool = True,
) -> Tuple[Any, Dict[str, Any]]:
    """Read a remote manifest from a URL, file URL, local path, or cache."""
    if not url:
        raise InvalidManifestError("Remote manifest URL must not be empty.")

    cache = Path(cache_path) if cache_path is not None else None
    if use_cache and cache is not None and cache.exists():
        text = _read_text(cache)
        return _loads(text, str(cache)), {
            "url": url,
            "cache_path": str(cache),
            "path": str(cache),
            "source_name": _source_name(url),
            "loaded_from": "cache",
        }

    text, loaded_from, source_path = _read_source(url)
    if cache is not None:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(text, encoding="utf-8")

    return _loads(text, url), {
        "url": url,
        "cache_path": str(cache) if cache is not None else None,
        "path": str(cache) if cache is not None else source_path,
        "source_name": _source_name(url),
        "loaded_from": loaded_from,
    }


def _read_source(url: str) -> Tuple[str, str, Optional[str]]:
    parsed = urllib.parse.urlparse(url)
    scheme = parsed.scheme.lower()

    if scheme in ("http", "https"):
        try:
            with urllib.request.urlopen(url) as response:
                return response.read().decode("utf-8"), "url", url
        except OSError as exc:
            raise InvalidManifestError(f"Could not read remote manifest URL {url}: {exc}") from exc

    if scheme == "file":
        path = Path(urllib.request.url2pathname(parsed.path))
        return _read_text(path), "file_url", str(path)

    path = Path(url)
    if path.exists():
        return _read_text(path), "local_file", str(path)

    raise InvalidManifestError(
        f"Remote manifest source is not a readable URL or local JSON file: {url}"
    )


def _source_name(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme in ("http", "https"):
        name = Path(parsed.path).stem
        return name or parsed.netloc or "remote"
    if parsed.scheme == "file":
        return Path(urllib.request.url2pathname(parsed.path)).stem or "remote"
    return Path(url).stem or "remote"


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise InvalidManifestError(f"Could not read manifest file {path}: {exc}") from exc


def _loads(text: str, source: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise InvalidManifestError(
            f"Invalid JSON in manifest {source}: {exc.msg} at line {exc.lineno}, column {exc.colno}."
        ) from exc
