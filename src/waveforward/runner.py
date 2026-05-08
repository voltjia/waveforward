"""Agent command runners for WaveForward local service turns."""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from waveforward.core import AgentSyncError

DEFAULT_OPENCODE_MODEL = "opencode/minimax-m2.5-free"
MAX_AGENT_OUTPUT_CHARS = 6000
ANSI_PATTERN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
OutputCallback = Callable[[str], None]
CancelCheck = Callable[[], bool]
COMMAND_CANCEL_GRACE_SECONDS = 5.0
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
    cancel_check: CancelCheck | None = None,
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
            cancel_check=cancel_check,
        )
    if normalized == "codex":
        return run_codex(
            root,
            prompt=prompt,
            model=model,
            reasoning_effort=reasoning_effort,
            on_output=on_output,
            cancel_check=cancel_check,
        )
    if normalized == "opencode":
        return run_opencode(
            root,
            prompt=prompt,
            model=model or DEFAULT_OPENCODE_MODEL,
            reasoning_effort=reasoning_effort,
            on_output=on_output,
            cancel_check=cancel_check,
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
    cancel_check: CancelCheck | None = None,
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
    returncode, output = _run_command(
        root,
        command,
        on_output=on_output,
        cancel_check=cancel_check,
    )
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
    cancel_check: CancelCheck | None = None,
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
        returncode, output = _run_command(
            root,
            command,
            on_output=on_output,
            cancel_check=cancel_check,
        )
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
    cancel_check: CancelCheck | None = None,
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
    returncode, output = _run_command(
        root,
        command,
        on_output=on_output,
        cancel_check=cancel_check,
    )
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
    cancel_check: CancelCheck | None = None,
) -> tuple[int, str]:
    if on_output is None and cancel_check is None:
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
        start_new_session=True,
    )
    output_queue: queue.Queue[str | None] = queue.Queue()

    def read_stdout() -> None:
        try:
            if process.stdout is not None:
                for chunk in process.stdout:
                    output_queue.put(chunk)
        finally:
            output_queue.put(None)

    reader = threading.Thread(
        target=read_stdout,
        name="waveforward-agent-output",
        daemon=True,
    )
    reader.start()

    chunks: list[str] = []
    stdout_done = False
    canceled = False
    while process.poll() is None:
        stdout_done = _drain_output_queue(
            output_queue,
            chunks,
            on_output=on_output,
            stdout_done=stdout_done,
        )
        if cancel_check is not None and cancel_check():
            canceled = True
            _terminate_process_tree(process)
            break
        time.sleep(0.05)

    if canceled and process.poll() is None:
        deadline = time.monotonic() + COMMAND_CANCEL_GRACE_SECONDS
        while process.poll() is None and time.monotonic() < deadline:
            stdout_done = _drain_output_queue(
                output_queue,
                chunks,
                on_output=on_output,
                stdout_done=stdout_done,
            )
            time.sleep(0.05)
        if process.poll() is None:
            _kill_process_tree(process)

    returncode = process.wait()
    reader.join(timeout=1.0)
    if process.stdout is not None:
        with suppress(OSError):
            process.stdout.close()
    while not stdout_done:
        stdout_done = _drain_output_queue(
            output_queue,
            chunks,
            on_output=on_output,
            stdout_done=stdout_done,
        )
    if canceled:
        raise AgentSyncError("Run canceled.")
    return returncode, _clean_output("".join(chunks), "")


def _drain_output_queue(
    output_queue: queue.Queue[str | None],
    chunks: list[str],
    *,
    on_output: OutputCallback | None,
    stdout_done: bool,
) -> bool:
    done = stdout_done
    while True:
        try:
            chunk = output_queue.get_nowait()
        except queue.Empty:
            return done
        if chunk is None:
            done = True
            continue
        chunks.append(chunk)
        cleaned = ANSI_PATTERN.sub("", chunk)
        if cleaned and on_output is not None:
            on_output(cleaned)


def _terminate_process_tree(process: subprocess.Popen[str]) -> None:
    if _terminate_with_psutil(process, kill=False):
        return
    if os.name == "posix":
        with suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(process.pid, signal.SIGTERM)
            return
    with suppress(ProcessLookupError, PermissionError, OSError):
        process.terminate()


def _kill_process_tree(process: subprocess.Popen[str]) -> None:
    if _terminate_with_psutil(process, kill=True):
        return
    if os.name == "posix":
        with suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(process.pid, signal.SIGKILL)
            return
    with suppress(ProcessLookupError, PermissionError, OSError):
        process.kill()


def _terminate_with_psutil(process: subprocess.Popen[str], *, kill: bool) -> bool:
    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:
        return False
    try:
        parent = psutil.Process(process.pid)
        children = parent.children(recursive=True)
        for child in children:
            child.kill() if kill else child.terminate()
        parent.kill() if kill else parent.terminate()
    except psutil.Error:
        return False
    return True


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
