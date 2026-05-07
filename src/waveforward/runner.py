"""Agent command runners for WaveForward local service turns."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from waveforward.core import AgentSyncError

DEFAULT_OPENCODE_MODEL = "opencode/minimax-m2.5-free"
MAX_AGENT_OUTPUT_CHARS = 6000
ANSI_PATTERN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
OutputCallback = Callable[[str], None]
REASONING_EFFORTS = ("low", "medium", "high", "xhigh")
AGENT_COMMANDS = {
    "claude-code": ("Claude Code", "claude"),
    "codex": ("Codex", "codex"),
    "opencode": ("OpenCode", "opencode"),
}
AGENT_MODEL_OPTIONS = {
    "claude-code": (
        {"value": "", "label": "Default"},
        {"value": "opus", "label": "Opus"},
        {"value": "sonnet", "label": "Sonnet"},
    ),
    "codex": (
        {"value": "", "label": "Default"},
        {"value": "gpt-5.5", "label": "GPT-5.5"},
        {"value": "gpt-5.4", "label": "GPT-5.4"},
        {"value": "gpt-5.4-mini", "label": "GPT-5.4 Mini"},
        {"value": "gpt-5.3-codex", "label": "GPT-5.3 Codex"},
        {"value": "gpt-5.3-codex-spark", "label": "GPT-5.3 Codex Spark"},
        {"value": "gpt-5.2", "label": "GPT-5.2"},
    ),
    "opencode": (
        {"value": DEFAULT_OPENCODE_MODEL, "label": "Minimax M2.5 Free"},
        {"value": "", "label": "Default"},
    ),
}


@dataclass(frozen=True)
class AgentRunResult:
    """Result from running a local agent command."""

    agent: str
    command: tuple[str, ...]
    returncode: int
    output: str
    model: str | None = None
    reasoning_effort: str | None = None


def run_agent(
    root: Path,
    *,
    agent: str,
    prompt: str,
    model: str | None = None,
    reasoning_effort: str | None = None,
    on_output: OutputCallback | None = None,
) -> AgentRunResult:
    """Run a supported local agent for a WaveForward conversation turn."""

    normalized = agent.lower().strip()
    if normalized in {"claude", "claude-code"}:
        return run_claude_code(
            root,
            prompt=prompt,
            model=model,
            reasoning_effort=reasoning_effort,
            on_output=on_output,
        )
    if normalized == "codex":
        return run_codex(
            root,
            prompt=prompt,
            model=model,
            reasoning_effort=reasoning_effort,
            on_output=on_output,
        )
    if normalized == "opencode":
        return run_opencode(
            root,
            prompt=prompt,
            model=model or DEFAULT_OPENCODE_MODEL,
            reasoning_effort=reasoning_effort,
            on_output=on_output,
        )
    raise AgentSyncError(f"Agent runner is not available yet: {agent}")


def agent_capabilities() -> list[dict[str, Any]]:
    """Return supported local agents and whether their commands are installed."""

    return [
        {
            "id": agent,
            "label": label,
            "command": command,
            "available": shutil.which(command) is not None,
            "model_options": list(AGENT_MODEL_OPTIONS.get(agent, ())),
            "reasoning_options": list(REASONING_EFFORTS),
            "supports_custom_model": True,
            "supports_reasoning_effort": agent == "codex",
        }
        for agent, (label, command) in AGENT_COMMANDS.items()
    ]


def run_claude_code(
    root: Path,
    *,
    prompt: str,
    model: str | None = None,
    reasoning_effort: str | None = None,
    on_output: OutputCallback | None = None,
) -> AgentRunResult:
    """Run Claude Code non-interactively in a workspace."""

    _require_agent_command("claude-code")
    model_args = ("--model", model.strip()) if model and model.strip() else ()
    command = (
        "claude",
        "--print",
        "--permission-mode",
        "acceptEdits",
        "--output-format",
        "text",
        *model_args,
        prompt,
    )
    returncode, output = _run_command(root, command, on_output=on_output)
    if returncode != 0:
        raise AgentSyncError(
            f"Claude Code failed with exit code {returncode}.\n{output}"
        )
    return AgentRunResult(
        agent="claude-code",
        command=command,
        returncode=returncode,
        output=output,
        model=model.strip() if model and model.strip() else None,
        reasoning_effort=_clean_reasoning_effort(reasoning_effort),
    )


def run_codex(
    root: Path,
    *,
    prompt: str,
    model: str | None = None,
    reasoning_effort: str | None = None,
    on_output: OutputCallback | None = None,
) -> AgentRunResult:
    """Run Codex CLI non-interactively in a workspace."""

    _require_agent_command("codex")
    model_args = ("--model", model.strip()) if model and model.strip() else ()
    effort = _clean_reasoning_effort(reasoning_effort)
    reasoning_args = (
        ("-c", f"model_reasoning_effort={json.dumps(effort)}") if effort else ()
    )
    with tempfile.TemporaryDirectory(prefix="waveforward-codex-") as temp_dir:
        last_message_path = Path(temp_dir) / "last-message.txt"
        command = (
            "codex",
            "exec",
            *model_args,
            *reasoning_args,
            "--dangerously-bypass-approvals-and-sandbox",
            "--cd",
            str(root),
            "--color",
            "never",
            "--output-last-message",
            str(last_message_path),
            prompt,
        )
        returncode, output = _run_command(root, command, on_output=on_output)
        if returncode == 0 and last_message_path.exists():
            output = _clean_output(last_message_path.read_text(encoding="utf-8"), "")
    if returncode != 0:
        raise AgentSyncError(f"Codex failed with exit code {returncode}.\n{output}")
    return AgentRunResult(
        agent="codex",
        command=command,
        returncode=returncode,
        output=output,
        model=model.strip() if model and model.strip() else None,
        reasoning_effort=effort,
    )


def run_opencode(
    root: Path,
    *,
    prompt: str,
    model: str = DEFAULT_OPENCODE_MODEL,
    reasoning_effort: str | None = None,
    on_output: OutputCallback | None = None,
) -> AgentRunResult:
    """Run OpenCode non-interactively in a workspace."""

    _require_agent_command("opencode")
    command = (
        "opencode",
        "run",
        "--model",
        model,
        "--dangerously-skip-permissions",
        prompt,
    )
    returncode, output = _run_command(root, command, on_output=on_output)
    if returncode != 0:
        raise AgentSyncError(f"OpenCode failed with exit code {returncode}.\n{output}")
    return AgentRunResult(
        agent="opencode",
        command=command,
        returncode=returncode,
        output=output,
        model=model,
        reasoning_effort=_clean_reasoning_effort(reasoning_effort),
    )


def _clean_reasoning_effort(value: str | None) -> str | None:
    effort = str(value or "").strip().lower()
    return effort if effort in REASONING_EFFORTS else None


def _run_command(
    root: Path,
    command: tuple[str, ...],
    *,
    on_output: OutputCallback | None,
) -> tuple[int, str]:
    if on_output is None:
        result = subprocess.run(
            command,
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
        )
        return result.returncode, _clean_output(result.stdout, result.stderr)

    process = subprocess.Popen(
        command,
        cwd=root,
        stderr=subprocess.STDOUT,
        stdout=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    chunks: list[str] = []
    if process.stdout is not None:
        for chunk in process.stdout:
            chunks.append(chunk)
            cleaned = ANSI_PATTERN.sub("", chunk)
            if cleaned:
                on_output(cleaned)
    return process.wait(), _clean_output("".join(chunks), "")


def _clean_output(stdout: str, stderr: str) -> str:
    text = "\n".join(part for part in (stdout, stderr) if part.strip())
    text = ANSI_PATTERN.sub("", text).strip()
    if len(text) > MAX_AGENT_OUTPUT_CHARS:
        return text[:MAX_AGENT_OUTPUT_CHARS].rstrip() + "\n...[truncated]"
    return text


def _require_agent_command(agent: str) -> None:
    label, command = AGENT_COMMANDS[agent]
    if shutil.which(command) is None:
        raise AgentSyncError(
            f"{label} is not installed or is not on PATH for this WaveForward service."
        )
