"""Service-level conversation model for WaveForward."""

from __future__ import annotations

import inspect
import json
import platform
import shutil
import tomllib
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from waveforward.core import (
    DEFAULT_MAX_FILE_BYTES,
    AgentSyncError,
    BundleResult,
    ImportResult,
    create_snapshot,
    export_snapshot_bundle,
    generate_handoff,
    import_snapshot_bundle,
    initialize_workspace,
    list_snapshots,
    restore_snapshot,
    run_doctor,
    sync_dir,
    utc_now,
)
from waveforward.runner import AgentRunResult, agent_capabilities, run_agent

CONVERSATION_VERSION = 1
DEFAULT_AGENT = "codex"
DEFAULT_MACHINE = "local"
OutputCallback = Callable[[str], None]


@dataclass(frozen=True)
class ContinuationResult:
    """A prepared continuation for a WaveForward conversation."""

    conversation: dict[str, Any]
    continuation: dict[str, Any]


@dataclass(frozen=True)
class ConversationTurnResult:
    """A completed service-driven conversation turn."""

    conversation: dict[str, Any]
    continuation: dict[str, Any] | None
    agent_run: AgentRunResult | None


@dataclass(frozen=True)
class ContinuationBundleResult:
    """An exported continuation bundle."""

    conversation_id: str
    continuation_id: str
    bundle: BundleResult


@dataclass(frozen=True)
class ContinuationImportResult:
    """A service-level continuation import."""

    import_result: ImportResult
    conversation: dict[str, Any] | None
    restored: bool


class AgentRunner(Protocol):
    """Callable agent runner protocol."""

    def __call__(
        self,
        root: Path,
        *,
        agent: str,
        prompt: str,
        on_output: OutputCallback | None = None,
    ) -> AgentRunResult:
        """Run an agent command."""


def create_conversation(
    start: Path | str = ".",
    *,
    title: str = "",
    agent: str = DEFAULT_AGENT,
    machine: str = DEFAULT_MACHINE,
    owner: str | None = None,
) -> dict[str, Any]:
    """Create a user-visible WaveForward conversation."""

    root = Path(start)
    initialize_workspace(root)
    now = utc_now()
    clean_owner = _clean_owner(owner)
    conversation = {
        "version": CONVERSATION_VERSION,
        "id": _build_id("conv"),
        "title": title.strip() or "New session",
        "created_at": now,
        "updated_at": now,
        "workspace": str(root.resolve()),
        "preferred": {
            "agent": agent.strip() or DEFAULT_AGENT,
            "machine": machine.strip() or DEFAULT_MACHINE,
        },
        "messages": [],
        "continuations": [],
    }
    if clean_owner:
        conversation["owner"] = clean_owner
    _write_conversation(root, conversation)
    return conversation


def ensure_default_conversation(
    start: Path | str = ".",
    *,
    owner: str | None = None,
) -> dict[str, Any]:
    """Return the newest conversation, creating one if needed."""

    conversations = list_conversations(start, owner=owner)
    if conversations:
        return get_conversation(start, conversations[0]["id"], owner=owner)
    return create_conversation(start, title="New session", owner=owner)


def list_conversations(
    start: Path | str = ".",
    *,
    include_archived: bool = False,
    owner: str | None = None,
) -> list[dict[str, Any]]:
    """List known WaveForward conversations, newest first."""

    root = Path(start)
    clean_owner = _clean_owner(owner)
    conversations_dir = sync_dir(root) / "conversations"
    if not conversations_dir.exists():
        return []

    conversations = []
    for path in conversations_dir.glob("*.json"):
        try:
            conversation = _read_conversation(path)
        except (OSError, json.JSONDecodeError):
            continue
        if not _owner_matches(conversation, clean_owner):
            continue
        if conversation.get("archived_at") and not include_archived:
            continue
        conversations.append(conversation)
    return sorted(
        conversations,
        key=lambda item: item.get("updated_at", ""),
        reverse=True,
    )


def get_conversation(
    start: Path | str,
    conversation_id: str,
    *,
    owner: str | None = None,
) -> dict[str, Any]:
    """Read a conversation by id."""

    root = Path(start)
    path = _conversation_path(root, conversation_id)
    conversation = _read_conversation(path)
    _ensure_owner(conversation, owner, "conversation")
    return conversation


def save_conversation(
    start: Path | str,
    conversation: dict[str, Any],
    *,
    owner: str | None = None,
) -> dict[str, Any]:
    """Persist a service conversation object."""

    root = Path(start)
    initialize_workspace(root)
    _ensure_owner(conversation, owner, "conversation")
    clean_owner = _clean_owner(owner)
    if clean_owner and not conversation.get("owner"):
        conversation["owner"] = clean_owner
    _write_conversation(root, conversation)
    return conversation


def archive_conversation(
    start: Path | str,
    conversation_id: str,
    *,
    archived: bool = True,
    owner: str | None = None,
) -> dict[str, Any]:
    """Archive or restore a WaveForward conversation."""

    root = Path(start)
    conversation = get_conversation(root, conversation_id, owner=owner)
    conversation["updated_at"] = utc_now()
    if archived:
        conversation["archived_at"] = conversation["updated_at"]
    else:
        conversation.pop("archived_at", None)
    _write_conversation(root, conversation)
    return conversation


def delete_conversation(
    start: Path | str,
    conversation_id: str,
    *,
    delete_artifacts: bool = True,
    owner: str | None = None,
) -> dict[str, Any]:
    """Delete a WaveForward conversation and its owned continuation artifacts."""

    root = Path(start)
    conversation = get_conversation(root, conversation_id, owner=owner)
    artifacts_deleted = 0
    if delete_artifacts:
        artifacts_deleted = _delete_conversation_artifacts(root, conversation)
    _conversation_path(root, conversation_id).unlink(missing_ok=True)
    return {
        "conversation_id": conversation_id,
        "deleted": True,
        "artifacts_deleted": artifacts_deleted,
    }


def add_message(
    start: Path | str,
    conversation_id: str,
    *,
    role: str,
    content: str,
    agent: str | None = None,
    machine: str | None = None,
    owner: str | None = None,
) -> dict[str, Any]:
    """Append a message to a WaveForward conversation."""

    if role not in {"user", "assistant", "service"}:
        raise ValueError(f"Unsupported message role: {role}")
    text = content.strip()
    if not text:
        raise ValueError("Message content is required.")

    root = Path(start)
    conversation = get_conversation(root, conversation_id, owner=owner)
    now = utc_now()
    message = {
        "id": _build_id("msg"),
        "role": role,
        "content": text,
        "created_at": now,
    }
    if agent:
        message["agent"] = agent
    if machine:
        message["machine"] = machine
    conversation["messages"].append(message)
    conversation["updated_at"] = now
    _write_conversation(root, conversation)
    return conversation


def prepare_continuation(
    start: Path | str,
    conversation_id: str,
    *,
    agent: str | None = None,
    machine: str | None = None,
    include_untracked: bool = True,
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    owner: str | None = None,
) -> ContinuationResult:
    """Prepare hidden snapshot and continuation artifacts for a conversation."""

    root = Path(start)
    conversation = get_conversation(root, conversation_id, owner=owner)
    destination_agent = agent or conversation["preferred"]["agent"]
    destination_machine = machine or conversation["preferred"]["machine"]
    conversation["preferred"] = {
        "agent": destination_agent,
        "machine": destination_machine,
    }

    snapshot = create_snapshot(
        root,
        message=f"WaveForward conversation: {conversation['title']}",
        task=_render_service_context(
            conversation,
            agent=destination_agent,
            machine=destination_machine,
        ),
        include_untracked=include_untracked,
        max_file_bytes=max_file_bytes,
    )
    handoff_path = generate_handoff(
        root,
        snapshot_ref=snapshot.snapshot_id,
        target=destination_agent,
    )
    now = utc_now()
    continuation = {
        "id": _build_id("cont"),
        "created_at": now,
        "agent": destination_agent,
        "machine": destination_machine,
        "snapshot_id": snapshot.snapshot_id,
        "handoff_path": str(handoff_path),
        "status": "ready",
    }
    conversation["continuations"].append(continuation)
    conversation["updated_at"] = now
    _write_conversation(root, conversation)
    _write_snapshot_conversation(snapshot.path, conversation)
    return ContinuationResult(conversation=conversation, continuation=continuation)


def run_conversation_turn(
    start: Path | str,
    conversation_id: str,
    *,
    content: str,
    agent: str | None = None,
    machine: str | None = None,
    execute_agent: bool = True,
    agent_runner: AgentRunner = run_agent,
    on_output: OutputCallback | None = None,
    owner: str | None = None,
) -> ConversationTurnResult:
    """Run one user-visible WaveForward conversation turn."""

    root = Path(start)
    conversation = add_message(
        root,
        conversation_id,
        role="user",
        content=content,
        agent=agent,
        machine=machine,
        owner=owner,
    )
    return complete_conversation_turn(
        root,
        conversation_id,
        agent=agent,
        machine=machine,
        execute_agent=execute_agent,
        agent_runner=agent_runner,
        on_output=on_output,
        conversation=conversation,
        owner=owner,
    )


def complete_conversation_turn(
    start: Path | str,
    conversation_id: str,
    *,
    agent: str | None = None,
    machine: str | None = None,
    execute_agent: bool = True,
    agent_runner: AgentRunner = run_agent,
    on_output: OutputCallback | None = None,
    conversation: dict[str, Any] | None = None,
    owner: str | None = None,
) -> ConversationTurnResult:
    """Complete a turn after the user message is already recorded."""

    root = Path(start)
    if conversation is None:
        conversation = get_conversation(root, conversation_id, owner=owner)
    else:
        _ensure_owner(conversation, owner, "conversation")
    destination_agent = agent or conversation["preferred"]["agent"]
    destination_machine = machine or conversation["preferred"]["machine"]
    conversation = _set_preferred_route(
        root,
        conversation,
        agent=destination_agent,
        machine=destination_machine,
    )
    agent_run = None
    if execute_agent:
        prompt = _render_agent_prompt(
            conversation,
            agent=destination_agent,
            machine=destination_machine,
        )
        agent_run = _run_agent_runner(
            agent_runner,
            root,
            agent=destination_agent,
            prompt=prompt,
            on_output=on_output,
        )
        conversation = add_message(
            root,
            conversation_id,
            role="assistant",
            content=agent_run.output or f"{destination_agent} completed the turn.",
            agent=destination_agent,
            machine=destination_machine,
            owner=owner,
        )

    return ConversationTurnResult(
        conversation=conversation,
        continuation=None,
        agent_run=agent_run,
    )


def export_continuation_bundle(
    start: Path | str,
    conversation_id: str,
    *,
    continuation_id: str | None = None,
    output: Path | str | None = None,
    overwrite: bool = False,
    owner: str | None = None,
) -> ContinuationBundleResult:
    """Export the snapshot bundle for a conversation continuation."""

    root = Path(start)
    conversation = get_conversation(root, conversation_id, owner=owner)
    continuation = _resolve_export_continuation(
        root,
        conversation,
        continuation_id=continuation_id,
        owner=owner,
    )
    bundle = export_snapshot_bundle(
        root,
        snapshot_ref=continuation["snapshot_id"],
        output=output,
        overwrite=overwrite,
    )
    return ContinuationBundleResult(
        conversation_id=conversation["id"],
        continuation_id=continuation["id"],
        bundle=bundle,
    )


def import_continuation_bundle(
    start: Path | str,
    *,
    bundle: Path | str,
    replace: bool = False,
    apply: bool = True,
    force: bool = False,
    owner: str | None = None,
) -> ContinuationImportResult:
    """Import a service-level continuation bundle into a workspace."""

    root = Path(start)
    imported = import_snapshot_bundle(root, bundle=bundle, replace=replace)
    snapshot_path = imported.path
    conversation = None
    conversation_path = snapshot_path / "conversation.json"
    if conversation_path.exists():
        conversation = json.loads(conversation_path.read_text(encoding="utf-8"))
        clean_owner = _clean_owner(owner)
        if clean_owner:
            conversation["owner"] = clean_owner
        _write_conversation(root, conversation)

    if apply:
        restore_snapshot(
            root,
            snapshot_ref=imported.snapshot_id,
            apply=True,
            force=force,
        )
    return ContinuationImportResult(
        import_result=imported,
        conversation=conversation,
        restored=apply,
    )


def service_status(
    start: Path | str = ".",
    *,
    owner: str | None = None,
) -> dict[str, Any]:
    """Return status for the WaveForward local service UI."""

    root = Path(start)
    initialized = (sync_dir(root) / "config.toml").exists()
    if initialized:
        conversations = [
            _conversation_summary(item)
            for item in list_conversations(root, owner=owner)
        ]
        archived_conversations = [
            _conversation_summary(item)
            for item in list_conversations(root, include_archived=True, owner=owner)
            if item.get("archived_at")
        ]
        snapshots = [_snapshot_summary(item) for item in list_snapshots(root)]
    else:
        conversations = []
        archived_conversations = []
        snapshots = []
    return {
        "workspace": str(root.resolve()),
        "machine": _service_machine_name(root),
        "initialized": initialized,
        "agents": agent_capabilities(),
        "checks": [
            {"name": check.name, "status": check.status, "detail": check.detail}
            for check in run_doctor(root)
        ],
        "conversations": conversations,
        "archived_conversations": archived_conversations,
        "snapshots": snapshots,
    }


def _render_service_context(
    conversation: dict[str, Any],
    *,
    agent: str,
    machine: str,
) -> str:
    lines = [
        "Continue the WaveForward conversation below.",
        f"Conversation: {conversation['title']}",
        f"Destination agent: {agent}",
        f"Destination machine: {machine}",
        "",
        "Canonical transcript:",
    ]
    for message in conversation["messages"][-40:]:
        role = message["role"]
        content = message["content"].strip()
        lines.append(f"\n[{role}]")
        lines.append(content)
    return "\n".join(lines).strip()


def _render_agent_prompt(
    conversation: dict[str, Any],
    *,
    agent: str,
    machine: str,
) -> str:
    lines = [
        "You are running inside a WaveForward-managed conversation.",
        f"Conversation: {conversation['title']}",
        f"Agent: {agent}",
        f"Machine: {machine}",
        "",
        "Use the transcript below as the canonical chat context. Continue the",
        "same conversation and respond to the latest user message.",
        "",
        "Transcript:",
    ]
    for message in _agent_visible_messages(conversation)[-40:]:
        role = "User" if message["role"] == "user" else "Assistant"
        lines.append(f"\n[{role}]")
        lines.append(message["content"].strip())
    lines.extend(
        [
            "",
            "Work directly in this repository when the user asks for code or file",
            "changes. Do not commit changes unless the user explicitly asks for a",
            "commit.",
        ]
    )
    return "\n".join(lines).strip()


def _conversation_summary(item: dict[str, Any]) -> dict[str, Any]:
    messages = item.get("messages", [])
    continuations = item.get("continuations", [])
    return {
        "id": item.get("id", ""),
        "title": item.get("title", "New session"),
        "updated_at": item.get("updated_at", ""),
        "message_count": len(messages),
        "continuation_count": len(continuations),
        "preferred": item.get("preferred", {}),
        "archived_at": item.get("archived_at"),
    }


def _set_preferred_route(
    root: Path,
    conversation: dict[str, Any],
    *,
    agent: str,
    machine: str,
) -> dict[str, Any]:
    preferred = {"agent": agent, "machine": machine}
    if conversation.get("preferred") == preferred:
        return conversation
    conversation = {**conversation, "preferred": preferred, "updated_at": utc_now()}
    _write_conversation(root, conversation)
    return conversation


def claim_unowned_conversations(start: Path | str, owner: str) -> int:
    """Assign old unowned conversations to one owner."""

    root = Path(start)
    clean_owner = _clean_owner(owner)
    if not clean_owner:
        return 0
    conversations_dir = sync_dir(root) / "conversations"
    if not conversations_dir.exists():
        return 0
    changed = 0
    for path in conversations_dir.glob("*.json"):
        try:
            conversation = _read_conversation(path)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if conversation.get("owner"):
            continue
        conversation["owner"] = clean_owner
        _write_conversation(root, conversation)
        changed += 1
    return changed


def _agent_visible_messages(conversation: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        message
        for message in conversation.get("messages", [])
        if message.get("role") in {"user", "assistant"} and message.get("content")
    ]


def _snapshot_summary(item: dict[str, Any]) -> dict[str, Any]:
    git = item.get("git", {})
    capture = item.get("capture", {})
    return {
        "id": item.get("id", ""),
        "created_at": item.get("created_at", ""),
        "message": item.get("message", ""),
        "branch": git.get("branch"),
        "dirty": bool(git.get("dirty", False)),
        "untracked_files": int(capture.get("untracked_files", 0)),
        "skipped_untracked_files": int(capture.get("skipped_untracked_files", 0)),
    }


def _run_agent_runner(
    agent_runner: AgentRunner,
    root: Path,
    *,
    agent: str,
    prompt: str,
    on_output: OutputCallback | None,
) -> AgentRunResult:
    if on_output is None or not _runner_accepts_output(agent_runner):
        return agent_runner(root, agent=agent, prompt=prompt)
    return agent_runner(root, agent=agent, prompt=prompt, on_output=on_output)


def _runner_accepts_output(agent_runner: AgentRunner) -> bool:
    try:
        parameters = inspect.signature(agent_runner).parameters
    except (TypeError, ValueError):
        return True
    return "on_output" in parameters or any(
        item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values()
    )


def _service_machine_name(root: Path) -> str:
    config_path = sync_dir(root) / "config.toml"
    if config_path.exists():
        try:
            config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError:
            config = {}
        machine = config.get("machine", {})
        if isinstance(machine, dict):
            name = machine.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
    return platform.node() or DEFAULT_MACHINE


def _resolve_continuation(
    conversation: dict[str, Any],
    continuation_id: str | None,
) -> dict[str, Any]:
    continuations = conversation.get("continuations", [])
    if not continuations:
        raise ValueError("Conversation does not have a prepared continuation.")
    if continuation_id is None or continuation_id == "latest":
        return continuations[-1]
    for item in continuations:
        if item.get("id") == continuation_id:
            return item
    raise ValueError(f"Unknown continuation: {continuation_id}")


def _resolve_export_continuation(
    root: Path,
    conversation: dict[str, Any],
    *,
    continuation_id: str | None,
    owner: str | None = None,
) -> dict[str, Any]:
    if continuation_id not in {None, "latest"}:
        return _resolve_continuation(conversation, continuation_id)

    continuations = conversation.get("continuations", [])
    latest = continuations[-1] if continuations else None
    if latest is not None and latest.get("created_at", "") >= conversation.get(
        "updated_at", ""
    ):
        return latest

    prepared = prepare_continuation(
        root,
        conversation["id"],
        agent=conversation["preferred"]["agent"],
        machine=conversation["preferred"]["machine"],
        owner=owner,
    )
    _sync_latest_snapshot_conversation(root, prepared.continuation["snapshot_id"])
    return prepared.continuation


def _conversation_path(root: Path, conversation_id: str) -> Path:
    if not conversation_id.startswith("conv_") or "/" in conversation_id:
        raise ValueError(f"Unknown conversation: {conversation_id}")
    return sync_dir(root) / "conversations" / f"{conversation_id}.json"


def _delete_conversation_artifacts(root: Path, conversation: dict[str, Any]) -> int:
    deleted = 0
    base = sync_dir(root)
    for continuation in conversation.get("continuations", []):
        snapshot_id = continuation.get("snapshot_id")
        if isinstance(snapshot_id, str) and _safe_artifact_name(snapshot_id):
            deleted += _remove_directory(base / "sessions" / snapshot_id)
            deleted += _remove_file(base / "bundles" / f"{snapshot_id}.wfbundle.tar.gz")

        handoff = continuation.get("handoff_path")
        if isinstance(handoff, str):
            deleted += _remove_path_under_base(root, base, handoff)
    return deleted


def _safe_artifact_name(value: str) -> bool:
    return (
        bool(value)
        and "/" not in value
        and "\\" not in value
        and value
        not in {
            ".",
            "..",
        }
    )


def _remove_path_under_base(root: Path, base: Path, raw_path: str) -> int:
    path = Path(raw_path)
    if not path.is_absolute():
        path = root / path
    try:
        resolved_path = path.resolve()
        resolved_base = base.resolve()
        if not resolved_path.is_relative_to(resolved_base):
            return 0
    except OSError:
        return 0
    if resolved_path.is_dir():
        return _remove_directory(resolved_path)
    return _remove_file(resolved_path)


def _remove_directory(path: Path) -> int:
    if not path.exists() or not path.is_dir():
        return 0
    shutil.rmtree(path)
    return 1


def _remove_file(path: Path) -> int:
    if not path.exists() or not path.is_file():
        return 0
    path.unlink()
    return 1


def _read_conversation(path: Path) -> dict[str, Any]:
    conversation = json.loads(path.read_text(encoding="utf-8"))
    if conversation.get("version") != CONVERSATION_VERSION:
        raise ValueError("Unsupported conversation version.")
    return conversation


def _clean_owner(owner: str | None) -> str:
    return str(owner or "").strip().lower()


def _owner_matches(conversation: dict[str, Any], owner: str) -> bool:
    return not owner or str(conversation.get("owner") or "").lower() == owner


def _ensure_owner(
    conversation: dict[str, Any],
    owner: str | None,
    label: str,
) -> None:
    clean_owner = _clean_owner(owner)
    if not clean_owner:
        return
    if str(conversation.get("owner") or "").lower() == clean_owner:
        return
    raise AgentSyncError(f"Unknown {label}.")


def _write_conversation(root: Path, conversation: dict[str, Any]) -> None:
    path = _conversation_path(root, conversation["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(conversation, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _write_snapshot_conversation(
    snapshot_path: Path,
    conversation: dict[str, Any],
) -> None:
    snapshot_path.joinpath("conversation.json").write_text(
        json.dumps(conversation, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sync_latest_snapshot_conversation(root: Path, snapshot_id: str) -> None:
    snapshot_path = sync_dir(root) / "sessions" / snapshot_id
    conversation_path = snapshot_path / "conversation.json"
    if not conversation_path.exists():
        return
    conversation = json.loads(conversation_path.read_text(encoding="utf-8"))
    latest = _conversation_path(root, conversation["id"])
    if latest.exists():
        shutil.copy2(latest, conversation_path)


def _build_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"
