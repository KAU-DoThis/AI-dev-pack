"""Small file operations sharing one resolver. Command execution stays with Host."""
from shs_agent.tool import Tool, ToolRegistry, ToolSpec
from shs_agent.agent import prompts


def registry(workspace):
    result = ToolRegistry()
    operations = FileTools(workspace)
    definitions = {
        "file_list": {"path": {"type": "string"}},
        "file_read": {"path": {"type": "string"}, "offset": {"type": "integer", "minimum": 0,
                      "description": prompts.FILE_READ_OFFSET_DESCRIPTION},
                      "limit": {"type": "integer", "minimum": 1, "description": prompts.FILE_READ_LIMIT_DESCRIPTION}},
        "file_search": {"path": {"type": "string"}, "query": {"type": "string"}},
        "file_write": {"path": {"type": "string"}, "content": {"type": "string"}},
        "file_edit": {"path": {"type": "string"}, "old": {"type": "string"}, "new": {"type": "string"}},
        "file_delete": {"path": {"type": "string"}},
    }
    for name, properties in definitions.items():
        required = [key for key in properties if key not in {"offset", "limit"}]
        result.register(Tool(ToolSpec(name, prompts.FILE_DESCRIPTIONS[name], {
            "type": "object", "properties": properties, "required": required,
            "additionalProperties": False}), getattr(operations, name)))
    return result


class FileTools:
    def __init__(self, workspace):
        self.workspace = workspace

    def file_list(self, path):
        folder = self.workspace.resolve(path)
        entries = []
        for item in sorted(folder.iterdir()):
            logical = self.workspace.logical(path).rstrip("/") + "/" + item.name
            try:
                actual = self.workspace.resolve(logical)
            except PermissionError:
                continue
            entries.append({"path": logical, "directory": actual.is_dir()})
        return {"entries": entries}

    def file_read(self, path, offset=0, limit=12000):
        if type(offset) is not int or offset < 0 or type(limit) is not int or limit < 1:
            raise ValueError("offset/limit must be nonnegative/positive integers")
        text = self.workspace.resolve(path).read_text(encoding="utf-8")
        return {"path": self.workspace.logical(path), "content": text[offset:offset+limit],
                "offset": offset, "total_chars": len(text), "has_more": offset+limit < len(text)}

    def file_search(self, path, query):
        if not isinstance(query, str) or not query:
            raise ValueError("query must be nonempty")
        base = self.workspace.resolve(path)
        files = base.rglob("*") if base.is_dir() else [base]
        matches = []
        for candidate in files:
            logical = self.workspace.logical(path).rstrip("/")
            if base.is_dir():
                logical += "/" + candidate.relative_to(base).as_posix()
            try:
                actual = self.workspace.resolve(logical)
                if not actual.is_file():
                    continue
                text = actual.read_text(encoding="utf-8")
            except (PermissionError, UnicodeError):
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if query in line:
                    matches.append({"path": logical, "line": number, "text": line})
        return {"matches": matches}

    def file_write(self, path, content):
        target = self.workspace.resolve(path, write=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {"path": self.workspace.logical(path), "written_chars": len(content)}

    def file_edit(self, path, old, new):
        target = self.workspace.resolve(path, write=True)
        text = target.read_text(encoding="utf-8")
        if not old or text.count(old) != 1:
            raise ValueError("old text must match exactly once")
        target.write_text(text.replace(old, new, 1), encoding="utf-8")
        return {"path": self.workspace.logical(path), "replacements": 1}

    def file_delete(self, path):
        target = self.workspace.resolve(path, write=True)
        logical = self.workspace.logical(path)
        mount = self.workspace.mount_for(logical)
        lexical = mount.root / logical[len(mount.path):].lstrip("/")
        if lexical.is_symlink() or (hasattr(lexical, "is_junction") and lexical.is_junction()):
            raise PermissionError("link deletion is a Host operation")
        if any(target == m.root for m in self.workspace.mounts):
            raise PermissionError("cannot delete a mount root")
        if target.is_dir():
            target.rmdir()  # Only empty directories; no implicit recursive deletion.
        else:
            target.unlink()
        return {"path": self.workspace.logical(path), "deleted": True}
