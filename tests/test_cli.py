from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waveforward.cli import _daemon_config_from_args, build_parser  # noqa: E402


class CliDaemonConfigTests(unittest.TestCase):
    def test_daemon_update_manifest_can_come_from_args(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            [
                "daemon",
                "--server",
                "https://app.example.test",
                "--update-manifest-url",
                "https://example.test/manifest.json",
            ]
        )

        config = _daemon_config_from_args(args)

        self.assertEqual(
            config.update_manifest_url,
            "https://example.test/manifest.json",
        )

    def test_daemon_update_manifest_can_come_from_environment(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["daemon", "--server", "https://app.example.test"])

        with patch.dict(
            os.environ,
            {"WAVEFORWARD_DAEMON_UPDATE_MANIFEST_URL": "/tmp/manifest.json"},
        ):
            config = _daemon_config_from_args(args)

        self.assertEqual(config.update_manifest_url, "/tmp/manifest.json")


if __name__ == "__main__":
    unittest.main()
