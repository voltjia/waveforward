"""Small persistence primitives for local WaveForward state."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

_LOCKS: dict[Path, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def path_lock(path: Path | str) -> threading.RLock:
    """Return one process-local lock for a canonical filesystem path."""

    resolved = Path(path).resolve()
    with _LOCKS_GUARD:
        lock = _LOCKS.get(resolved)
        if lock is None:
            lock = threading.RLock()
            _LOCKS[resolved] = lock
        return lock


def workspace_write_lock(root: Path | str) -> threading.RLock:
    """Return one process-local lock for workspace-mutating operations."""

    return path_lock(Path(root).resolve() / ".waveforward-workspace-write-lock")


def atomic_write_text(path: Path, text: str, *, mode: int | None = None) -> None:
    """Write text with a same-directory replace under the path lock."""

    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path_lock(path)
    with lock:
        tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
        try:
            tmp_path.write_text(text, encoding="utf-8")
            if mode is not None:
                tmp_path.chmod(mode)
            tmp_path.replace(path)
        finally:
            with suppress(FileNotFoundError):
                tmp_path.unlink()


def read_json[T](path: Path, *, default: T | None = None) -> Any | T:
    """Read JSON under the path lock."""

    lock = path_lock(path)
    with lock:
        if not path.exists():
            if default is not None:
                return default
            raise FileNotFoundError(path)
        return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, data: Any, *, mode: int | None = None) -> None:
    """Atomically write pretty JSON under the path lock."""

    atomic_write_text(
        path,
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        mode=mode,
    )


def mutate_json[T](
    path: Path,
    default_factory: Callable[[], T],
    mutator: Callable[[Any | T], Any],
    *,
    mode: int | None = None,
) -> Any:
    """Read, mutate, and atomically write JSON while holding one path lock."""

    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path_lock(path)
    with lock:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            data = default_factory()
        result = mutator(data)
        tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
        try:
            tmp_path.write_text(
                json.dumps(data, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            if mode is not None:
                tmp_path.chmod(mode)
            tmp_path.replace(path)
        finally:
            with suppress(FileNotFoundError):
                tmp_path.unlink()
        return result
