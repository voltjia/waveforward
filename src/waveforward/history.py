"""Import helpers for existing local agent session histories."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from waveforward.core import utc_now
from waveforward.service import CONVERSATION_VERSION, DEFAULT_MACHINE

SUPPORTED_HISTORY_SOURCES = ("claude-code", "codex", "opencode")
DEFAULT_HISTORY_LIMIT = 30
MAX_HISTORY_FILES_PER_SOURCE = 300
MAX_IMPORTED_MESSAGES = 200
MAX_IMPORTED_MESSAGE_CHARS = 12000
MAX_FILE_BYTES = 8 * 1024 * 1024

SOURCE_LABELS = {
    "claude-code": "Claude Code",
    "codex": "Codex",
    "opencode": "OpenCode",
}


@dataclass(frozen=True)
class HistorySession:
    """One importable local agent session."""

    id: str
    source: str
    source_label: str
    title: str
    updated_at: str
    message_count: int
    preview: str
    path: Path
    messages: tuple[dict[str, Any], ...]

    def summary(self) -> dict[str, Any]:
        """Return the cloud-safe candidate shape."""

        return {
            "id": self.id,
            "source": self.source,
            "source_label": self.source_label,
            "title": self.title,
            "updated_at": self.updated_at,
            "message_count": self.message_count,
            "preview": self.preview,
        }


def discover_agent_sessions(
    *,
    home: Path | str | None = None,
    limit: int = DEFAULT_HISTORY_LIMIT,
    sources: list[str] | tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """Discover importable local agent sessions without exposing file paths."""

    sessions = _discover_sessions(
        home=_home_path(home),
        sources=_normalize_sources(sources),
    )
    return [item.summary() for item in sessions[: _positive_limit(limit)]]


def import_agent_sessions(
    workspace: Path | str,
    candidate_ids: list[str] | tuple[str, ...],
    *,
    home: Path | str | None = None,
    machine: str = DEFAULT_MACHINE,
    owner: str | None = None,
) -> list[dict[str, Any]]:
    """Convert selected local agent histories into WaveForward conversations."""

    wanted = {str(item).strip() for item in candidate_ids if str(item).strip()}
    if not wanted:
        return []

    sessions = _discover_sessions(
        home=_home_path(home),
        sources=SUPPORTED_HISTORY_SOURCES,
    )
    conversations = []
    for session in sessions:
        if session.id not in wanted:
            continue
        conversations.append(
            _session_to_conversation(
                session,
                workspace=Path(workspace),
                machine=machine,
                owner=owner,
            )
        )
    return conversations


def _discover_sessions(
    *,
    home: Path,
    sources: tuple[str, ...],
) -> list[HistorySession]:
    sessions: list[HistorySession] = []
    if "claude-code" in sources:
        sessions.extend(
            _discover_jsonl_source("claude-code", home / ".claude/projects")
        )
    if "codex" in sources:
        sessions.extend(_discover_jsonl_source("codex", home / ".codex/sessions"))
    if "opencode" in sources:
        sessions.extend(_discover_opencode_sessions(home))
    return sorted(sessions, key=lambda item: item.updated_at, reverse=True)


def _discover_jsonl_source(source: str, root: Path) -> list[HistorySession]:
    if not root.exists():
        return []
    sessions = []
    for path in _recent_files(root, "*.jsonl"):
        messages = _messages_from_jsonl(path)
        if not messages:
            continue
        sessions.append(_build_session(source, path, messages))
    return sessions


def _discover_opencode_sessions(home: Path) -> list[HistorySession]:
    sessions = []
    for storage in _opencode_storage_dirs(home):
        session_root = storage / "session"
        if not session_root.exists():
            continue
        for path in _recent_files(session_root, "*.json"):
            messages = _opencode_messages_for_session(storage, path)
            if not messages:
                messages = _messages_from_json(path)
            if not messages:
                continue
            sessions.append(_build_session("opencode", path, messages))
    return sessions


def _opencode_storage_dirs(home: Path) -> list[Path]:
    data_home = Path(os.getenv("XDG_DATA_HOME") or home / ".local/share")
    roots = [
        data_home / "opencode",
        home / ".local/share/opencode",
        home / ".opencode",
    ]
    storages: list[Path] = []
    for root in roots:
        storages.append(root / "storage")
        project_root = root / "project"
        if project_root.exists():
            storages.extend(project / "storage" for project in project_root.iterdir())
    seen = set()
    unique = []
    for path in storages:
        resolved = str(path)
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(path)
    return unique


def _opencode_messages_for_session(
    storage: Path,
    session_path: Path,
) -> list[dict[str, Any]]:
    session_data = _read_json(session_path)
    session_id = str(session_data.get("id") or session_path.stem)
    message_root = storage / "message" / session_id
    paths = []
    if message_root.exists():
        paths = _recent_files(message_root, "*.json", newest_first=False)
    else:
        root = storage / "message"
        if root.exists():
            paths = [
                path
                for path in _recent_files(root, "*.json", newest_first=False)
                if session_id in path.name
            ]
    messages = []
    for path in paths:
        messages.extend(_messages_from_json(path))
        if len(messages) >= MAX_IMPORTED_MESSAGES:
            break
    return _sort_messages(messages[:MAX_IMPORTED_MESSAGES])


def _messages_from_jsonl(path: Path) -> list[dict[str, Any]]:
    if _too_large(path):
        return []
    messages = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                message = _message_from_item(item)
                if message:
                    messages.append(message)
                if len(messages) >= MAX_IMPORTED_MESSAGES:
                    break
    except OSError:
        return []
    return _sort_messages(messages)


def _messages_from_json(path: Path) -> list[dict[str, Any]]:
    data = _read_json(path)
    if not data:
        return []
    if isinstance(data.get("messages"), list):
        messages = [
            message
            for item in data["messages"]
            if isinstance(item, dict)
            for message in [_message_from_item(item)]
            if message
        ]
        return _sort_messages(messages[:MAX_IMPORTED_MESSAGES])
    message = _message_from_item(data)
    return [message] if message else []


def _message_from_item(item: dict[str, Any]) -> dict[str, Any] | None:
    role = _extract_role(item)
    if role not in {"user", "assistant"}:
        return None
    content = _extract_content(item)
    if not content:
        return None
    created_at = _extract_time(item) or utc_now()
    return {
        "role": role,
        "content": content[:MAX_IMPORTED_MESSAGE_CHARS],
        "created_at": created_at,
    }


def _extract_role(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    for key in ("role", "author", "speaker"):
        value = str(item.get(key) or "").lower()
        if value in {"user", "human"}:
            return "user"
        if value in {"assistant", "agent", "ai"}:
            return "assistant"
    item_type = str(item.get("type") or item.get("kind") or "").lower()
    if item_type in {"user", "human", "user_message", "input"}:
        return "user"
    if item_type in {"assistant", "agent", "assistant_message", "response"}:
        return "assistant"
    for key in ("message", "payload", "item", "event"):
        nested = item.get(key)
        if isinstance(nested, dict):
            role = _extract_role(nested)
            if role:
                return role
    return ""


def _extract_content(item: Any) -> str:
    parts = _content_parts(item)
    text = "\n".join(part for part in parts if part).strip()
    return _compact_text(text)


def _content_parts(item: Any) -> list[str]:
    if isinstance(item, str):
        return [item]
    if isinstance(item, list):
        parts: list[str] = []
        for value in item:
            parts.extend(_content_parts(value))
        return parts
    if not isinstance(item, dict):
        return []

    block_type = str(item.get("type") or "").lower()
    if block_type in {"text", "input_text", "output_text"} and isinstance(
        item.get("text"), str
    ):
        return [item["text"]]

    parts = []
    for key in (
        "content",
        "text",
        "parts",
        "message",
        "payload",
        "item",
        "response",
        "output",
    ):
        value = item.get(key)
        if value is None:
            continue
        if key == "message" and isinstance(value, str):
            parts.append(value)
            continue
        if key in {"message", "payload", "item", "response", "output"} and isinstance(
            value, dict
        ):
            parts.extend(_content_parts(value))
            continue
        if key in {"content", "text", "parts"}:
            parts.extend(_content_parts(value))
    return parts


def _extract_time(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    for key in ("created_at", "updated_at", "timestamp", "time"):
        normalized = _normalize_time(item.get(key))
        if normalized:
            return normalized
    for key in ("message", "payload", "item"):
        value = item.get(key)
        if isinstance(value, dict):
            normalized = _extract_time(value)
            if normalized:
                return normalized
    return ""


def _normalize_time(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, int | float):
        seconds = float(value)
        if seconds > 10_000_000_000:
            seconds = seconds / 1000
        return datetime.fromtimestamp(seconds, UTC).isoformat().replace("+00:00", "Z")
    text = str(value).strip()
    if not text:
        return ""
    if text.endswith("Z"):
        return text
    with_z = text.replace("+00:00", "Z")
    return with_z if "T" in with_z else ""


def _build_session(
    source: str,
    path: Path,
    messages: list[dict[str, Any]],
) -> HistorySession:
    safe_messages = tuple(messages[:MAX_IMPORTED_MESSAGES])
    updated_at = _latest_message_time(safe_messages) or _mtime(path)
    first_user = next(
        (
            str(message["content"]).strip()
            for message in safe_messages
            if message.get("role") == "user"
        ),
        "",
    )
    preview = _compact_text(first_user or str(safe_messages[0].get("content") or ""))
    title = _title_from_preview(preview, fallback=path.stem)
    source_label = SOURCE_LABELS[source]
    return HistorySession(
        id=_candidate_id(source, path),
        source=source,
        source_label=source_label,
        title=title,
        updated_at=updated_at,
        message_count=len(safe_messages),
        preview=preview[:180],
        path=path,
        messages=safe_messages,
    )


def _session_to_conversation(
    session: HistorySession,
    *,
    workspace: Path,
    machine: str,
    owner: str | None,
) -> dict[str, Any]:
    now = utc_now()
    messages = []
    for index, message in enumerate(session.messages):
        message_id = hashlib.sha256(f"{session.id}:{index}".encode()).hexdigest()[:12]
        messages.append(
            {
                "id": f"msg_import_{message_id}",
                "role": message["role"],
                "content": message["content"],
                "created_at": message.get("created_at") or now,
                "agent": session.source,
                "machine": machine,
            }
        )
    conversation = {
        "version": CONVERSATION_VERSION,
        "id": f"conv_import_{session.id}",
        "title": session.title,
        "created_at": messages[0]["created_at"] if messages else now,
        "updated_at": session.updated_at or now,
        "workspace": str(workspace.resolve()),
        "preferred": {
            "agent": session.source,
            "machine": machine or DEFAULT_MACHINE,
        },
        "messages": messages,
        "continuations": [],
        "imported_from": {
            "id": session.id,
            "source": session.source,
            "source_label": session.source_label,
            "imported_at": now,
        },
    }
    if owner:
        conversation["owner"] = owner
    return conversation


def _candidate_id(source: str, path: Path) -> str:
    digest = hashlib.sha256(f"{source}:{path.expanduser()}".encode()).hexdigest()
    return f"{source}-{digest[:16]}"


def _recent_files(root: Path, pattern: str, *, newest_first: bool = True) -> list[Path]:
    try:
        paths = [path for path in root.rglob(pattern) if path.is_file()]
    except OSError:
        return []
    return sorted(
        paths,
        key=lambda item: item.stat().st_mtime if item.exists() else 0,
        reverse=newest_first,
    )[:MAX_HISTORY_FILES_PER_SOURCE]


def _read_json(path: Path) -> dict[str, Any]:
    if _too_large(path):
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _too_large(path: Path) -> bool:
    try:
        return path.stat().st_size > MAX_FILE_BYTES
    except OSError:
        return True


def _latest_message_time(messages: tuple[dict[str, Any], ...]) -> str:
    values = [str(message.get("created_at") or "") for message in messages]
    return max(values) if values else ""


def _mtime(path: Path) -> str:
    try:
        return (
            datetime.fromtimestamp(path.stat().st_mtime, UTC)
            .isoformat()
            .replace(
                "+00:00",
                "Z",
            )
        )
    except OSError:
        return utc_now()


def _sort_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(messages, key=lambda item: str(item.get("created_at") or ""))


def _title_from_preview(preview: str, *, fallback: str) -> str:
    text = _compact_text(preview or fallback)
    if not text:
        return "Imported session"
    return text[:70]


def _compact_text(text: str) -> str:
    return " ".join(str(text or "").split())


def _home_path(home: Path | str | None) -> Path:
    return Path(home).expanduser() if home is not None else Path.home()


def _normalize_sources(sources: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    if not sources:
        return SUPPORTED_HISTORY_SOURCES
    normalized = tuple(
        source for source in sources if source in SUPPORTED_HISTORY_SOURCES
    )
    return normalized or SUPPORTED_HISTORY_SOURCES


def _positive_limit(value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return DEFAULT_HISTORY_LIMIT
    return min(max(parsed, 1), 100)
