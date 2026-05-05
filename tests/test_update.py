from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waveforward.cli import main  # noqa: E402
from waveforward.core import AgentSyncError  # noqa: E402
from waveforward.update import (  # noqa: E402
    check_for_update,
    download_update_wheel,
    load_update_manifest,
)


class WaveForwardUpdateTests(unittest.TestCase):
    def test_update_check_verifies_local_manifest_asset(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            wheel = root / "waveforward-0.2.0-py3-none-any.whl"
            wheel.write_bytes(b"alpha wheel")
            manifest_path = _write_manifest(
                root,
                version="0.2.0",
                commit="newer",
                wheel_url=wheel.name,
                wheel_sha256=_sha256(wheel.read_bytes()),
            )

            result = check_for_update(
                manifest_path,
                current_version="0.1.0",
                current_commit="older",
                verify_asset=True,
            )

            self.assertTrue(result.update_available)
            self.assertTrue(result.verified)
            self.assertEqual(result.reason, "latest version is newer")
            self.assertEqual(result.wheel_url, str(wheel))

    def test_update_check_detects_same_version_release_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            wheel = root / "waveforward-0.1.0-py3-none-any.whl"
            wheel.write_bytes(b"alpha wheel")
            manifest_path = _write_manifest(
                root,
                version="0.1.0",
                commit="newer",
                wheel_url=wheel.name,
                wheel_sha256=_sha256(wheel.read_bytes()),
            )

            result = check_for_update(
                manifest_path,
                current_version="0.1.0",
                current_commit="older",
            )

            self.assertTrue(result.update_available)
            self.assertEqual(
                result.reason,
                "same version has a different release commit",
            )

    def test_update_check_rejects_asset_checksum_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            wheel = root / "waveforward-0.2.0-py3-none-any.whl"
            wheel.write_bytes(b"changed")
            manifest_path = _write_manifest(
                root,
                version="0.2.0",
                commit="newer",
                wheel_url=wheel.name,
                wheel_sha256="0" * 64,
            )

            with self.assertRaisesRegex(AgentSyncError, "checksum mismatch"):
                check_for_update(manifest_path, verify_asset=True)

    def test_update_manifest_validation_rejects_unknown_format(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "manifest.json"
            path.write_text(
                json.dumps(
                    {
                        "format": "other",
                        "format_version": 1,
                        "version": "0.2.0",
                        "wheel": {"url": "x.whl", "sha256": "0" * 64},
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(AgentSyncError, "format"):
                load_update_manifest(path)

    def test_download_update_wheel_verifies_and_copies_asset(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            wheel = root / "waveforward-0.2.0-py3-none-any.whl"
            wheel.write_bytes(b"alpha wheel")
            manifest_path = _write_manifest(
                root,
                version="0.2.0",
                commit="newer",
                wheel_url=wheel.name,
                wheel_sha256=_sha256(wheel.read_bytes()),
            )
            output_dir = root / "downloads"
            output_dir.mkdir()

            downloaded = download_update_wheel(manifest_path, output_dir)

            self.assertEqual(downloaded.name, wheel.name)
            self.assertEqual(downloaded.read_bytes(), b"alpha wheel")

    def test_update_check_cli_json_and_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            wheel = root / "waveforward-0.2.0-py3-none-any.whl"
            wheel.write_bytes(b"alpha wheel")
            manifest_path = _write_manifest(
                root,
                version="0.2.0",
                commit="newer",
                wheel_url=wheel.name,
                wheel_sha256=_sha256(wheel.read_bytes()),
            )
            output = StringIO()

            with redirect_stdout(output):
                code = main(
                    [
                        "update-check",
                        str(manifest_path),
                        "--current-version",
                        "0.1.0",
                        "--json",
                        "--exit-code",
                    ]
                )

            self.assertEqual(code, 2)
            data = json.loads(output.getvalue())
            self.assertTrue(data["update_available"])
            self.assertEqual(data["latest_version"], "0.2.0")

    def test_update_install_cli_dry_run_reports_verified_update(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            wheel = root / "waveforward-0.2.0-py3-none-any.whl"
            wheel.write_bytes(b"alpha wheel")
            manifest_path = _write_manifest(
                root,
                version="0.2.0",
                commit="newer",
                wheel_url=wheel.name,
                wheel_sha256=_sha256(wheel.read_bytes()),
            )
            output = StringIO()

            with redirect_stdout(output):
                code = main(
                    [
                        "update-install",
                        str(manifest_path),
                        "--current-version",
                        "0.1.0",
                        "--json",
                    ]
                )

            self.assertEqual(code, 0)
            data = json.loads(output.getvalue())
            self.assertFalse(data["applied"])
            self.assertTrue(data["verified"])
            self.assertTrue(data["would_install"])

    def test_update_install_cli_apply_uses_verified_wheel(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            wheel = root / "waveforward-0.2.0-py3-none-any.whl"
            wheel.write_bytes(b"alpha wheel")
            manifest_path = _write_manifest(
                root,
                version="0.2.0",
                commit="newer",
                wheel_url=wheel.name,
                wheel_sha256=_sha256(wheel.read_bytes()),
            )
            installed: dict[str, object] = {}

            def fake_install(python: str, wheel_path: Path) -> None:
                installed["python"] = python
                installed["wheel_name"] = wheel_path.name
                installed["content"] = wheel_path.read_bytes()

            output = StringIO()
            with (
                patch("waveforward.cli._pip_install_update", side_effect=fake_install),
                redirect_stdout(output),
            ):
                code = main(
                    [
                        "update-install",
                        str(manifest_path),
                        "--current-version",
                        "0.1.0",
                        "--python",
                        "/opt/waveforward/bin/python",
                        "--apply",
                        "--json",
                    ]
                )

            self.assertEqual(code, 0)
            data = json.loads(output.getvalue())
            self.assertTrue(data["applied"])
            self.assertEqual(installed["python"], "/opt/waveforward/bin/python")
            self.assertEqual(installed["wheel_name"], wheel.name)
            self.assertEqual(installed["content"], b"alpha wheel")


def _write_manifest(
    root: Path,
    *,
    version: str,
    commit: str,
    wheel_url: str,
    wheel_sha256: str,
) -> Path:
    path = root / "waveforward-alpha-manifest.json"
    path.write_text(
        json.dumps(
            {
                "format": "waveforward.alpha_manifest",
                "format_version": 1,
                "version": version,
                "commit": commit,
                "wheel": {
                    "url": wheel_url,
                    "sha256": wheel_sha256,
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


if __name__ == "__main__":
    unittest.main()
