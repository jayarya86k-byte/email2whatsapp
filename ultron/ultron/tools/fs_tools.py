"""Filesystem tools, all confined to the configured sandbox root."""

from __future__ import annotations

import fnmatch
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from .registry import ToolRegistry

MAX_READ_BYTES = 400_000
TEXT_SUFFIXES = {
    ".txt", ".md", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".csv", ".tsv", ".html", ".css", ".sql", ".sh",
    ".xml", ".go", ".rs", ".java", ".c", ".h", ".cpp", ".log", ".env.example",
}


def _resolve(policy: Any, raw: str) -> Path:
    return Path(os.path.expandvars(os.path.expanduser(raw)))


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...<truncated {len(text) - limit} chars>"


def register(registry: ToolRegistry, policy: Any, audit: Any) -> None:
    limit = policy.max_output

    def read_file(path: str, start_line: int = 1, max_lines: int = 2000) -> Dict[str, Any]:
        """Read a text file and return its contents with line numbers."""
        target = _resolve(policy, path)
        if not target.exists():
            return {"ok": False, "error": f"file not found: {target}"}
        if target.is_dir():
            return {"ok": False, "error": f"{target} is a directory; use list_dir"}
        data = target.read_bytes()[:MAX_READ_BYTES]
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            return {"ok": False, "error": "binary file", "size_bytes": target.stat().st_size}
        lines = text.splitlines()
        window = lines[max(0, start_line - 1): start_line - 1 + max_lines]
        body = "\n".join(f"{index:>5}| {line}"
                         for index, line in enumerate(window, start=max(1, start_line)))
        audit.record("fs.read", detail={"path": policy.relativize(str(target))})
        return {"ok": True, "path": str(target), "lines": len(lines),
                "content": _truncate(body, limit)}

    def write_file(path: str, content: str, append: bool = False) -> Dict[str, Any]:
        """Create or overwrite a text file inside the workspace."""
        target = _resolve(policy, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        with target.open(mode, encoding="utf-8") as handle:
            handle.write(content)
        audit.record("fs.write", detail={"path": policy.relativize(str(target)),
                                        "bytes": len(content), "append": append})
        return {"ok": True, "path": str(target), "bytes_written": len(content)}

    def list_dir(path: str = ".", pattern: str = "*", recursive: bool = False) -> Dict[str, Any]:
        """List files in a directory, optionally filtered by glob and recursive."""
        target = _resolve(policy, path)
        if not target.is_dir():
            return {"ok": False, "error": f"not a directory: {target}"}
        entries: List[Dict[str, Any]] = []
        iterator = target.rglob("*") if recursive else target.glob("*")
        for item in sorted(iterator):
            if not item.is_file() and not recursive:
                continue
            name = str(item.relative_to(target))
            if not fnmatch.fnmatch(name, pattern) and not fnmatch.fnmatch(item.name, pattern):
                continue
            try:
                size = item.stat().st_size
            except OSError:
                size = -1
            entries.append({"path": name, "size": size,
                            "dir": str(item.relative_to(target.parent)) if item.is_dir() else None})
            if len(entries) >= 2000:
                break
        return {"ok": True, "path": str(target), "count": len(entries), "entries": entries}

    def move_file(source: str, destination: str) -> Dict[str, Any]:
        """Move or rename a file."""
        src, dst = _resolve(policy, source), _resolve(policy, destination)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        audit.record("fs.move", risk=0.3, capability="fs.write",
                     detail={"from": str(src), "to": str(dst)})
        return {"ok": True, "from": str(src), "to": str(dst)}

    def delete_file(path: str, recursive: bool = False) -> Dict[str, Any]:
        """Delete a file or directory. Destructive and always confirmed."""
        target = _resolve(policy, path)
        if not target.exists():
            return {"ok": False, "error": f"not found: {target}"}
        if target.is_dir():
            if not recursive:
                return {"ok": False, "error": "directory; pass recursive=true"}
            shutil.rmtree(target)
        else:
            target.unlink()
        audit.record("fs.delete", risk=0.75, capability="fs.delete",
                     detail={"path": str(target), "recursive": recursive})
        return {"ok": True, "deleted": str(target)}

    def search_files(pattern: str, path: str = ".", content: bool = False) -> Dict[str, Any]:
        """Find files by glob, or search file contents with a regex."""
        root = _resolve(policy, path)
        hits: List[Dict[str, Any]] = []
        if content:
            import re as _re
            try:
                regex = _re.compile(pattern)
            except _re.error as exc:
                return {"ok": False, "error": f"bad regex: {exc}"}
            for item in root.rglob("*"):
                if len(hits) >= 300:
                    break
                if not item.is_file() or item.suffix.lower() not in TEXT_SUFFIXES:
                    continue
                if item.stat().st_size > MAX_READ_BYTES:
                    continue
                try:
                    for number, line in enumerate(item.read_text("utf-8", "ignore").splitlines(), 1):
                        if regex.search(line):
                            hits.append({"path": str(item), "line": number, "text": line[:400]})
                            break
                except OSError:
                    continue
        else:
            for item in root.rglob(pattern):
                hits.append({"path": str(item), "dir": item.is_dir()})
                if len(hits) >= 500:
                    break
        return {"ok": True, "count": len(hits), "results": hits}

    def file_info(path: str) -> Dict[str, Any]:
        """Return size, type and modification time for a file."""
        target = _resolve(policy, path)
        if not target.exists():
            return {"ok": False, "error": f"not found: {target}"}
        stat = target.stat()
        return {"ok": True, "path": str(target), "size": stat.st_size,
                "modified": stat.st_mtime, "suffix": target.suffix,
                "sha_hint": oct(stat.st_mode & 0o777)}

    registry.function_tool(read_file, risk=0.05, capability="fs.read",
                           category="files", confirm_args=["path"])
    registry.function_tool(write_file, risk=0.35, capability="fs.write",
                           category="files", confirm_args=["path"])
    registry.function_tool(list_dir, risk=0.05, capability="fs.read", category="files")
    registry.function_tool(move_file, risk=0.4, capability="fs.write",
                           category="files", confirm_args=["source", "destination"])
    registry.function_tool(delete_file, risk=0.75, capability="fs.delete",
                           category="files", confirm_args=["path"])
    registry.function_tool(search_files, risk=0.1, capability="fs.read", category="files")
    registry.function_tool(file_info, risk=0.05, capability="fs.read", category="files")
