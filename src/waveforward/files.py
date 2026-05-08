"""Safe workspace file access for the WaveForward GUI."""

from __future__ import annotations

import hashlib
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from waveforward.core import AgentSyncError
from waveforward.store import workspace_write_lock

MAX_TEXT_FILE_BYTES = 1024 * 1024
MAX_TREE_ENTRIES = 800
BLOCKED_DIR_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    ".waveforward",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
}
BLOCKED_FILE_NAMES = {
    ".env",
    ".env.local",
    ".env.production",
    ".npmrc",
    ".pypirc",
}


def list_workspace_tree(
    start: Path | str,
    *,
    path: str = "",
    limit: int = MAX_TREE_ENTRIES,
) -> dict[str, Any]:
    """Return one directory level from a workspace root."""

    root = Path(start).resolve()
    directory, relative = _resolve_workspace_path(root, path)
    if not directory.exists():
        raise AgentSyncError("Workspace path was not found.")
    if not directory.is_dir():
        raise AgentSyncError("Workspace path is not a directory.")

    entries = []
    for child in sorted(directory.iterdir(), key=_tree_sort_key):
        if len(entries) >= max(limit, 1):
            break
        if _is_blocked_name(child):
            continue
        try:
            resolved = child.resolve()
        except OSError:
            continue
        if not _is_within(root, resolved):
            continue
        entries.append(_tree_entry(root, child))

    return {
        "path": relative,
        "entries": entries,
        "truncated": len(entries) >= max(limit, 1),
    }


def read_workspace_file(
    start: Path | str,
    *,
    path: str,
    max_bytes: int = MAX_TEXT_FILE_BYTES,
) -> dict[str, Any]:
    """Read one UTF-8 text file from a workspace root."""

    root = Path(start).resolve()
    file_path, relative = _resolve_workspace_path(root, path)
    _ensure_readable_text_file(file_path, max_bytes=max_bytes)
    raw = file_path.read_bytes()
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise AgentSyncError("Only UTF-8 text files can be opened.") from error
    return {
        "path": relative,
        "name": file_path.name,
        "bytes": len(raw),
        "content": content,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "modified_at": _mtime(file_path),
    }


def write_workspace_file(
    start: Path | str,
    *,
    path: str,
    content: str,
    base_sha256: str | None = None,
    create: bool = False,
    max_bytes: int = MAX_TEXT_FILE_BYTES,
) -> dict[str, Any]:
    """Write one UTF-8 text file with optimistic conflict detection."""

    root = Path(start).resolve()
    file_path, relative = _resolve_workspace_path(root, path, allow_missing=create)
    encoded = content.encode("utf-8")
    if len(encoded) > max_bytes:
        raise AgentSyncError(
            f"File is too large to save in the code workspace "
            f"({len(encoded)} bytes > {max_bytes} bytes)."
        )
    if b"\x00" in encoded:
        raise AgentSyncError("Binary content cannot be saved in the code workspace.")

    with workspace_write_lock(root):
        if file_path.exists():
            if not file_path.is_file():
                raise AgentSyncError("Workspace path is not a file.")
            _ensure_readable_text_file(file_path, max_bytes=max_bytes)
            current = file_path.read_bytes()
            if not base_sha256:
                raise AgentSyncError(
                    "base_sha256 is required to save an existing file."
                )
            if hashlib.sha256(current).hexdigest() != base_sha256:
                raise AgentSyncError("File changed on disk. Reload before saving.")
            mode = file_path.stat().st_mode
        else:
            if not create:
                raise AgentSyncError("Workspace file was not found.")
            if not file_path.parent.exists():
                raise AgentSyncError("Parent directory was not found.")
            mode = 0o644

        temp_path = file_path.with_name(f".{file_path.name}.waveforward-tmp")
        try:
            if temp_path.exists() or temp_path.is_symlink():
                temp_path.unlink()
            temp_path.write_bytes(encoded)
            os.chmod(temp_path, mode & 0o777)
            os.replace(temp_path, file_path)
        finally:
            if temp_path.exists():
                temp_path.unlink()
    return read_workspace_file(root, path=relative, max_bytes=max_bytes)


def workspace_file_diff(start: Path | str, *, path: str) -> dict[str, Any]:
    """Return Git diff for one workspace file when Git can provide it."""

    root = Path(start).resolve()
    file_path, relative = _resolve_workspace_path(root, path)
    if not file_path.exists() or not file_path.is_file():
        raise AgentSyncError("Workspace file was not found.")
    result = subprocess.run(
        ["git", "diff", "--", Path(relative).as_posix()],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1):
        return {"path": relative, "diff": "", "available": False}
    return {"path": relative, "diff": result.stdout, "available": True}


def _resolve_workspace_path(
    root: Path,
    path: str,
    *,
    allow_missing: bool = False,
) -> tuple[Path, str]:
    relative = _clean_relative_path(path)
    candidate = (root / relative).resolve(strict=not allow_missing)
    if not _is_within(root, candidate):
        raise AgentSyncError("Workspace path escapes the project root.")
    if any(part in BLOCKED_DIR_NAMES for part in Path(relative).parts):
        raise AgentSyncError("Path is not available in the code workspace.")
    if candidate.name in BLOCKED_FILE_NAMES:
        raise AgentSyncError("File is not available in the code workspace.")
    return candidate, relative


def _clean_relative_path(path: str) -> str:
    raw = str(path or "").strip().replace("\\", "/")
    if not raw or raw == ".":
        return ""
    candidate = Path(raw)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise AgentSyncError("Workspace path must be relative.")
    return candidate.as_posix()


def _ensure_readable_text_file(path: Path, *, max_bytes: int) -> None:
    if not path.exists() or not path.is_file():
        raise AgentSyncError("Workspace file was not found.")
    size = path.stat().st_size
    if size > max_bytes:
        raise AgentSyncError(
            f"File is too large to open in the code workspace "
            f"({size} bytes > {max_bytes} bytes)."
        )
    if b"\x00" in path.read_bytes()[:8192]:
        raise AgentSyncError("Binary files cannot be opened in the code workspace.")


def _tree_entry(root: Path, path: Path) -> dict[str, Any]:
    stat = path.stat()
    kind = "directory" if path.is_dir() else "file"
    entry = {
        "name": path.name,
        "path": path.relative_to(root).as_posix(),
        "type": kind,
        "modified_at": _mtime(path),
    }
    if kind == "file":
        entry["bytes"] = stat.st_size
    return entry


def _tree_sort_key(path: Path) -> tuple[int, str]:
    return (0 if path.is_dir() else 1, path.name.lower())


def _is_blocked_name(path: Path) -> bool:
    if path.is_dir() and path.name in BLOCKED_DIR_NAMES:
        return True
    return path.is_file() and path.name in BLOCKED_FILE_NAMES


def _is_within(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _mtime(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat()
