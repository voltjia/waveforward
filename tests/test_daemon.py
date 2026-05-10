"""Tests for the outbound WaveForward daemon."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waveforward.daemon import (  # noqa: E402
    PUBLIC_RELEASE_MANIFEST_URL,
    READONLY_WORKSPACE_JOB_KINDS,
    UNSAFE_AGENT_EXECUTION_ENV,
    CloudClient,
    DaemonConfig,
    _accepted_job_kinds,
    _ActiveDaemonJob,
    _daemon_runtime_payload,
    _daemon_update_manifest_url,
    _daemon_update_payload,
    _daemon_workspaces,
    _job_workspace_root,
    _load_or_create_daemon_state,
    _poll_once,
    _post_job_completion,
    _process_job,
    _refresh_active_jobs,
    _save_daemon_state,
    daemon_status,
    start_daemon_process,
)
from waveforward.files import read_workspace_file  # noqa: E402


class _FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class _TimeoutResponse:
    def read(self) -> bytes:
        raise TimeoutError("response read timed out")


class _FlakyOpener:
    def __init__(self) -> None:
        self.calls = 0
        self.requests = []

    def open(self, request, timeout: float):
        self.calls += 1
        self.requests.append((request, timeout))
        if self.calls == 1:
            raise URLError("temporary TLS EOF")
        return _FakeResponse({"ok": True, "value": 42})


class _ReadTimeoutOpener:
    def __init__(self) -> None:
        self.calls = 0

    def open(self, _request, timeout: float):
        self.calls += 1
        if self.calls == 1:
            return _TimeoutResponse()
        return _FakeResponse({"ok": True, "value": 7})


class _FakeProcess:
    pid = 4242


class _CompletionClient:
    def __init__(self, failures: int) -> None:
        self.calls = 0
        self.failures = failures

    def post(self, _path: str, _payload: dict[str, object]) -> dict[str, object]:
        self.calls += 1
        if self.calls <= self.failures:
            from waveforward.core import AgentSyncError

            raise AgentSyncError("Cloud request failed: 502")
        return {"ok": True}


class _RecordingClient:
    auth_header = "Bearer machine-token"
    server = "https://app.example.test/"

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, object]]] = []

    def post(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        self.posts.append((path, payload))
        return {"ok": True}


class _PollingClient:
    auth_header = "Bearer machine-token"
    server = "https://app.example.test/"

    def __init__(self, jobs: list[dict[str, object]]) -> None:
        self.jobs = list(jobs)
        self.next_payloads: list[dict[str, object]] = []

    def post(self, path: str, payload: dict[str, object]) -> dict[str, object]:
        if path == "/api/daemon/register":
            return {
                "machine": {
                    "id": payload["machine_id"],
                    "name": "Remote machine",
                }
            }
        if path == "/api/daemon/jobs/next":
            self.next_payloads.append(dict(payload))
            job = self.jobs.pop(0) if self.jobs else None
            return {"job": job}
        if path.endswith("/status"):
            return {"run": {"status": "running"}}
        return {"ok": True}


class DaemonClientTests(unittest.TestCase):
    def test_cloud_client_retries_transient_url_errors(self) -> None:
        client = CloudClient(
            DaemonConfig(server="https://example.test", request_retries=2)
        )
        opener = _FlakyOpener()
        client.opener = opener

        result = client.post("/api/daemon/test", {"hello": "world"})

        self.assertEqual(result["value"], 42)
        self.assertEqual(opener.calls, 2)
        self.assertEqual(opener.requests[0][0].get_header("Connection"), "close")

    def test_cloud_client_retries_response_read_timeouts(self) -> None:
        client = CloudClient(
            DaemonConfig(server="https://example.test", request_retries=2)
        )
        opener = _ReadTimeoutOpener()
        client.opener = opener

        result = client.post("/api/daemon/test", {})

        self.assertEqual(result["value"], 7)
        self.assertEqual(opener.calls, 2)

    def test_cloud_client_can_rotate_to_machine_bearer_token(self) -> None:
        client = CloudClient(DaemonConfig(server="https://example.test"))
        client.set_bearer_token("wfm_machine")
        opener = _FlakyOpener()
        opener.calls = 1
        client.opener = opener

        client.post("/api/daemon/test", {})

        self.assertEqual(
            opener.requests[0][0].get_header("Authorization"),
            "Bearer wfm_machine",
        )

    def test_daemon_state_persists_machine_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"

            with patch.dict("os.environ", {"WAVEFORWARD_HOME": str(home)}):
                state = _load_or_create_daemon_state(root)
                state["machine_token"] = "wfm_saved"
                _save_daemon_state(root, state)
                loaded = _load_or_create_daemon_state(root)

            self.assertEqual(loaded["machine_id"], state["machine_id"])
            self.assertEqual(loaded["machine_token"], "wfm_saved")
            self.assertTrue((home / "daemon.json").exists())

    def test_daemon_status_does_not_expose_machine_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            with patch.dict("os.environ", {"WAVEFORWARD_HOME": str(home)}):
                _save_daemon_state(
                    root,
                    {"machine_id": "machine_alpha", "machine_token": "wfm_secret"},
                )

                status = daemon_status(root)

            self.assertTrue(status["configured"])
            self.assertTrue(status["has_machine_token"])
            self.assertEqual(status["machine_id"], "machine_alpha")
            self.assertNotIn("wfm_secret", str(status))

    def test_start_daemon_process_detaches_and_sets_execution_opt_in(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            config = DaemonConfig(
                server="https://app.example.test",
                auth_token="setup-secret",
                machine_name="Laptop",
                update_manifest_url="https://example.test/manifest.json",
            )
            with (
                patch.dict("os.environ", {"WAVEFORWARD_HOME": str(home)}),
                patch(
                    "waveforward.daemon.Popen",
                    return_value=_FakeProcess(),
                ) as popen,
            ):
                result = start_daemon_process(
                    root,
                    config=config,
                    allow_agent_execution=True,
                    python="/usr/bin/python3",
                )

            command = popen.call_args.args[0]
            kwargs = popen.call_args.kwargs
            self.assertTrue(result["started"])
            self.assertEqual(result["pid"], 4242)
            self.assertEqual(
                command[:4], ["/usr/bin/python3", "-m", "waveforward.cli", "daemon"]
            )
            self.assertIn("--auth-token", command)
            self.assertIn("setup-secret", command)
            self.assertEqual(kwargs["cwd"], root.resolve())
            self.assertEqual(kwargs["env"][UNSAFE_AGENT_EXECUTION_ENV], "1")
            self.assertEqual(
                kwargs["env"]["WAVEFORWARD_DAEMON_UPDATE_MANIFEST_URL"],
                "https://example.test/manifest.json",
            )
            pid_path = home / "daemon.pid"
            self.assertEqual(pid_path.read_text(encoding="utf-8"), "4242\n")
            self.assertNotIn("setup-secret", pid_path.read_text(encoding="utf-8"))
            self.assertFalse((root / ".waveforward").exists())

    def test_start_daemon_process_reuses_running_daemon(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            home.mkdir()
            (home / "daemon.pid").write_text(
                "4242\n",
                encoding="utf-8",
            )
            with (
                patch.dict("os.environ", {"WAVEFORWARD_HOME": str(home)}),
                patch("waveforward.daemon._pid_running", return_value=True),
                patch("waveforward.daemon.Popen") as popen,
            ):
                result = start_daemon_process(
                    root,
                    config=DaemonConfig(server="https://app.example.test"),
                )

            self.assertFalse(result["started"])
            self.assertTrue(result["already_running"])
            self.assertEqual(result["pid"], 4242)
            popen.assert_not_called()

    def test_daemon_update_payload_checks_manifest_and_caches_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".waveforward").mkdir()
            wheel = root / "waveforward-0.2.0-py3-none-any.whl"
            wheel.write_bytes(b"alpha wheel")
            manifest = root / "waveforward-alpha-manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "commit": "newer",
                        "format": "waveforward.alpha_manifest",
                        "format_version": 1,
                        "version": "0.2.0",
                        "wheel": {
                            "sha256": (
                                "d15f65f57bc48a48cc57b4940637e934"
                                "f25fdeccfc04df967818d2cd1d8e2acf"
                            ),
                            "url": wheel.name,
                        },
                    }
                ),
                encoding="utf-8",
            )
            state = {"machine_id": "machine_alpha"}
            client = CloudClient(DaemonConfig(server="https://example.test"))
            config = DaemonConfig(
                server="https://example.test",
                update_check_interval=3600,
                update_manifest_url=str(manifest),
            )

            result = _daemon_update_payload(client, root, state, config=config)
            manifest.unlink()
            cached = _daemon_update_payload(client, root, state, config=config)

            self.assertTrue(result["configured"])
            self.assertTrue(result["verified"])
            self.assertTrue(result["update_available"])
            self.assertEqual(result["latest_version"], "0.2.0")
            self.assertEqual(cached["latest_version"], "0.2.0")

    def test_daemon_update_payload_reports_missing_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".waveforward").mkdir()
            state = {"machine_id": "machine_alpha"}
            client = CloudClient(DaemonConfig(server="https://example.test"))

            result = _daemon_update_payload(
                client,
                root,
                state,
                config=DaemonConfig(
                    server="https://example.test",
                    update_manifest_url=str(root / "missing.json"),
                ),
            )

            self.assertTrue(result["configured"])
            self.assertFalse(result["verified"])
            self.assertEqual(result["reason"], "update check failed")

    def test_daemon_update_defaults_to_public_manifest(self) -> None:
        client = CloudClient(DaemonConfig(server="https://app.example.test"))

        manifest = _daemon_update_manifest_url(
            client,
            DaemonConfig(server="https://app.example.test"),
        )

        self.assertEqual(manifest, PUBLIC_RELEASE_MANIFEST_URL)

    def test_daemon_runtime_reports_capabilities(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            client = CloudClient(DaemonConfig(server="https://app.example.test"))
            config = DaemonConfig(server="https://app.example.test")

            with patch("waveforward.daemon.check_for_update") as check:
                check.return_value.current_commit = ""
                check.return_value.current_version = "0.1.2"
                check.return_value.latest_commit = ""
                check.return_value.latest_version = "0.1.2"
                check.return_value.reason = "current"
                check.return_value.update_available = False
                check.return_value.verified = True
                payload = _daemon_runtime_payload(
                    client,
                    root,
                    {"machine_id": "machine_alpha"},
                    config=config,
                )

            self.assertEqual(
                payload["capabilities"],
                [
                    "home_config",
                    "multi_workspace",
                    "session_import",
                    "workspace_files",
                ],
            )
            self.assertEqual(check.call_args.kwargs["headers"], {})

    def test_daemon_workspaces_deduplicates_and_labels_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            nested = root / "nested"
            nested.mkdir()
            config = DaemonConfig(
                server="https://app.example.test",
                workspaces=(str(root), str(nested), str(root)),
            )

            workspaces = _daemon_workspaces(root, config)

            self.assertEqual(
                [item["path"] for item in workspaces],
                [str(root), str(nested)],
            )
            self.assertEqual(workspaces[0]["name"], root.name)
            self.assertTrue(workspaces[0]["active"])

    def test_job_workspace_root_rejects_unregistered_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            allowed = root / "allowed"
            denied = root / "denied"
            allowed.mkdir()
            denied.mkdir()

            with self.assertRaisesRegex(Exception, "Workspace is not registered"):
                _job_workspace_root(
                    root,
                    {"workspaces": [{"path": str(allowed.resolve())}]},
                    {"workspace": str(denied.resolve())},
                )

    def test_workspace_file_job_reads_remote_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "remote.py").write_text("print('remote')\n", encoding="utf-8")
            client = _RecordingClient()
            machine = {"id": "machine-test", "name": "Remote machine"}
            job = {
                "agent": "codex",
                "id": "run-file",
                "job": {"kind": "workspace_file_read", "path": "remote.py"},
            }

            _process_job(client, root, machine=machine, job=job)

            path, payload = client.posts[-1]
            self.assertEqual(path, "/api/daemon/jobs/run-file/complete")
            self.assertEqual(payload["agent_run"]["agent"], "workspace-files")
            self.assertEqual(
                payload["agent_run"]["file"]["content"],
                "print('remote')\n",
            )

    def test_workspace_file_job_writes_remote_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "remote.py"
            target.write_text("print('before')\n", encoding="utf-8")
            file_payload = read_workspace_file(root, path="remote.py")
            client = _RecordingClient()
            machine = {"id": "machine-test", "name": "Remote machine"}
            job = {
                "agent": "codex",
                "id": "run-file",
                "job": {
                    "base_sha256": file_payload["sha256"],
                    "content": "print('after')\n",
                    "kind": "workspace_file_write",
                    "path": "remote.py",
                },
            }

            _process_job(client, root, machine=machine, job=job)

            self.assertEqual(target.read_text(encoding="utf-8"), "print('after')\n")
            _path, payload = client.posts[-1]
            self.assertEqual(
                payload["agent_run"]["file"]["content"], "print('after')\n"
            )

    def test_session_import_scan_job_posts_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            client = _RecordingClient()
            machine = {"id": "machine-test", "name": "Remote machine"}
            job = {
                "agent": "codex",
                "id": "run-import",
                "job": {"kind": "session_import_scan", "limit": 10},
            }

            with patch(
                "waveforward.daemon.discover_agent_sessions",
                return_value=[{"id": "codex-1", "title": "Imported"}],
            ):
                _process_job(client, Path(tmp), machine=machine, job=job)

            _path, payload = client.posts[-1]
            self.assertEqual(payload["agent_run"]["agent"], "session-import")
            self.assertEqual(payload["agent_run"]["candidates"][0]["id"], "codex-1")

    def test_poll_once_keeps_control_loop_available_during_job(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".waveforward").mkdir()
            state = {"machine_id": "machine-alpha"}
            active_jobs: dict[str, _ActiveDaemonJob] = {}
            started = threading.Event()
            release = threading.Event()
            job = {
                "agent": "codex",
                "id": "run-agent",
                "job": {"kind": "conversation_turn"},
            }
            client = _PollingClient([job])

            def process_job(**_kwargs: object) -> None:
                started.set()
                release.wait(timeout=5)

            with (
                patch("waveforward.daemon._daemon_runtime_payload", return_value={}),
                patch("waveforward.daemon._process_job", side_effect=process_job),
            ):
                claimed = _poll_once(
                    client,
                    root,
                    state,
                    config=DaemonConfig(server="https://app.example.test"),
                    active_jobs=active_jobs,
                )
                self.assertTrue(started.wait(timeout=2))
                self.assertTrue(claimed)
                self.assertIn("run-agent", active_jobs)

                _poll_once(
                    client,
                    root,
                    state,
                    config=DaemonConfig(server="https://app.example.test"),
                    active_jobs=active_jobs,
                )

            self.assertEqual(
                client.next_payloads[-1]["accepted_kinds"],
                sorted(READONLY_WORKSPACE_JOB_KINDS),
            )
            release.set()
            active_jobs["run-agent"].thread.join(timeout=2)

    def test_refresh_active_jobs_observes_remote_cancel(self) -> None:
        stop = threading.Event()
        cancel_event = threading.Event()

        def wait_forever() -> None:
            stop.wait(timeout=5)

        thread = threading.Thread(target=wait_forever)
        thread.start()
        try:
            active_jobs = {
                "run-cancel": _ActiveDaemonJob(
                    id="run-cancel",
                    kind="conversation_turn",
                    thread=thread,
                    cancel_event=cancel_event,
                    started_at=0.0,
                )
            }

            class CancelClient:
                def post(
                    self,
                    _path: str,
                    _payload: dict[str, object],
                ) -> dict[str, object]:
                    return {"run": {"status": "canceled"}}

            _refresh_active_jobs(
                CancelClient(),
                active_jobs,
                machine_id="machine-alpha",
            )

            self.assertTrue(cancel_event.is_set())
        finally:
            stop.set()
            thread.join(timeout=2)

    def test_accepted_job_kinds_block_exclusive_jobs(self) -> None:
        thread = threading.Thread(target=lambda: None)
        active_jobs = {
            "write": _ActiveDaemonJob(
                id="write",
                kind="workspace_file_write",
                thread=thread,
                cancel_event=threading.Event(),
                started_at=0.0,
            )
        }

        self.assertEqual(_accepted_job_kinds(active_jobs), set())
        active_jobs["write"] = _ActiveDaemonJob(
            id="update",
            kind="daemon_update",
            thread=thread,
            cancel_event=threading.Event(),
            started_at=0.0,
        )
        self.assertEqual(_accepted_job_kinds(active_jobs), set())

    def test_daemon_update_job_installs_verified_wheel_and_restarts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wheel = root / "waveforward-0.2.0-py3-none-any.whl"
            wheel.write_bytes(b"wheel")
            client = _RecordingClient()
            machine = {"id": "machine-test", "name": "Remote machine"}
            job = {
                "agent": "codex",
                "id": "run-update",
                "job": {
                    "kind": "daemon_update",
                    "manifest_url": "https://example.test/manifest.json",
                },
            }

            with (
                patch("waveforward.daemon.download_update_wheel", return_value=wheel),
                patch("waveforward.daemon._restart_daemon_process") as restart,
                patch("waveforward.daemon.subprocess.run") as run,
            ):
                run.return_value = subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout="installed\n",
                    stderr="",
                )

                _process_job(client, root, machine=machine, job=job)

            command = run.call_args.args[0]
            self.assertEqual(command[:4], [sys.executable, "-m", "pip", "install"])
            self.assertIn(str(wheel), command)
            restart.assert_called_once_with()
            _path, payload = client.posts[-1]
            self.assertEqual(payload["agent_run"]["agent"], "daemon-update")
            self.assertIn("installed", payload["agent_run"]["output"])

    def test_job_completion_retries_transient_cloud_errors(self) -> None:
        client = _CompletionClient(failures=2)
        with patch("waveforward.daemon.time.sleep") as sleep:
            _post_job_completion(
                client,
                "/api/daemon/jobs/run_alpha/complete",
                {"machine_id": "machine_alpha"},
                attempts=3,
            )

        self.assertEqual(client.calls, 3)
        self.assertEqual(sleep.call_count, 2)


if __name__ == "__main__":
    unittest.main()
