"""Service-level conversation model for WaveForward."""

from __future__ import annotations

import inspect
import json
import platform
import shlex
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
class SlashCommandResult:
    """A completed WaveForward-native slash command."""

    conversation: dict[str, Any]
    command: dict[str, Any]


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
        model: str | None = None,
        reasoning_effort: str | None = None,
        on_output: OutputCallback | None = None,
    ) -> AgentRunResult:
        """Run an agent command."""


def create_conversation(
    start: Path | str = ".",
    *,
    title: str = "",
    agent: str = DEFAULT_AGENT,
    machine: str = DEFAULT_MACHINE,
    model: str | None = None,
    reasoning_effort: str | None = None,
    owner: str | None = None,
    workspace: str | None = None,
) -> dict[str, Any]:
    """Create a user-visible WaveForward conversation."""

    root = Path(start)
    initialize_workspace(root)
    now = utc_now()
    clean_owner = _clean_owner(owner)
    workspace_value = str(workspace or "").strip()
    conversation = {
        "version": CONVERSATION_VERSION,
        "id": _build_id("conv"),
        "title": title.strip() or "New session",
        "created_at": now,
        "updated_at": now,
        "workspace": workspace_value or str(root.resolve()),
        "preferred": {
            "agent": agent.strip() or DEFAULT_AGENT,
            "machine": machine.strip() or DEFAULT_MACHINE,
        },
        "messages": [],
        "continuations": [],
    }
    _set_route_options(
        conversation["preferred"],
        model=model,
        reasoning_effort=reasoning_effort,
    )
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


def rename_conversation(
    start: Path | str,
    conversation_id: str,
    *,
    title: str,
    owner: str | None = None,
) -> dict[str, Any]:
    """Rename a WaveForward conversation."""

    clean_title = title.strip()
    if not clean_title:
        raise ValueError("Session title is required.")
    root = Path(start)
    conversation = get_conversation(root, conversation_id, owner=owner)
    conversation["title"] = clean_title
    conversation["updated_at"] = utc_now()
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
    model: str | None = None,
    reasoning_effort: str | None = None,
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
    if model:
        message["model"] = model
    if reasoning_effort:
        message["reasoning_effort"] = reasoning_effort
    conversation["messages"].append(message)
    conversation["updated_at"] = now
    _write_conversation(root, conversation)
    return conversation


def execute_slash_command(
    start: Path | str,
    conversation_id: str,
    *,
    content: str,
    agent: str | None = None,
    machine: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    status: dict[str, Any] | None = None,
    owner: str | None = None,
) -> SlashCommandResult:
    """Execute one WaveForward-native slash command in a conversation."""

    root = Path(start)
    parsed = _parse_slash_command(content)
    if parsed is None:
        raise ValueError("Slash command must start with /.")
    name, args = parsed
    conversation = add_message(
        root,
        conversation_id,
        role="user",
        content=content,
        agent=agent,
        machine=machine,
        model=model,
        reasoning_effort=reasoning_effort,
        owner=owner,
    )
    preferred = conversation.get("preferred", {})
    active_agent = str(agent or preferred.get("agent") or DEFAULT_AGENT)
    active_machine = str(machine or preferred.get("machine") or DEFAULT_MACHINE)
    active_model = model if model is not None else preferred.get("model")
    active_reasoning = (
        reasoning_effort
        if reasoning_effort is not None
        else preferred.get("reasoning_effort")
    )
    context_status = status or service_status(root, owner=owner)
    command = {
        "name": name,
        "args": args,
        "route": {
            "agent": active_agent,
            "machine": active_machine,
            "model": str(active_model or ""),
            "reasoning_effort": str(active_reasoning or ""),
        },
    }

    if name in {"help", "h", "?"}:
        output = _slash_help()
    elif name in {"sessions", "session"}:
        output = _slash_sessions(context_status)
    elif name in {"machines", "machine"}:
        output = _slash_machines(context_status)
    elif name in {"agents", "agent"}:
        output = _slash_agents(context_status)
    elif name == "model":
        route, output = _slash_model(
            args,
            agent=active_agent,
            machine=active_machine,
            model=str(active_model or ""),
            reasoning_effort=str(active_reasoning or ""),
        )
        command["route"] = route
        conversation = _set_preferred_route(
            root,
            conversation,
            agent=route["agent"],
            machine=route["machine"],
            model=route["model"],
            reasoning_effort=route["reasoning_effort"],
        )
    elif name == "archive":
        output = "Session archived."
        conversation = archive_conversation(
            root,
            conversation_id,
            archived=True,
            owner=owner,
        )
        command["archived"] = True
    elif name == "rename":
        title = " ".join(args).strip()
        if not title:
            output = "Usage: /rename New session title"
            command["error"] = "missing_title"
        else:
            conversation = rename_conversation(
                root,
                conversation_id,
                title=title,
                owner=owner,
            )
            output = f"Session renamed to: {title}"
            command["title"] = title
    else:
        output = f"Unknown command: /{name}\n\n{_slash_help()}"
        command["error"] = "unknown_command"

    conversation = add_message(
        root,
        conversation_id,
        role="service",
        content=output,
        agent=command["route"]["agent"],
        machine=command["route"]["machine"],
        model=command["route"].get("model") or None,
        reasoning_effort=command["route"].get("reasoning_effort") or None,
        owner=owner,
    )
    if command.get("archived"):
        conversation = archive_conversation(
            root,
            conversation_id,
            archived=True,
            owner=owner,
        )
    return SlashCommandResult(conversation=conversation, command=command)


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
    model: str | None = None,
    reasoning_effort: str | None = None,
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
        model=model,
        reasoning_effort=reasoning_effort,
        owner=owner,
    )
    return complete_conversation_turn(
        root,
        conversation_id,
        agent=agent,
        machine=machine,
        model=model,
        reasoning_effort=reasoning_effort,
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
    model: str | None = None,
    reasoning_effort: str | None = None,
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
    destination_model = (
        model if model is not None else conversation.get("preferred", {}).get("model")
    )
    destination_reasoning = (
        reasoning_effort
        if reasoning_effort is not None
        else conversation.get("preferred", {}).get("reasoning_effort")
    )
    conversation = _set_preferred_route(
        root,
        conversation,
        agent=destination_agent,
        machine=destination_machine,
        model=destination_model,
        reasoning_effort=destination_reasoning,
    )
    agent_run = None
    if execute_agent:
        prompt = _render_agent_prompt(
            conversation,
            agent=destination_agent,
            machine=destination_machine,
            model=destination_model,
            reasoning_effort=destination_reasoning,
        )
        agent_run = _run_agent_runner(
            agent_runner,
            root,
            agent=destination_agent,
            model=destination_model,
            reasoning_effort=destination_reasoning,
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
            model=destination_model,
            reasoning_effort=destination_reasoning,
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


def _parse_slash_command(content: str) -> tuple[str, list[str]] | None:
    text = content.strip()
    if not text.startswith("/") or text.startswith("//"):
        return None
    body = text[1:].strip()
    if not body:
        return "help", []
    try:
        parts = shlex.split(body)
    except ValueError as error:
        raise ValueError(f"Could not parse slash command: {error}") from error
    if not parts:
        return "help", []
    return parts[0].lower(), parts[1:]


def _slash_help() -> str:
    return "\n".join(
        [
            "WaveForward commands",
            "/help - Show available commands.",
            "/sessions - List recent active and archived sessions.",
            "/machines - List connected machines.",
            "/agents - List available agents.",
            "/model [model] [effort] - Set or show the current route model.",
            "/archive - Archive this session.",
            "/rename <title> - Rename this session.",
            "",
            "Use // at the start of a message to send a literal slash command "
            "to the agent.",
        ]
    )


def _slash_sessions(status: dict[str, Any]) -> str:
    active = list(status.get("conversations") or [])
    archived = list(status.get("archived_conversations") or [])
    lines = [
        f"Sessions: {len(active)} active, {len(archived)} archived.",
    ]
    if active:
        lines.append("")
        lines.append("Active")
        lines.extend(_format_session_summary(item) for item in active[:12])
    if archived:
        lines.append("")
        lines.append("Archived")
        lines.extend(_format_session_summary(item) for item in archived[:8])
    if not active and not archived:
        lines.append("No sessions yet.")
    return "\n".join(lines)


def _slash_machines(status: dict[str, Any]) -> str:
    lines = [
        f"Service machine: {status.get('machine') or DEFAULT_MACHINE}",
        f"Workspace: {status.get('workspace') or '-'}",
    ]
    machines = list(status.get("daemon_machines") or [])
    environments = list(status.get("environments") or [])
    if machines:
        lines.append("")
        lines.append("Connected machines")
        for machine in machines[:12]:
            name = machine.get("name") or machine.get("id") or "Machine"
            state = "online" if machine.get("online") else "offline"
            workspace = machine.get("workspace") or "workspace unknown"
            lines.append(f"- {name}: {state}, {workspace}")
    if environments:
        lines.append("")
        lines.append("Saved environments")
        for environment in environments[:12]:
            name = environment.get("name") or "Environment"
            endpoint = environment.get("endpoint") or environment.get("ssh_host") or "-"
            lines.append(f"- {name}: {endpoint}")
    if not machines and not environments:
        lines.append("")
        lines.append("No remote machines are connected yet.")
    return "\n".join(lines)


def _slash_agents(status: dict[str, Any]) -> str:
    lines = ["Agents"]
    local_agents = list(status.get("agents") or [])
    if local_agents:
        lines.append("")
        lines.append("This service")
        lines.extend(_format_agent_summary(item) for item in local_agents)
    for machine in list(status.get("daemon_machines") or [])[:12]:
        agents = list(machine.get("agents") or [])
        if not agents:
            continue
        lines.append("")
        lines.append(str(machine.get("name") or machine.get("id") or "Machine"))
        lines.extend(_format_agent_summary(item) for item in agents)
    if len(lines) == 1:
        lines.append("No agent information is available yet.")
    return "\n".join(lines)


def _slash_model(
    args: list[str],
    *,
    agent: str,
    machine: str,
    model: str,
    reasoning_effort: str,
) -> tuple[dict[str, str], str]:
    efforts = {"low", "medium", "high", "xhigh"}
    clean_args = [item.strip() for item in args if item.strip()]
    next_model = model
    next_effort = reasoning_effort
    if not clean_args:
        route = {
            "agent": agent,
            "machine": machine,
            "model": next_model,
            "reasoning_effort": next_effort,
        }
        return route, _format_model_route(route)
    if len(clean_args) == 1 and clean_args[0].lower() in efforts:
        next_effort = clean_args[0].lower()
    elif len(clean_args) == 1 and clean_args[0].lower() == "default":
        next_model = ""
        next_effort = ""
    else:
        next_model = "" if clean_args[0].lower() == "default" else clean_args[0]
        if len(clean_args) >= 2:
            effort = clean_args[1].lower()
            if effort not in efforts and effort != "default":
                route = {
                    "agent": agent,
                    "machine": machine,
                    "model": model,
                    "reasoning_effort": reasoning_effort,
                }
                return (
                    route,
                    "Usage: /model [model] [low|medium|high|xhigh]\n"
                    "Use /model default to clear model and effort.",
                )
            next_effort = "" if effort == "default" else effort
    route = {
        "agent": agent,
        "machine": machine,
        "model": next_model,
        "reasoning_effort": next_effort,
    }
    return route, f"Model route updated.\n\n{_format_model_route(route)}"


def _format_model_route(route: dict[str, str]) -> str:
    return "\n".join(
        [
            f"Agent: {route.get('agent') or DEFAULT_AGENT}",
            f"Machine: {route.get('machine') or DEFAULT_MACHINE}",
            f"Model: {route.get('model') or 'default'}",
            f"Effort: {route.get('reasoning_effort') or 'default'}",
        ]
    )


def _format_session_summary(item: dict[str, Any]) -> str:
    preferred = item.get("preferred") or {}
    route = " / ".join(
        part
        for part in (
            str(preferred.get("agent") or ""),
            str(preferred.get("machine") or ""),
        )
        if part
    )
    workspace = item.get("workspace_name") or _workspace_name(
        str(item.get("workspace") or "")
    )
    count = int(item.get("message_count") or 0)
    title = item.get("title") or "New session"
    suffix = f" - {route}" if route else ""
    return f"- {title} ({workspace}, {count} messages){suffix}"


def _format_agent_summary(item: dict[str, Any]) -> str:
    state = "available" if item.get("available") else "unavailable"
    label = item.get("label") or item.get("id") or "agent"
    models = [
        str(option.get("value") or "").strip()
        for option in item.get("model_options") or []
        if str(option.get("value") or "").strip()
    ]
    model_text = f"; models: {', '.join(models[:4])}" if models else ""
    return f"- {label}: {state}{model_text}"


def _render_service_context(
    conversation: dict[str, Any],
    *,
    agent: str,
    machine: str,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> str:
    lines = [
        "Continue the WaveForward conversation below.",
        f"Conversation: {conversation['title']}",
        f"Destination agent: {agent}",
        f"Destination machine: {machine}",
        f"Destination model: {model or 'default'}",
        f"Destination reasoning effort: {reasoning_effort or 'default'}",
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
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> str:
    lines = [
        "You are running inside a WaveForward-managed conversation.",
        f"Conversation: {conversation['title']}",
        f"Agent: {agent}",
        f"Machine: {machine}",
        f"Model: {model or 'default'}",
        f"Reasoning effort: {reasoning_effort or 'default'}",
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
    workspace = str(item.get("workspace") or "")
    return {
        "id": item.get("id", ""),
        "title": item.get("title", "New session"),
        "updated_at": item.get("updated_at", ""),
        "workspace": workspace,
        "workspace_name": _workspace_name(workspace),
        "message_count": len(messages),
        "continuation_count": len(continuations),
        "preferred": item.get("preferred", {}),
        "archived_at": item.get("archived_at"),
    }


def _workspace_name(workspace: str) -> str:
    clean = workspace.strip().replace("\\", "/").rstrip("/")
    if not clean:
        return "No workspace"
    parts = [part for part in clean.split("/") if part]
    return parts[-1] if parts else clean


def _set_preferred_route(
    root: Path,
    conversation: dict[str, Any],
    *,
    agent: str,
    machine: str,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    preferred = {"agent": agent, "machine": machine}
    _set_route_options(preferred, model=model, reasoning_effort=reasoning_effort)
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
    model: str | None,
    reasoning_effort: str | None,
    prompt: str,
    on_output: OutputCallback | None,
) -> AgentRunResult:
    kwargs: dict[str, Any] = {"agent": agent, "prompt": prompt}
    if model and _runner_accepts_parameter(agent_runner, "model"):
        kwargs["model"] = model
    if reasoning_effort and _runner_accepts_parameter(
        agent_runner,
        "reasoning_effort",
    ):
        kwargs["reasoning_effort"] = reasoning_effort
    if on_output is not None and _runner_accepts_parameter(agent_runner, "on_output"):
        kwargs["on_output"] = on_output
    return agent_runner(root, **kwargs)


def _runner_accepts_output(agent_runner: AgentRunner) -> bool:
    return _runner_accepts_parameter(agent_runner, "on_output")


def _runner_accepts_parameter(agent_runner: AgentRunner, name: str) -> bool:
    try:
        parameters = inspect.signature(agent_runner).parameters
    except (TypeError, ValueError):
        return True
    return name in parameters or any(
        item.kind is inspect.Parameter.VAR_KEYWORD for item in parameters.values()
    )


def _set_route_options(
    target: dict[str, Any],
    *,
    model: str | None,
    reasoning_effort: str | None,
) -> None:
    clean_model = str(model or "").strip()
    clean_reasoning = str(reasoning_effort or "").strip()
    if clean_model:
        target["model"] = clean_model
    if clean_reasoning:
        target["reasoning_effort"] = clean_reasoning


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
