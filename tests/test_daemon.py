"""Tests for the outbound WaveForward daemon."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.error import URLError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waveforward.daemon import (  # noqa: E402
    CloudClient,
    DaemonConfig,
    _daemon_update_payload,
    _load_or_create_daemon_state,
    _save_daemon_state,
    daemon_status,
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


if __name__ == "__main__":
    unittest.main()
