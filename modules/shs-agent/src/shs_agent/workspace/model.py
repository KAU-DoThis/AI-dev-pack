from __future__ import annotations

import posixpath
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


@dataclass(frozen=True)
class Mount:
    path: str
    root: Path | str
    read_only: bool = False
    delegable: bool = False

    def __post_init__(self):
        if not isinstance(self.path, str) or not self.path.startswith("/") or self.path.startswith("//") or "\\" in self.path or ":" in self.path or "\0" in self.path:
            raise ValueError("mount path must be an absolute logical POSIX path")
        object.__setattr__(self, "path", posixpath.normpath(self.path))
        object.__setattr__(self, "root", Path(self.root).resolve(strict=True))
        if not self.root.is_dir():
            raise ValueError("mount root must be an existing directory")
        if type(self.read_only) is not bool or type(self.delegable) is not bool:
            raise TypeError("mount permissions must be booleans")


@dataclass(frozen=True, init=False)
class Workspace:
    mounts: tuple[Mount, ...]
    cwd: str
    workspace_id: str
    revision: int

    def __init__(self, root=None, *, mounts=(), cwd=None, read_only=False, delegable=False,
                 workspace_id=None, revision=0):
        values = ((Mount("/workspace", root, read_only, delegable),) if root is not None else ()) + tuple(mounts)
        if not values or not all(isinstance(m, Mount) for m in values):
            raise ValueError("workspace needs at least one Mount")
        if len({m.path for m in values}) != len(values):
            raise ValueError("duplicate logical mount")
        if type(revision) is not int or revision < 0:
            raise ValueError("workspace revision must be a nonnegative integer")
        if cwd is not None and (not isinstance(cwd, str) or not cwd.startswith("/") or cwd.startswith("//")):
            raise ValueError("cwd must be an absolute logical path")
        object.__setattr__(self, "mounts", values)
        object.__setattr__(self, "cwd", cwd or values[0].path)
        object.__setattr__(self, "workspace_id", workspace_id or uuid4().hex)
        object.__setattr__(self, "revision", revision)
        if not self.resolve(".").is_dir():
            raise ValueError("workspace cwd must be an existing directory")

    def logical(self, path):
        if not isinstance(path, str) or not path or "\0" in path or ":" in path or path.startswith(("\\\\", "//")):
            raise ValueError("use a workspace logical path")
        path = path.replace("\\", "/")
        return posixpath.normpath(path if path.startswith("/") else posixpath.join(self.cwd, path))

    def mount_for(self, path):
        logical = self.logical(path)
        matches = [m for m in self.mounts if logical == m.path or logical.startswith(m.path.rstrip("/") + "/")]
        if not matches:
            raise PermissionError("path is outside workspace mounts")
        return max(matches, key=lambda m: len(m.path))

    def resolve(self, path, *, write=False):
        logical = self.logical(path)
        mount = self.mount_for(logical)
        if write and mount.read_only:
            raise PermissionError("mount is read-only")
        relative = logical[len(mount.path):].lstrip("/")
        physical = (mount.root / relative).resolve()
        if not physical.is_relative_to(mount.root):
            raise PermissionError("resolved path escapes mount")
        return physical

    def snapshot(self):
        return {"workspace_id": self.workspace_id, "revision": self.revision, "cwd": self.cwd,
                "mounts": [{"path": m.path, "root": str(m.root), "read_only": m.read_only,
                            "delegable": m.delegable} for m in self.mounts]}

    def delegate(self, paths=None, *, redelegate=False):
        if type(redelegate) is not bool:
            raise TypeError("redelegate must be a boolean")
        paths = [m.path for m in self.mounts] if paths is None else paths
        if not isinstance(paths, (list, tuple)) or not paths:
            raise ValueError("workspace_paths must be a nonempty array")
        mounts = []
        for path in paths:
            mount = self.mount_for(path)
            if not mount.delegable:
                raise PermissionError("mount was not delegated for child assignment")
            logical, physical = self.logical(path), self.resolve(path)
            # A broader parent mount must never erase a restricted nested overlay.
            # Require explicit leaf scopes instead of manufacturing hidden mounts.
            if any(m.path.startswith(logical.rstrip("/") + "/") for m in self.mounts):
                raise PermissionError("delegate leaf scopes explicitly when nested mounts are present")
            mounts.append(Mount(logical, physical, mount.read_only, redelegate))
        return Workspace(mounts=mounts, cwd=self.cwd if any(
            self.cwd == m.path or self.cwd.startswith(m.path + "/") for m in mounts) else mounts[0].path)

    def revised(self, revision):
        return Workspace(mounts=self.mounts, cwd=self.cwd,
                         workspace_id=self.workspace_id, revision=revision)
