from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waveforward.core import AgentSyncError  # noqa: E402
from waveforward.runner import (  # noqa: E402
    agent_capabilities,
    run_claude_code,
    run_codex,
    run_opencode,
)


class RunnerTests(unittest.TestCase):
    def test_run_claude_code_uses_print_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch(
                    "waveforward.runner.shutil.which",
                    return_value="/usr/bin/claude",
                ),
                patch("waveforward.runner.subprocess.run") as run,
            ):
                run.return_value = subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout="done\n",
                    stderr="",
                )

                result = run_claude_code(root, prompt="continue this")

            command = run.call_args.args[0]
            self.assertEqual(command[0], "claude")
            self.assertIn("--print", command)
            self.assertIn("--permission-mode", command)
            self.assertIn("acceptEdits", command)
            self.assertIn("--output-format", command)
            self.assertIn("text", command)
            self.assertEqual(result.agent, "claude-code")
            self.assertEqual(result.output, "done")

    def test_run_codex_uses_exec_in_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch("waveforward.runner.shutil.which", return_value="/usr/bin/codex"),
                patch("waveforward.runner.subprocess.run") as run,
            ):
                run.return_value = subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout="done\n",
                    stderr="",
                )

                result = run_codex(root, prompt="continue this")

            command = run.call_args.args[0]
            self.assertEqual(command[:2], ("codex", "exec"))
            self.assertIn("--dangerously-bypass-approvals-and-sandbox", command)
            self.assertIn("--cd", command)
            self.assertIn(str(root), command)
            self.assertIn("--output-last-message", command)
            self.assertEqual(result.agent, "codex")
            self.assertEqual(result.output, "done")

    def test_run_opencode_uses_free_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                patch(
                    "waveforward.runner.shutil.which",
                    return_value="/usr/bin/opencode",
                ),
                patch("waveforward.runner.subprocess.run") as run,
            ):
                run.return_value = subprocess.CompletedProcess(
                    args=[],
                    returncode=0,
                    stdout="done\n",
                    stderr="",
                )

                result = run_opencode(root, prompt="continue this")

            command = run.call_args.args[0]
            self.assertEqual(command[:2], ("opencode", "run"))
            self.assertIn("opencode/minimax-m2.5-free", command)
            self.assertEqual(result.agent, "opencode")
            self.assertEqual(result.output, "done")

    def test_agent_capabilities_report_installed_commands(self) -> None:
        def fake_which(command: str) -> str | None:
            return f"/usr/bin/{command}" if command == "codex" else None

        with patch("waveforward.runner.shutil.which", side_effect=fake_which):
            capabilities = agent_capabilities()

        available = {item["id"]: item["available"] for item in capabilities}
        self.assertTrue(available["codex"])
        self.assertFalse(available["claude-code"])
        self.assertFalse(available["opencode"])

    def test_missing_agent_command_returns_clear_error(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("waveforward.runner.shutil.which", return_value=None),
            self.assertRaisesRegex(AgentSyncError, "Codex is not installed"),
        ):
            run_codex(Path(directory), prompt="continue this")


if __name__ == "__main__":
    unittest.main()
