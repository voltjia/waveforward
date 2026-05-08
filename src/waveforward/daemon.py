"""Outbound daemon for connecting local machines to a WaveForward service."""

from __future__ import annotations

import base64
import json
import os
import platform
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from subprocess import DEVNULL, STDOUT, Popen
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, build_opener

from waveforward import __version__
from waveforward.core import (
    AgentSyncError,
    initialize_workspace,
    sync_dir,
    update_machine_name,
)
from waveforward.files import (
    list_workspace_tree,
    read_workspace_file,
    workspace_file_diff,
    write_workspace_file,
)
from waveforward.history import (
    DEFAULT_HISTORY_LIMIT,
    discover_agent_sessions,
    import_agent_sessions,
)
from waveforward.runner import AgentRunResult, agent_capabilities
from waveforward.service import (
    complete_conversation_turn,
    save_conversation,
    service_status,
)
from waveforward.store import atomic_write_text, write_json
from waveforward.update import check_for_update, download_update_wheel

UNSAFE_AGENT_EXECUTION_ENV = "WAVEFORWARD_ALLOW_UNSAFE_AGENT_EXECUTION"
PUBLIC_RELEASE_MANIFEST_URL = (
    "https://github.com/voltjia/waveforward/releases/latest/download/"
    "waveforward-release-manifest.json"
)
WORKSPACE_JOB_KINDS = {
    "workspace_tree",
    "workspace_file_read",
    "workspace_file_write",
    "workspace_file_diff",
}
READONLY_WORKSPACE_JOB_KINDS = {
    "workspace_tree",
    "workspace_file_read",
    "workspace_file_diff",
}
EXCLUSIVE_JOB_KINDS = {"daemon_update", "workspace_file_write"}


@dataclass
class _ActiveDaemonJob:
    """One daemon job running outside the control polling loop."""

    id: str
    kind: str
    thread: threading.Thread
    cancel_event: threading.Event
    started_at: float


@dataclass(frozen=True)
class DaemonConfig:
    """Runtime settings for the outbound daemon."""

    server: str
    auth_user: str | None = None
    auth_password: str | None = None
    auth_token: str | None = None
    machine_name: str | None = None
    poll_interval: float = 2.0
    request_retries: int = 3
    update_check_interval: float = 300.0
    update_manifest_url: str | None = None

    @classmethod
    def from_env(cls) -> DaemonConfig:
        """Build daemon settings from environment variables."""

        return cls(
            server=_required_env("WAVEFORWARD_DAEMON_SERVER"),
            auth_user=os.getenv("WAVEFORWARD_DAEMON_USER")
            or os.getenv("WAVEFORWARD_AUTH_USER"),
            auth_password=os.getenv("WAVEFORWARD_DAEMON_PASSWORD")
            or os.getenv("WAVEFORWARD_AUTH_PASSWORD"),
            auth_token=os.getenv("WAVEFORWARD_DAEMON_TOKEN")
            or os.getenv("WAVEFORWARD_AUTH_TOKEN"),
            machine_name=os.getenv("WAVEFORWARD_DAEMON_MACHINE"),
            poll_interval=float(os.getenv("WAVEFORWARD_DAEMON_INTERVAL", "2.0")),
            request_retries=_env_positive_int("WAVEFORWARD_DAEMON_REQUEST_RETRIES", 3),
            update_check_interval=float(
                os.getenv("WAVEFORWARD_DAEMON_UPDATE_INTERVAL", "300.0")
            ),
            update_manifest_url=os.getenv("WAVEFORWARD_DAEMON_UPDATE_MANIFEST_URL"),
        )


class CloudClient:
    """Small JSON client for the WaveForward daemon API."""

    def __init__(self, config: DaemonConfig) -> None:
        self.server = config.server.rstrip("/") + "/"
        self.auth_header = _build_auth_header(config)
        self.opener = build_opener()
        self.request_retries = max(config.request_retries, 1)

    def set_bearer_token(self, token: str) -> None:
        """Use a Bearer token for subsequent cloud requests."""

        value = token.strip()
        self.auth_header = f"Bearer {value}" if value else None

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Connection": "close",
            "Content-Type": "application/json",
        }
        if self.auth_header:
            headers["Authorization"] = self.auth_header

        for attempt in range(self.request_retries):
            request = Request(
                urljoin(self.server, path.lstrip("/")),
                data=body,
                headers=headers,
                method="POST",
            )
            try:
                response = self.opener.open(request, timeout=30)
                data = json.loads(response.read().decode("utf-8"))
            except HTTPError as error:
                detail = error.read().decode("utf-8", errors="replace")
                message = f"Cloud request failed: {error.code} {detail}"
                if error.code < 500 or attempt >= self.request_retries - 1:
                    raise AgentSyncError(message) from error
                time.sleep(_request_retry_delay(attempt))
            except (OSError, URLError) as error:
                reason = getattr(error, "reason", error)
                if attempt >= self.request_retries - 1:
                    raise AgentSyncError(f"Cloud request failed: {reason}") from error
                time.sleep(_request_retry_delay(attempt))
            except json.JSONDecodeError as error:
                raise AgentSyncError("Cloud returned invalid JSON.") from error
            else:
                if not data.get("ok", False):
                    raise AgentSyncError(
                        str(data.get("error") or "Cloud request failed.")
                    )
                return data
        else:
            raise AgentSyncError("Cloud request failed.")


def run_daemon(
    start: Path | str = ".",
    *,
    config: DaemonConfig,
    once: bool = False,
) -> None:
    """Run the outbound daemon loop."""

    root = Path(start)
    initialize_workspace(root)
    if config.machine_name:
        update_machine_name(root, config.machine_name)

    daemon_state = _load_or_create_daemon_state(root)
    client = CloudClient(config)
    if daemon_state.get("machine_token"):
        client.set_bearer_token(str(daemon_state["machine_token"]))
    retry_delay = max(config.poll_interval, 0.5)
    active_jobs: dict[str, _ActiveDaemonJob] = {}
    while True:
        try:
            claimed = _poll_once(
                client,
                root,
                daemon_state,
                config=config,
                active_jobs=active_jobs,
            )
            retry_delay = max(config.poll_interval, 0.5)
        except AgentSyncError as error:
            if once:
                raise
            _log_daemon_warning(error)
            time.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, 30.0)
            continue
        if once:
            while active_jobs:
                _refresh_active_jobs(
                    client,
                    active_jobs,
                    machine_id=str(daemon_state["machine_id"]),
                )
                time.sleep(0.05)
            if not claimed:
                return
            return
        time.sleep(max(config.poll_interval, 0.2))


def daemon_status(start: Path | str = ".") -> dict[str, Any]:
    """Return local daemon registration state without exposing token values."""

    root = Path(start)
    path = _daemon_state_path(root)
    pid_path = _daemon_pid_path(root)
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    machine_id = str(data.get("machine_id") or "").strip()
    pid = _read_daemon_pid(pid_path)
    return {
        "configured": bool(machine_id),
        "has_machine_token": bool(str(data.get("machine_token") or "").strip()),
        "log_path": str(_daemon_log_path(root)),
        "machine_id": machine_id,
        "pid": pid or None,
        "pid_path": str(pid_path),
        "running": bool(pid and _pid_running(pid)),
        "state_path": str(path),
    }


def start_daemon_process(
    start: Path | str = ".",
    *,
    config: DaemonConfig,
    allow_agent_execution: bool = False,
    python: str | None = None,
) -> dict[str, Any]:
    """Start the outbound daemon as a detached background process."""

    root = Path(start).resolve()
    initialize_workspace(root)
    pid_path = _daemon_pid_path(root)
    log_path = _daemon_log_path(root)
    existing_pid = _read_daemon_pid(pid_path)
    if existing_pid and _pid_running(existing_pid):
        return {
            "already_running": True,
            "log_path": str(log_path),
            "pid": existing_pid,
            "pid_path": str(pid_path),
            "started": False,
        }

    executable = (python or sys.executable).strip()
    if not executable:
        raise AgentSyncError("Python executable is required to start the daemon.")
    if not config.server:
        raise AgentSyncError("Missing daemon server URL.")

    command = _daemon_process_command(config, python=executable)
    env = os.environ.copy()
    if allow_agent_execution:
        env[UNSAFE_AGENT_EXECUTION_ENV] = "1"
    if config.update_manifest_url:
        env["WAVEFORWARD_DAEMON_UPDATE_MANIFEST_URL"] = config.update_manifest_url

    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as log_file:
        process = Popen(
            command,
            cwd=root,
            env=env,
            stdin=DEVNULL,
            stdout=log_file,
            stderr=STDOUT,
            start_new_session=True,
        )
    _write_daemon_pid(pid_path, process.pid)
    return {
        "already_running": False,
        "log_path": str(log_path),
        "pid": process.pid,
        "pid_path": str(pid_path),
        "started": True,
    }


def _poll_once(
    client: CloudClient,
    root: Path,
    daemon_state: dict[str, Any],
    *,
    config: DaemonConfig,
    active_jobs: dict[str, _ActiveDaemonJob],
) -> bool:
    machine_id = str(daemon_state["machine_id"])
    machine = _register(client, root, daemon_state, config=config)
    _refresh_active_jobs(client, active_jobs, machine_id=machine_id)
    accepted_kinds = _accepted_job_kinds(active_jobs)
    payload: dict[str, Any] = {"machine_id": machine_id}
    if accepted_kinds is not None:
        payload["accepted_kinds"] = sorted(accepted_kinds)
    job = client.post(
        "/api/daemon/jobs/next",
        payload,
    ).get("job")
    if job:
        _start_daemon_job(
            client,
            root,
            machine=machine,
            job=job,
            active_jobs=active_jobs,
        )
        return True
    return False


def _accepted_job_kinds(active_jobs: dict[str, _ActiveDaemonJob]) -> set[str] | None:
    if not active_jobs:
        return None
    if any(job.kind in EXCLUSIVE_JOB_KINDS for job in active_jobs.values()):
        return set()
    if any(job.kind not in WORKSPACE_JOB_KINDS for job in active_jobs.values()):
        return READONLY_WORKSPACE_JOB_KINDS
    return READONLY_WORKSPACE_JOB_KINDS


def _start_daemon_job(
    client: CloudClient,
    root: Path,
    *,
    machine: dict[str, Any],
    job: dict[str, Any],
    active_jobs: dict[str, _ActiveDaemonJob],
) -> None:
    job_id = str(job["id"])
    job_kind = str((job.get("job") or {}).get("kind") or "conversation_turn")
    cancel_event = threading.Event()
    thread = threading.Thread(
        target=_process_job,
        name=f"waveforward-daemon-{job_id}",
        kwargs={
            "client": client,
            "root": root,
            "machine": machine,
            "job": job,
            "cancel_event": cancel_event,
        },
        daemon=True,
    )
    active_jobs[job_id] = _ActiveDaemonJob(
        id=job_id,
        kind=job_kind,
        thread=thread,
        cancel_event=cancel_event,
        started_at=time.time(),
    )
    thread.start()


def _refresh_active_jobs(
    client: CloudClient,
    active_jobs: dict[str, _ActiveDaemonJob],
    *,
    machine_id: str,
) -> None:
    finished = [
        job_id for job_id, active in active_jobs.items() if not active.thread.is_alive()
    ]
    for job_id in finished:
        active_jobs.pop(job_id, None)
    for job_id, active in list(active_jobs.items()):
        try:
            result = client.post(
                f"/api/daemon/jobs/{job_id}/status",
                {"machine_id": machine_id},
            )
        except AgentSyncError:
            continue
        run = result.get("run") if isinstance(result, dict) else None
        if isinstance(run, dict) and run.get("status") == "canceled":
            active.cancel_event.set()


def _register(
    client: CloudClient,
    root: Path,
    daemon_state: dict[str, Any],
    *,
    config: DaemonConfig,
) -> dict[str, Any]:
    machine_id = str(daemon_state["machine_id"])
    status = service_status(root)
    result = client.post(
        "/api/daemon/register",
        {
            "machine_id": machine_id,
            "name": status["machine"],
            "workspace": status["workspace"],
            "agents": agent_capabilities(),
            "daemon": _daemon_runtime_payload(
                client,
                root,
                daemon_state,
                config=config,
            ),
        },
    )
    machine_token = str(result.get("machine_token") or "").strip()
    if machine_token:
        daemon_state["machine_token"] = machine_token
        _save_daemon_state(root, daemon_state)
        client.set_bearer_token(machine_token)
    return result["machine"]


def _process_job(
    client: CloudClient,
    root: Path,
    *,
    machine: dict[str, Any],
    job: dict[str, Any],
    cancel_event: threading.Event | None = None,
) -> None:
    job_id = job["id"]
    agent = str(job["agent"])
    model = str(job.get("model") or "")
    reasoning_effort = str(job.get("reasoning_effort") or "")
    machine_name = str(machine.get("name") or job.get("machine") or "daemon")
    job_payload = dict(job.get("job") or {})
    model = model or str(job_payload.get("model") or "")
    reasoning_effort = reasoning_effort or str(
        job_payload.get("reasoning_effort") or ""
    )
    job_kind = str(job_payload.get("kind") or "conversation_turn")
    cancel_check = cancel_event.is_set if cancel_event is not None else None
    if cancel_check is not None and cancel_check():
        _post_canceled_completion(client, machine, job_id)
        return
    if job_kind == "daemon_update":
        _process_daemon_update_job(
            client,
            root,
            machine=machine,
            job=job,
            payload=job_payload,
            config_manifest=str(job_payload.get("manifest_url") or ""),
            cancel_event=cancel_event,
        )
        return
    if job_kind == "session_import_scan":
        _process_session_import_scan(
            client,
            root,
            machine=machine,
            job=job,
            payload=job_payload,
        )
        return
    if job_kind == "session_import_import":
        _process_session_import_import(
            client,
            root,
            machine=machine,
            job=job,
            payload=job_payload,
            machine_name=machine_name,
        )
        return
    if job_kind.startswith("workspace_"):
        _process_workspace_file_job(
            client,
            root,
            machine=machine,
            job=job,
            payload=job_payload,
            cancel_event=cancel_event,
        )
        return

    conversation = job["conversation"]
    save_conversation(root, conversation)

    def post_output(chunk: str) -> None:
        try:
            client.post(
                f"/api/daemon/jobs/{job_id}/output",
                {"machine_id": machine["id"], "output": chunk},
            )
        except AgentSyncError:
            return

    try:
        turn = complete_conversation_turn(
            root,
            conversation["id"],
            agent=agent,
            machine=machine_name,
            model=model,
            reasoning_effort=reasoning_effort,
            execute_agent=bool(job.get("execute_agent", True)),
            on_output=post_output,
            cancel_check=cancel_check,
        )
    except Exception as error:
        _post_job_completion(
            client,
            f"/api/daemon/jobs/{job_id}/complete",
            {
                "machine_id": machine["id"],
                "error": str(error),
            },
        )
        return

    _post_job_completion(
        client,
        f"/api/daemon/jobs/{job_id}/complete",
        {
            "machine_id": machine["id"],
            "conversation": turn.conversation,
            "agent_run": _agent_run_payload(turn.agent_run),
        },
    )


def _post_canceled_completion(
    client: CloudClient,
    machine: dict[str, Any],
    job_id: str,
) -> None:
    _post_job_completion(
        client,
        f"/api/daemon/jobs/{job_id}/complete",
        {"machine_id": machine["id"], "error": "Run canceled."},
    )


def _process_session_import_scan(
    client: CloudClient,
    root: Path,
    *,
    machine: dict[str, Any],
    job: dict[str, Any],
    payload: dict[str, Any],
) -> None:
    job_id = job["id"]
    try:
        candidates = discover_agent_sessions(
            limit=_payload_positive_int(
                payload.get("limit"),
                default=DEFAULT_HISTORY_LIMIT,
            ),
            sources=list(payload.get("sources") or []),
        )
    except Exception as error:
        _post_job_completion(
            client,
            f"/api/daemon/jobs/{job_id}/complete",
            {"machine_id": machine["id"], "error": str(error)},
        )
        return
    _post_job_completion(
        client,
        f"/api/daemon/jobs/{job_id}/complete",
        {
            "machine_id": machine["id"],
            "agent_run": {
                "agent": "session-import",
                "returncode": 0,
                "output": f"Found {len(candidates)} importable sessions.",
                "candidates": candidates,
            },
        },
    )


def _process_session_import_import(
    client: CloudClient,
    root: Path,
    *,
    machine: dict[str, Any],
    job: dict[str, Any],
    payload: dict[str, Any],
    machine_name: str,
) -> None:
    job_id = job["id"]
    candidate_ids = [
        str(item).strip()
        for item in payload.get("candidate_ids") or []
        if str(item).strip()
    ]
    try:
        conversations = import_agent_sessions(
            root,
            candidate_ids,
            machine=machine_name,
        )
    except Exception as error:
        _post_job_completion(
            client,
            f"/api/daemon/jobs/{job_id}/complete",
            {"machine_id": machine["id"], "error": str(error)},
        )
        return
    _post_job_completion(
        client,
        f"/api/daemon/jobs/{job_id}/complete",
        {
            "machine_id": machine["id"],
            "imported_conversations": conversations,
            "agent_run": {
                "agent": "session-import",
                "returncode": 0,
                "output": f"Imported {len(conversations)} sessions.",
                "imported": [
                    {
                        "id": item["id"],
                        "title": item["title"],
                        "message_count": len(item.get("messages") or []),
                    }
                    for item in conversations
                ],
            },
        },
    )


def _process_workspace_file_job(
    client: CloudClient,
    root: Path,
    *,
    machine: dict[str, Any],
    job: dict[str, Any],
    payload: dict[str, Any],
    cancel_event: threading.Event | None = None,
) -> None:
    job_id = job["id"]
    job_kind = str(payload.get("kind") or "")
    path = str(payload.get("path") or "")
    try:
        if cancel_event is not None and cancel_event.is_set():
            raise AgentSyncError("Run canceled.")
        if job_kind == "workspace_tree":
            result_key = "tree"
            result = list_workspace_tree(root, path=path)
            output = f"Listed {len(result.get('entries') or [])} workspace entries."
        elif job_kind == "workspace_file_read":
            result_key = "file"
            result = read_workspace_file(root, path=path)
            output = f"Opened {result['path']}."
        elif job_kind == "workspace_file_write":
            result_key = "file"
            result = write_workspace_file(
                root,
                path=path,
                content=str(payload.get("content") or ""),
                base_sha256=_payload_optional_string(payload.get("base_sha256")),
                create=bool(payload.get("create", False)),
            )
            output = f"Saved {result['path']}."
        elif job_kind == "workspace_file_diff":
            result_key = "diff"
            result = workspace_file_diff(root, path=path)
            output = f"Prepared diff for {result['path']}."
        else:
            raise AgentSyncError("Unknown workspace file job.")
    except Exception as error:
        _post_job_completion(
            client,
            f"/api/daemon/jobs/{job_id}/complete",
            {"machine_id": machine["id"], "error": str(error)},
        )
        return

    _post_job_completion(
        client,
        f"/api/daemon/jobs/{job_id}/complete",
        {
            "machine_id": machine["id"],
            "agent_run": {
                "agent": "workspace-files",
                "returncode": 0,
                "output": output,
                result_key: result,
            },
        },
    )


def _process_daemon_update_job(
    client: CloudClient,
    root: Path,
    *,
    machine: dict[str, Any],
    job: dict[str, Any],
    payload: dict[str, Any],
    config_manifest: str,
    cancel_event: threading.Event | None = None,
) -> None:
    job_id = job["id"]
    manifest = config_manifest or str(payload.get("manifest") or "")
    if not manifest:
        manifest = PUBLIC_RELEASE_MANIFEST_URL
    try:
        if cancel_event is not None and cancel_event.is_set():
            raise AgentSyncError("Run canceled.")
        with tempfile.TemporaryDirectory(prefix="waveforward-update-") as tmp:
            wheel = download_update_wheel(manifest, tmp)
            client.post(
                f"/api/daemon/jobs/{job_id}/output",
                {"machine_id": machine["id"], "output": f"Installing {wheel.name}\n"},
            )
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "--upgrade", str(wheel)],
                cwd=root,
                check=False,
                capture_output=True,
                text=True,
            )
        output = "\n".join(
            item for item in (result.stdout, result.stderr) if item.strip()
        ).strip()
        if result.returncode != 0:
            raise AgentSyncError(
                f"Daemon update failed with exit code {result.returncode}.\n{output}"
            )
        _post_job_completion(
            client,
            f"/api/daemon/jobs/{job_id}/complete",
            {
                "machine_id": machine["id"],
                "agent_run": {
                    "agent": "daemon-update",
                    "returncode": result.returncode,
                    "output": output or "Daemon update installed; restarting.",
                },
            },
        )
    except Exception as error:
        _post_job_completion(
            client,
            f"/api/daemon/jobs/{job_id}/complete",
            {"machine_id": machine["id"], "error": str(error)},
        )
        return
    _restart_daemon_process()


def _restart_daemon_process() -> None:
    args = [sys.executable, "-m", "waveforward.cli", *sys.argv[1:]]
    os.execv(sys.executable, args)


def _post_job_completion(
    client: CloudClient,
    path: str,
    payload: dict[str, Any],
    *,
    attempts: int = 12,
) -> None:
    last_error: AgentSyncError | None = None
    for attempt in range(max(attempts, 1)):
        try:
            client.post(path, payload)
            return
        except AgentSyncError as error:
            last_error = error
            if attempt >= attempts - 1:
                break
            time.sleep(min(1.5 * (attempt + 1), 10.0))
    if last_error is not None:
        raise last_error


def _agent_run_payload(item: AgentRunResult | None) -> dict[str, Any] | None:
    if item is None:
        return None
    return {
        "agent": item.agent,
        "command": list(item.command),
        "returncode": item.returncode,
        "output": item.output,
        "model": item.model,
        "reasoning_effort": item.reasoning_effort,
    }


def _daemon_runtime_payload(
    client: CloudClient,
    root: Path,
    daemon_state: dict[str, Any],
    *,
    config: DaemonConfig,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "capabilities": ["session_import", "workspace_files"],
        "platform": platform.platform(),
        "python": platform.python_version(),
        "version": __version__,
    }
    payload["update"] = _daemon_update_payload(
        client,
        root,
        daemon_state,
        config=config,
    )
    return payload


def _daemon_update_payload(
    client: CloudClient,
    root: Path,
    daemon_state: dict[str, Any],
    *,
    config: DaemonConfig,
) -> dict[str, Any]:
    manifest = _daemon_update_manifest_url(client, config)
    if not manifest:
        return {
            "checked_at": "",
            "configured": False,
            "current_version": __version__,
            "reason": "update manifest is not configured",
            "update_available": False,
            "verified": False,
        }

    now = time.time()
    cached = daemon_state.get("update")
    last_checked = float(daemon_state.get("update_checked_monotonic") or 0.0)
    interval = max(config.update_check_interval, 1.0)
    if isinstance(cached, dict) and now - last_checked < interval:
        return dict(cached)

    headers = {}
    if client.auth_header and _same_origin(manifest, client.server):
        headers["Authorization"] = client.auth_header
    try:
        result = check_for_update(
            manifest,
            headers=headers,
            verify_asset=True,
        )
        update = {
            "checked_at": _utc_timestamp(),
            "configured": True,
            "current_commit": result.current_commit,
            "current_version": result.current_version,
            "latest_commit": result.latest_commit,
            "latest_version": result.latest_version,
            "reason": result.reason,
            "update_available": result.update_available,
            "verified": result.verified,
        }
    except AgentSyncError as error:
        update = {
            "checked_at": _utc_timestamp(),
            "configured": True,
            "current_version": __version__,
            "error": str(error),
            "reason": "update check failed",
            "update_available": False,
            "verified": False,
        }
    daemon_state["update"] = update
    daemon_state["update_checked_monotonic"] = now
    _save_daemon_state(root, daemon_state)
    return update


def _daemon_update_manifest_url(client: CloudClient, config: DaemonConfig) -> str:
    configured = (config.update_manifest_url or "").strip()
    if configured:
        return configured
    return PUBLIC_RELEASE_MANIFEST_URL


def _load_or_create_daemon_state(root: Path) -> dict[str, Any]:
    path = _daemon_state_path(root)
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = {}
    machine_id = str(data.get("machine_id") or "").strip()
    if not machine_id:
        machine_id = f"machine_{uuid.uuid4().hex[:16]}"

    state: dict[str, Any] = {"machine_id": machine_id}
    machine_token = str(data.get("machine_token") or "").strip()
    if machine_token:
        state["machine_token"] = machine_token
    if isinstance(data.get("update"), dict):
        state["update"] = data["update"]
    if data.get("update_checked_monotonic"):
        state["update_checked_monotonic"] = data["update_checked_monotonic"]
    _save_daemon_state(root, state)
    return state


def _save_daemon_state(root: Path, state: dict[str, Any]) -> None:
    path = _daemon_state_path(root)
    write_json(path, state, mode=0o600)


def _daemon_state_path(root: Path) -> Path:
    return sync_dir(root) / "daemon.json"


def _daemon_pid_path(root: Path) -> Path:
    return sync_dir(root) / "daemon.pid"


def _daemon_log_path(root: Path) -> Path:
    return sync_dir(root) / "daemon.log"


def _daemon_process_command(config: DaemonConfig, *, python: str) -> list[str]:
    command = [
        python,
        "-m",
        "waveforward.cli",
        "daemon",
        "--server",
        config.server,
    ]
    if config.auth_user:
        command.extend(["--auth-user", config.auth_user])
    if config.auth_password:
        command.extend(["--auth-password", config.auth_password])
    if config.auth_token:
        command.extend(["--auth-token", config.auth_token])
    if config.machine_name:
        command.extend(["--machine", config.machine_name])
    command.extend(["--poll-interval", str(config.poll_interval)])
    return command


def _read_daemon_pid(path: Path) -> int:
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _write_daemon_pid(path: Path, pid: int) -> None:
    atomic_write_text(path, f"{pid}\n", mode=0o600)


def _pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _build_auth_header(config: DaemonConfig) -> str | None:
    if config.auth_token:
        return f"Bearer {config.auth_token}"
    if config.auth_user and config.auth_password:
        token = base64.b64encode(
            f"{config.auth_user}:{config.auth_password}".encode()
        ).decode("ascii")
        return f"Basic {token}"
    return None


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise AgentSyncError(f"Missing required environment variable: {name}")
    return value


def _env_positive_int(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError as error:
        raise AgentSyncError(f"{name} must be an integer.") from error
    return max(parsed, 1)


def _payload_positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(parsed, 1)


def _payload_optional_string(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _same_origin(left: str, right: str) -> bool:
    left_url = urlparse(left)
    right_url = urlparse(right)
    return bool(
        left_url.scheme
        and right_url.scheme
        and left_url.scheme == right_url.scheme
        and left_url.netloc == right_url.netloc
    )


def _request_retry_delay(attempt: int) -> float:
    return min(0.25 * (attempt + 1), 1.0)


def _utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _log_daemon_warning(error: AgentSyncError) -> None:
    print(f"warning: {error}; retrying", file=sys.stderr, flush=True)
