"""Core snapshot and handoff behavior for WaveForward."""

from __future__ import annotations

import hashlib
import io
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_MAX_FILE_BYTES = 256 * 1024
BUNDLE_VERSION = 1
SNAPSHOT_VERSION = 1
SYNC_DIR_NAME = ".waveforward"
NON_GIT_WORKSPACE_MESSAGE = (
    "This workspace is not managed by Git. Basic conversations still work, "
    "but snapshots, packages, and machine migration require Git. Run `git init` "
    "to enable those features."
)


class AgentSyncError(RuntimeError):
    """Raised when WaveForward cannot complete a requested operation."""


WaveForwardError = AgentSyncError


@dataclass(frozen=True)
class SnapshotResult:
    """A created snapshot."""

    snapshot_id: str
    path: Path
    metadata: dict[str, Any]


@dataclass(frozen=True)
class RestoreResult:
    """A restore attempt summary."""

    snapshot_id: str
    applied: bool
    patches: tuple[str, ...]
    copied_untracked: tuple[str, ...]
    untracked_collisions: tuple[str, ...]


@dataclass(frozen=True)
class SnapshotVerification:
    """A verified snapshot summary."""

    snapshot_id: str
    path: Path
    captured_untracked: int
    skipped_untracked: int


@dataclass(frozen=True)
class BundleResult:
    """A snapshot bundle written to disk."""

    snapshot_id: str
    path: Path
    bytes: int


@dataclass(frozen=True)
class ImportResult:
    """A snapshot bundle import summary."""

    snapshot_id: str
    path: Path
    replaced: bool


@dataclass(frozen=True)
class DoctorCheck:
    """A local environment diagnostic result."""

    name: str
    status: str
    detail: str


def repo_root(start: Path | str = ".") -> Path:
    """Return the containing Git repository root."""

    result = _run(["git", "rev-parse", "--show-toplevel"], Path(start), check=False)
    if result.returncode != 0:
        raise AgentSyncError(NON_GIT_WORKSPACE_MESSAGE)
    return Path(result.stdout.strip()).resolve()


def workspace_root(start: Path | str = ".") -> Path:
    """Return a Git root when available, otherwise the given directory."""

    try:
        return repo_root(start)
    except AgentSyncError:
        return Path(start).resolve()


def is_git_workspace(start: Path | str = ".") -> bool:
    """Return whether the path is inside a Git worktree."""

    try:
        repo_root(start)
    except AgentSyncError:
        return False
    return True


def sync_dir(root: Path) -> Path:
    """Return the sync metadata directory for a repository root."""

    return root / SYNC_DIR_NAME


def initialize_workspace(
    start: Path | str = ".",
    *,
    machine_name: str | None = None,
    force: bool = False,
) -> Path:
    """Create the local WaveForward metadata directory."""

    root = workspace_root(start)
    base = sync_dir(root)
    config_path = base / "config.toml"

    base.mkdir(parents=True, exist_ok=True)
    (base / "sessions").mkdir(exist_ok=True)
    (base / "handoffs").mkdir(exist_ok=True)
    (base / "bundles").mkdir(exist_ok=True)
    (base / "conversations").mkdir(exist_ok=True)

    if config_path.exists() and not force:
        return config_path

    now = utc_now()
    name = machine_name or platform.node() or "unknown"
    content = "\n".join(
        [
            "version = 1",
            f"workspace_id = {_toml_string(uuid.uuid4().hex)}",
            f"created_at = {_toml_string(now)}",
            "",
            "[machine]",
            f"name = {_toml_string(name)}",
            f"workspace = {_toml_string(str(root))}",
            "",
        ]
    )
    config_path.write_text(content, encoding="utf-8")
    return config_path


def update_machine_name(start: Path | str = ".", machine_name: str = "") -> Path:
    """Update the local machine name while preserving workspace metadata."""

    name = machine_name.strip()
    if not name:
        raise AgentSyncError("Machine name is required.")

    root = workspace_root(start)
    config_path = initialize_workspace(root)
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError:
        config = {}

    workspace_id = config.get("workspace_id")
    if not isinstance(workspace_id, str) or not workspace_id.strip():
        workspace_id = uuid.uuid4().hex
    created_at = config.get("created_at")
    if not isinstance(created_at, str) or not created_at.strip():
        created_at = utc_now()

    content = "\n".join(
        [
            "version = 1",
            f"workspace_id = {_toml_string(workspace_id)}",
            f"created_at = {_toml_string(created_at)}",
            "",
            "[machine]",
            f"name = {_toml_string(name)}",
            f"workspace = {_toml_string(str(root))}",
            "",
        ]
    )
    config_path.write_text(content, encoding="utf-8")
    return config_path


def create_snapshot(
    start: Path | str = ".",
    *,
    message: str = "",
    task: str = "",
    include_untracked: bool = True,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
) -> SnapshotResult:
    """Capture a portable snapshot of the current Git workspace."""

    root = repo_root(start)
    initialize_workspace(root)

    snapshot_id = build_snapshot_id()
    snapshot_path = sync_dir(root) / "sessions" / snapshot_id
    snapshot_path.mkdir(parents=True)

    status_text = _git(root, "status", "--short", "--branch").stdout
    status_entries = _status_entries(root)
    branch = _git(root, "branch", "--show-current").stdout.strip() or None
    commit = _current_commit(root)

    (snapshot_path / "status.txt").write_text(status_text, encoding="utf-8")
    _write_command_output(snapshot_path / "workspace.patch", root, "diff", "--binary")
    _write_command_output(
        snapshot_path / "staged.patch",
        root,
        "diff",
        "--cached",
        "--binary",
    )

    manifest = {"files": [], "skipped": []}
    if include_untracked:
        manifest = _capture_untracked_files(
            root,
            snapshot_path / "untracked",
            max_file_bytes=max_file_bytes,
        )
    _write_json(snapshot_path / "untracked_manifest.json", manifest)

    metadata = {
        "version": SNAPSHOT_VERSION,
        "id": snapshot_id,
        "created_at": utc_now(),
        "message": message,
        "task": task,
        "source": {
            "machine": _config_machine_name(root),
            "workspace": str(root),
            "platform": platform.platform(),
            "python": platform.python_version(),
        },
        "git": {
            "branch": branch,
            "commit": commit,
            "dirty": bool(status_entries),
            "status_entries": status_entries,
        },
        "artifacts": {
            "status": "status.txt",
            "workspace_patch": "workspace.patch",
            "staged_patch": "staged.patch",
            "untracked_manifest": "untracked_manifest.json",
        },
        "capture": {
            "include_untracked": include_untracked,
            "max_file_bytes": max_file_bytes,
            "untracked_files": len(manifest["files"]),
            "skipped_untracked_files": len(manifest["skipped"]),
        },
    }
    _write_json(snapshot_path / "metadata.json", metadata)
    return SnapshotResult(
        snapshot_id=snapshot_id, path=snapshot_path, metadata=metadata
    )


def list_snapshots(start: Path | str = ".") -> list[dict[str, Any]]:
    """Return known snapshots, newest first."""

    root = workspace_root(start)
    sessions = sync_dir(root) / "sessions"
    if not sessions.exists():
        return []

    snapshots = []
    for metadata_path in sessions.glob("*/metadata.json"):
        try:
            snapshots.append(_read_json(metadata_path))
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(snapshots, key=lambda item: item.get("created_at", ""), reverse=True)


def generate_handoff(
    start: Path | str = ".",
    *,
    snapshot_ref: str = "latest",
    target: str = "generic",
) -> Path:
    """Create a Markdown handoff document for another agent."""

    root = workspace_root(start)
    snapshot_path = resolve_snapshot_path(root, snapshot_ref)
    metadata = _read_json(snapshot_path / "metadata.json")
    manifest = _read_json(snapshot_path / "untracked_manifest.json")
    status_text = (snapshot_path / "status.txt").read_text(encoding="utf-8")

    handoff_dir = sync_dir(root) / "handoffs"
    handoff_dir.mkdir(parents=True, exist_ok=True)
    target_slug = target.lower().replace(" ", "-")
    handoff_path = handoff_dir / f"{metadata['id']}-{target_slug}.md"
    handoff_path.write_text(
        _render_handoff(metadata, manifest, status_text, snapshot_path, root, target),
        encoding="utf-8",
    )
    return handoff_path


def verify_snapshot(
    start: Path | str = ".",
    *,
    snapshot_ref: str = "latest",
) -> SnapshotVerification:
    """Verify that a local snapshot has all required artifacts intact."""

    root = workspace_root(start)
    snapshot_path = resolve_snapshot_path(root, snapshot_ref)
    metadata = _validate_snapshot_directory(snapshot_path)
    capture = metadata.get("capture", {})
    return SnapshotVerification(
        snapshot_id=metadata["id"],
        path=snapshot_path,
        captured_untracked=int(capture.get("untracked_files", 0)),
        skipped_untracked=int(capture.get("skipped_untracked_files", 0)),
    )


def export_snapshot_bundle(
    start: Path | str = ".",
    *,
    snapshot_ref: str = "latest",
    output: Path | str | None = None,
    overwrite: bool = False,
) -> BundleResult:
    """Export a snapshot directory as a portable tarball."""

    root = repo_root(start)
    snapshot_path = resolve_snapshot_path(root, snapshot_ref)
    metadata = _validate_snapshot_directory(snapshot_path)
    snapshot_id = metadata["id"]

    if output is None:
        bundle_path = sync_dir(root) / "bundles" / f"{snapshot_id}.wfbundle.tar.gz"
    else:
        bundle_path = Path(output)
        if not bundle_path.is_absolute():
            bundle_path = root / bundle_path

    if bundle_path.exists() and not overwrite:
        raise AgentSyncError(f"Bundle already exists: {bundle_path}")
    bundle_path.parent.mkdir(parents=True, exist_ok=True)

    bundle_manifest = {
        "version": BUNDLE_VERSION,
        "created_at": utc_now(),
        "snapshot_id": snapshot_id,
        "snapshot_dir": f"snapshots/{snapshot_id}",
    }
    with tarfile.open(bundle_path, "w:gz") as bundle:
        _add_json_to_tar(bundle, "waveforward-bundle.json", bundle_manifest)
        bundle.add(snapshot_path, arcname=f"snapshots/{snapshot_id}", recursive=True)

    return BundleResult(
        snapshot_id=snapshot_id,
        path=bundle_path,
        bytes=bundle_path.stat().st_size,
    )


def import_snapshot_bundle(
    start: Path | str = ".",
    *,
    bundle: Path | str,
    replace: bool = False,
) -> ImportResult:
    """Import a portable snapshot bundle into the local workspace."""

    root = repo_root(start)
    initialize_workspace(root)
    bundle_path = Path(bundle)
    if not bundle_path.is_absolute():
        bundle_path = root / bundle_path
    if not bundle_path.is_file():
        raise AgentSyncError(f"Bundle does not exist: {bundle_path}")

    with tempfile.TemporaryDirectory(prefix="waveforward-import-") as temp_dir:
        extracted = Path(temp_dir)
        with tarfile.open(bundle_path, "r:*") as archive:
            _safe_extract_tar(archive, extracted)

        bundle_manifest = _read_json(extracted / "waveforward-bundle.json")
        if bundle_manifest.get("version") != BUNDLE_VERSION:
            raise AgentSyncError("Unsupported bundle version.")

        snapshot_id = bundle_manifest.get("snapshot_id")
        if not isinstance(snapshot_id, str):
            raise AgentSyncError("Bundle is missing a snapshot id.")
        _safe_relative_path(snapshot_id)

        snapshot_path = extracted / "snapshots" / snapshot_id
        metadata = _validate_snapshot_directory(snapshot_path)
        if metadata["id"] != snapshot_id:
            raise AgentSyncError("Bundle metadata does not match snapshot id.")

        destination = sync_dir(root) / "sessions" / snapshot_id
        replaced = destination.exists()
        if replaced and not replace:
            raise AgentSyncError(f"Snapshot already exists: {snapshot_id}")
        if replaced:
            shutil.rmtree(destination)
        shutil.copytree(snapshot_path, destination)

    return ImportResult(snapshot_id=snapshot_id, path=destination, replaced=replaced)


def run_doctor(start: Path | str = ".") -> list[DoctorCheck]:
    """Inspect local prerequisites and workspace health."""

    checks: list[DoctorCheck] = []
    git_available = True
    try:
        root = repo_root(start)
    except AgentSyncError as error:
        root = workspace_root(start)
        git_available = False
        checks.append(DoctorCheck("git repository", "warn", str(error)))
    else:
        checks.append(DoctorCheck("git repository", "ok", str(root)))

    python_detail = (
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    )
    python_status = "ok" if sys.version_info >= (3, 12) else "warn"
    checks.append(DoctorCheck("Python", python_status, python_detail))

    config_path = sync_dir(root) / "config.toml"
    if config_path.exists():
        checks.append(DoctorCheck("WaveForward config", "ok", str(config_path)))
    else:
        checks.append(
            DoctorCheck("WaveForward config", "warn", "Run `waveforward init`.")
        )

    base = sync_dir(root)
    if base.exists():
        checks.append(_writable_check(base))
    else:
        checks.append(DoctorCheck("metadata directory", "warn", f"{base} is missing."))

    if git_available:
        ignored = _git(
            root, "check-ignore", "-q", f"{SYNC_DIR_NAME}/config.toml", check=False
        )
        if ignored.returncode == 0:
            checks.append(
                DoctorCheck("metadata ignore", "ok", f"{SYNC_DIR_NAME} ignored.")
            )
        else:
            checks.append(
                DoctorCheck(
                    "metadata ignore",
                    "warn",
                    f"Add `{SYNC_DIR_NAME}/` to `.gitignore`.",
                )
            )

        dirty = _git(root, "status", "--porcelain=v1").stdout.strip()
        if dirty:
            checks.append(DoctorCheck("worktree", "warn", "Uncommitted changes exist."))
        else:
            checks.append(DoctorCheck("worktree", "ok", "Clean."))
    else:
        checks.append(
            DoctorCheck("worktree", "warn", "Snapshot and migration features disabled.")
        )

    snapshots = list_snapshots(root)
    checks.append(DoctorCheck("snapshots", "ok", f"{len(snapshots)} available."))
    return checks


def restore_snapshot(
    start: Path | str = ".",
    *,
    snapshot_ref: str = "latest",
    apply: bool = False,
    force: bool = False,
) -> RestoreResult:
    """Restore a snapshot, or describe what would be restored."""

    root = repo_root(start)
    snapshot_path = resolve_snapshot_path(root, snapshot_ref)
    metadata = _read_json(snapshot_path / "metadata.json")
    manifest = _read_json(snapshot_path / "untracked_manifest.json")
    _validate_untracked_manifest(snapshot_path, manifest)
    untracked_files = tuple(item["path"] for item in manifest["files"])
    collisions = _untracked_collisions(root, untracked_files)

    if not apply:
        return RestoreResult(
            snapshot_id=metadata["id"],
            applied=False,
            patches=_patches_present(snapshot_path),
            copied_untracked=untracked_files,
            untracked_collisions=collisions,
        )

    if _git(root, "status", "--porcelain=v1").stdout.strip() and not force:
        raise AgentSyncError(
            "Worktree is not clean. Commit, stash, or re-run restore with --force."
        )
    if collisions and not force:
        joined = ", ".join(collisions)
        raise AgentSyncError(
            f"Untracked restore would overwrite existing files: {joined}"
        )

    applied_patches: list[str] = []
    staged_patch = snapshot_path / "staged.patch"
    workspace_patch = snapshot_path / "workspace.patch"

    if _has_content(staged_patch):
        _git(root, "apply", "--3way", "--index", str(staged_patch))
        applied_patches.append("staged.patch")
    if _has_content(workspace_patch):
        _git(root, "apply", "--3way", str(workspace_patch))
        applied_patches.append("workspace.patch")

    copied = _restore_untracked_files(root, snapshot_path, manifest, force=force)
    return RestoreResult(
        snapshot_id=metadata["id"],
        applied=True,
        patches=tuple(applied_patches),
        copied_untracked=tuple(copied),
        untracked_collisions=(),
    )


def resolve_snapshot_path(root: Path, snapshot_ref: str) -> Path:
    """Resolve a snapshot id, id prefix, or latest alias to a snapshot path."""

    sessions = sync_dir(root) / "sessions"
    if snapshot_ref == "latest":
        snapshots = list_snapshots(root)
        if not snapshots:
            raise AgentSyncError("No snapshots exist yet.")
        snapshot_ref = snapshots[0]["id"]

    exact = sessions / snapshot_ref
    if (exact / "metadata.json").exists():
        return exact

    matches = [
        path
        for path in sessions.glob(f"{snapshot_ref}*")
        if (path / "metadata.json").exists()
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise AgentSyncError(f"Snapshot prefix is ambiguous: {snapshot_ref}")
    raise AgentSyncError(f"Unknown snapshot: {snapshot_ref}")


def build_snapshot_id() -> str:
    """Build a sortable snapshot id."""

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{timestamp}-{uuid.uuid4().hex[:8]}"


def utc_now() -> str:
    """Return a compact UTC ISO timestamp."""

    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _render_handoff(
    metadata: dict[str, Any],
    manifest: dict[str, Any],
    status_text: str,
    snapshot_path: Path,
    root: Path,
    target: str,
) -> str:
    changed_files = _changed_file_lines(metadata["git"]["status_entries"])
    untracked_lines = _untracked_file_lines(manifest)
    workspace_patch = snapshot_path / "workspace.patch"
    staged_patch = snapshot_path / "staged.patch"
    snapshot_display = _display_path(root, snapshot_path)
    target_name = target or "generic"
    task = metadata.get("task") or "Not specified."
    message = metadata.get("message") or "Not specified."
    branch = metadata["git"].get("branch") or "(detached or unborn)"
    commit = metadata["git"].get("commit") or "(no commit yet)"

    return f"""# Agent Session Handoff

Target agent: {target_name}
Snapshot: {metadata["id"]}
Created: {metadata["created_at"]}

## Current Task
{task}

## Snapshot Message
{message}

## Repository State
- Source machine: {metadata["source"]["machine"]}
- Source workspace: {metadata["source"]["workspace"]}
- Branch: {branch}
- Commit: {commit}
- Dirty when captured: {"yes" if metadata["git"]["dirty"] else "no"}
- Snapshot artifacts: `{snapshot_display}`
- Staged patch bytes: {staged_patch.stat().st_size}
- Workspace patch bytes: {workspace_patch.stat().st_size}

## Changed Files
{changed_files}

## Captured Untracked Files
{untracked_lines}

## Git Status
```text
{status_text.rstrip()}
```

## Resume Prompt
You are resuming an agent coding session from snapshot `{metadata["id"]}`.
Read this handoff first, inspect the repository state, then continue from the
current task. If you need to reconstruct local changes, use the snapshot
artifacts above rather than guessing from memory.
"""


def _changed_file_lines(entries: list[dict[str, str]]) -> str:
    if not entries:
        return "- None."
    lines = []
    for entry in entries[:50]:
        source = f" <- {entry['source_path']}" if "source_path" in entry else ""
        lines.append(f"- `{entry['code']}` `{entry['path']}`{source}")
    if len(entries) > 50:
        lines.append(f"- ... {len(entries) - 50} more")
    return "\n".join(lines)


def _untracked_file_lines(manifest: dict[str, Any]) -> str:
    files = manifest.get("files", [])
    skipped = manifest.get("skipped", [])
    lines = [f"- `{item['path']}` ({item['bytes']} bytes)" for item in files[:50]]
    if len(files) > 50:
        lines.append(f"- ... {len(files) - 50} more captured")
    if skipped:
        lines.append(f"- Skipped: {len(skipped)} file(s)")
    return "\n".join(lines) if lines else "- None."


def _display_path(root: Path, path: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _status_entries(root: Path) -> list[dict[str, str]]:
    raw = _git(root, "status", "--porcelain=v1", "-z").stdout
    tokens = [token for token in raw.split("\0") if token]
    entries: list[dict[str, str]] = []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        code = token[:2]
        entry = {"code": code, "path": token[3:]}
        if code[:1] in {"R", "C"} and index + 1 < len(tokens):
            index += 1
            entry["source_path"] = tokens[index]
        entries.append(entry)
        index += 1
    return entries


def _capture_untracked_files(
    root: Path,
    destination: Path,
    *,
    max_file_bytes: int,
) -> dict[str, list[dict[str, Any]]]:
    manifest: dict[str, list[dict[str, Any]]] = {"files": [], "skipped": []}
    destination.mkdir(parents=True, exist_ok=True)

    for relative in _untracked_paths(root):
        source = root / relative
        manifest_path = relative.as_posix()
        if source.is_symlink():
            manifest["skipped"].append(
                {"path": manifest_path, "reason": "symlink-not-supported"}
            )
            continue
        if not source.is_file():
            manifest["skipped"].append(
                {"path": manifest_path, "reason": "not-a-regular-file"}
            )
            continue

        size = source.stat().st_size
        if size > max_file_bytes:
            manifest["skipped"].append(
                {
                    "path": manifest_path,
                    "reason": "file-too-large",
                    "bytes": size,
                }
            )
            continue

        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        manifest["files"].append(
            {
                "path": manifest_path,
                "bytes": size,
                "sha256": _sha256(source),
                "mode": stat.S_IMODE(source.stat().st_mode),
            }
        )
    return manifest


def _restore_untracked_files(
    root: Path,
    snapshot_path: Path,
    manifest: dict[str, Any],
    *,
    force: bool,
) -> list[str]:
    copied = []
    for item in manifest["files"]:
        relative = _safe_relative_path(item["path"])
        source = snapshot_path / "untracked" / relative
        target = _safe_child_path(root, relative)
        if target.exists() and not force:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        with suppress(OSError):
            os.chmod(target, item["mode"])
        copied.append(item["path"])
    return copied


def _untracked_paths(root: Path) -> list[Path]:
    raw = _git(root, "ls-files", "--others", "--exclude-standard", "-z").stdout
    paths = []
    for item in raw.split("\0"):
        if not item:
            continue
        relative = Path(item)
        if relative.parts and relative.parts[0] == SYNC_DIR_NAME:
            continue
        paths.append(relative)
    return paths


def _untracked_collisions(root: Path, paths: tuple[str, ...]) -> tuple[str, ...]:
    collisions = [
        path
        for path in paths
        if _safe_child_path(root, _safe_relative_path(path)).exists()
    ]
    return tuple(collisions)


def _validate_untracked_manifest(snapshot_path: Path, manifest: dict[str, Any]) -> None:
    base = snapshot_path / "untracked"
    for item in manifest["files"]:
        relative = _safe_relative_path(item["path"])
        source = _safe_child_path(base, relative)
        if not source.is_file():
            raise AgentSyncError(f"Captured untracked file is missing: {item['path']}")
        actual_bytes = source.stat().st_size
        if actual_bytes != item["bytes"]:
            raise AgentSyncError(
                f"Captured untracked file size changed: {item['path']}"
            )
        actual_sha = _sha256(source)
        if actual_sha != item["sha256"]:
            raise AgentSyncError(
                f"Captured untracked file checksum changed: {item['path']}"
            )


def _safe_relative_path(value: str) -> Path:
    path = Path(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise AgentSyncError(f"Unsafe snapshot path: {value}")
    return path


def _safe_child_path(root: Path, relative: Path) -> Path:
    candidate = (root / relative).resolve()
    root_resolved = root.resolve()
    if not candidate.is_relative_to(root_resolved):
        raise AgentSyncError(f"Snapshot path escapes target directory: {relative}")
    return candidate


def _validate_snapshot_directory(snapshot_path: Path) -> dict[str, Any]:
    required = (
        "metadata.json",
        "status.txt",
        "workspace.patch",
        "staged.patch",
        "untracked_manifest.json",
    )
    for name in required:
        if not (snapshot_path / name).is_file():
            raise AgentSyncError(f"Snapshot is missing required artifact: {name}")

    metadata = _read_json(snapshot_path / "metadata.json")
    if metadata.get("id") != snapshot_path.name:
        raise AgentSyncError("Snapshot directory name does not match metadata id.")
    manifest = _read_json(snapshot_path / "untracked_manifest.json")
    _validate_untracked_manifest(snapshot_path, manifest)
    return metadata


def _add_json_to_tar(
    archive: tarfile.TarFile,
    name: str,
    payload: dict[str, Any],
) -> None:
    data = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = int(datetime.now(UTC).timestamp())
    archive.addfile(info, io.BytesIO(data))


def _safe_extract_tar(archive: tarfile.TarFile, destination: Path) -> None:
    destination_resolved = destination.resolve()
    members = archive.getmembers()
    for member in members:
        target = (destination / member.name).resolve()
        if not target.is_relative_to(destination_resolved):
            raise AgentSyncError(f"Unsafe bundle member path: {member.name}")
        if member.issym() or member.islnk():
            raise AgentSyncError(f"Bundle links are not supported: {member.name}")
    archive.extractall(destination, members=members, filter="data")


def _writable_check(path: Path) -> DoctorCheck:
    try:
        with tempfile.NamedTemporaryFile(prefix=".doctor-", dir=path):
            pass
    except OSError as error:
        return DoctorCheck("metadata writable", "error", str(error))
    return DoctorCheck("metadata writable", "ok", str(path))


def _patches_present(snapshot_path: Path) -> tuple[str, ...]:
    return tuple(
        patch.name
        for patch in (snapshot_path / "staged.patch", snapshot_path / "workspace.patch")
        if _has_content(patch)
    )


def _has_content(path: Path) -> bool:
    return path.exists() and path.stat().st_size > 0


def _current_commit(root: Path) -> str | None:
    result = _git(root, "rev-parse", "--verify", "HEAD", check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _config_machine_name(root: Path) -> str:
    config_path = sync_dir(root) / "config.toml"
    try:
        with config_path.open("rb") as file:
            config = tomllib.load(file)
    except OSError:
        return platform.node() or "unknown"
    return config.get("machine", {}).get("name") or platform.node() or "unknown"


def _write_command_output(path: Path, root: Path, *args: str) -> None:
    result = _git(root, *args)
    path.write_text(result.stdout, encoding="utf-8")


def _write_json(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _toml_string(value: str) -> str:
    return json.dumps(value)


def _git(
    root: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return _run(["git", *args], root, check=check)


def _run(
    args: list[str],
    cwd: Path,
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=False)
    if check and result.returncode != 0:
        command = " ".join(args)
        detail = result.stderr.strip() or result.stdout.strip()
        raise AgentSyncError(f"Command failed: {command}\n{detail}")
    return result
