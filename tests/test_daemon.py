"""Tests for the outbound WaveForward daemon."""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waveforward.daemon import (  # noqa: E402
    UNSAFE_AGENT_EXECUTION_ENV,
    CloudClient,
    DaemonConfig,
    _daemon_update_payload,
    _load_or_create_daemon_state,
    _post_job_completion,
    _save_daemon_state,
    daemon_status,
    start_daemon_process,
)


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
            (root / ".waveforward").mkdir()

            state = _load_or_create_daemon_state(root)
            state["machine_token"] = "wfm_saved"
            _save_daemon_state(root, state)
            loaded = _load_or_create_daemon_state(root)

            self.assertEqual(loaded["machine_id"], state["machine_id"])
            self.assertEqual(loaded["machine_token"], "wfm_saved")

    def test_daemon_status_does_not_expose_machine_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".waveforward").mkdir()
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
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            config = DaemonConfig(
                server="https://app.example.test",
                auth_token="setup-secret",
                machine_name="Laptop",
            )
            with patch(
                "waveforward.daemon.Popen",
                return_value=_FakeProcess(),
            ) as popen:
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
            pid_path = root / ".waveforward" / "daemon.pid"
            self.assertEqual(pid_path.read_text(encoding="utf-8"), "4242\n")
            self.assertNotIn("setup-secret", pid_path.read_text(encoding="utf-8"))

    def test_start_daemon_process_reuses_running_daemon(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
            (root / ".waveforward").mkdir()
            (root / ".waveforward" / "daemon.pid").write_text(
                "4242\n",
                encoding="utf-8",
            )
            with (
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
