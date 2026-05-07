from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waveforward.core import AgentSyncError, initialize_workspace  # noqa: E402
from waveforward.files import (  # noqa: E402
    list_workspace_tree,
    read_workspace_file,
    workspace_file_diff,
    write_workspace_file,
)


class WorkspaceFileTests(unittest.TestCase):
    def test_read_write_and_protect_paths(self) -> None:
        with git_repo() as root:
            source = root / "src" / "app.py"
            source.parent.mkdir()
            source.write_text("print('hello')\n", encoding="utf-8")
            (root / ".env").write_text("TOKEN=secret\n", encoding="utf-8")
            initialize_workspace(root, machine_name="files-test")

            tree = list_workspace_tree(root)
            paths = {entry["path"] for entry in tree["entries"]}
            self.assertIn(".gitignore", paths)
            self.assertIn("src", paths)
            self.assertNotIn(".git", paths)
            self.assertNotIn(".waveforward", paths)
            self.assertNotIn(".env", paths)

            file_payload = read_workspace_file(root, path="src/app.py")
            self.assertEqual(file_payload["content"], "print('hello')\n")
            self.assertEqual(
                file_payload["sha256"],
                hashlib.sha256(b"print('hello')\n").hexdigest(),
            )

            saved = write_workspace_file(
                root,
                path="src/app.py",
                content="print('saved')\n",
                base_sha256=file_payload["sha256"],
            )
            self.assertEqual(saved["content"], "print('saved')\n")
            self.assertEqual(source.read_text(encoding="utf-8"), "print('saved')\n")

            with self.assertRaisesRegex(AgentSyncError, "changed on disk"):
                write_workspace_file(
                    root,
                    path="src/app.py",
                    content="print('stale')\n",
                    base_sha256=file_payload["sha256"],
                )
            with self.assertRaisesRegex(AgentSyncError, "base_sha256"):
                write_workspace_file(
                    root,
                    path="src/app.py",
                    content="print('blind')\n",
                )
            with self.assertRaisesRegex(AgentSyncError, "relative"):
                read_workspace_file(root, path="../outside.py")
            with self.assertRaisesRegex(AgentSyncError, "not available"):
                read_workspace_file(root, path=".env")

    def test_reject_binary_and_report_diff(self) -> None:
        with git_repo() as root:
            source = root / "tracked.py"
            source.write_text("print('old')\n", encoding="utf-8")
            run(["git", "add", "tracked.py"], root)
            run(["git", "commit", "-m", "tracked"], root)
            source.write_text("print('new')\n", encoding="utf-8")
            (root / "binary.bin").write_bytes(b"\x00\x01")

            diff = workspace_file_diff(root, path="tracked.py")

            self.assertTrue(diff["available"])
            self.assertIn("-print('old')", diff["diff"])
            self.assertIn("+print('new')", diff["diff"])
            with self.assertRaisesRegex(AgentSyncError, "Binary"):
                read_workspace_file(root, path="binary.bin")


class git_repo:
    def __enter__(self) -> Path:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        run(["git", "init"], root)
        run(["git", "config", "user.name", "WaveForward Tests"], root)
        run(["git", "config", "user.email", "waveforward@example.test"], root)
        (root / ".gitignore").write_text(".waveforward/\n", encoding="utf-8")
        run(["git", "add", ".gitignore"], root)
        run(["git", "commit", "-m", "initial"], root)
        self.root = root
        return root

    def __exit__(self, *_exc: object) -> None:
        self._tmp.cleanup()


def run(args: list[str], cwd: Path) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


if __name__ == "__main__":
    unittest.main()
