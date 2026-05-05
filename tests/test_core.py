from __future__ import annotations

import io
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from waveforward.core import (  # noqa: E402
    AgentSyncError,
    create_snapshot,
    export_snapshot_bundle,
    generate_handoff,
    import_snapshot_bundle,
    initialize_workspace,
    list_snapshots,
    restore_snapshot,
    run_doctor,
    verify_snapshot,
)


class AgentSyncCoreTests(unittest.TestCase):
    def test_snapshot_captures_untracked_files_and_handoff(self) -> None:
        with git_repo() as root:
            initialize_workspace(root, machine_name="desktop")
            (root / "notes.txt").write_text("continue parser work\n", encoding="utf-8")

            result = create_snapshot(
                root,
                message="parser checkpoint",
                task="Finish parser restore tests.",
            )
            handoff = generate_handoff(
                root, snapshot_ref=result.snapshot_id, target="codex"
            )

            manifest = result.path / "untracked" / "notes.txt"
            self.assertTrue(manifest.exists())
            self.assertEqual(
                manifest.read_text(encoding="utf-8"), "continue parser work\n"
            )
            self.assertIn("parser checkpoint", handoff.read_text(encoding="utf-8"))
            self.assertIn(
                "Finish parser restore tests.", handoff.read_text(encoding="utf-8")
            )

    def test_restore_copies_captured_untracked_files(self) -> None:
        with git_repo() as root:
            initialize_workspace(root)
            target = root / "draft.md"
            target.write_text("handoff notes\n", encoding="utf-8")
            result = create_snapshot(root)
            target.unlink()

            restore = restore_snapshot(
                root, snapshot_ref=result.snapshot_id, apply=True
            )

            self.assertTrue(restore.applied)
            self.assertEqual(target.read_text(encoding="utf-8"), "handoff notes\n")
            self.assertEqual(restore.copied_untracked, ("draft.md",))

    def test_restore_rejects_corrupt_captured_untracked_file(self) -> None:
        with git_repo() as root:
            initialize_workspace(root)
            target = root / "draft.md"
            target.write_text("handoff notes\n", encoding="utf-8")
            result = create_snapshot(root)
            (result.path / "untracked" / "draft.md").write_text(
                "tampered\n", encoding="utf-8"
            )
            target.unlink()

            with self.assertRaisesRegex(AgentSyncError, "changed"):
                restore_snapshot(root, snapshot_ref=result.snapshot_id, apply=True)

    def test_restore_rejects_unsafe_manifest_path(self) -> None:
        with git_repo() as root:
            initialize_workspace(root)
            target = root / "draft.md"
            target.write_text("handoff notes\n", encoding="utf-8")
            result = create_snapshot(root)
            manifest = result.path / "untracked_manifest.json"
            manifest.write_text(
                manifest.read_text(encoding="utf-8").replace(
                    "draft.md", "../outside.md"
                ),
                encoding="utf-8",
            )
            target.unlink()

            with self.assertRaisesRegex(AgentSyncError, "Unsafe snapshot path"):
                restore_snapshot(root, snapshot_ref=result.snapshot_id, apply=True)

    def test_verify_snapshot_reports_integrity_summary(self) -> None:
        with git_repo() as root:
            initialize_workspace(root)
            (root / "draft.md").write_text("handoff notes\n", encoding="utf-8")
            snapshot = create_snapshot(root)

            result = verify_snapshot(root, snapshot_ref=snapshot.snapshot_id)

            self.assertEqual(result.snapshot_id, snapshot.snapshot_id)
            self.assertEqual(result.captured_untracked, 1)
            self.assertEqual(result.skipped_untracked, 0)

    def test_list_snapshots_newest_first(self) -> None:
        with git_repo() as root:
            initialize_workspace(root)
            first = create_snapshot(root, message="first")
            second = create_snapshot(root, message="second")

            snapshots = list_snapshots(root)

            self.assertEqual(
                [snapshots[0]["id"], snapshots[1]["id"]],
                [second.snapshot_id, first.snapshot_id],
            )

    def test_export_import_bundle_roundtrip(self) -> None:
        with git_repo() as source:
            initialize_workspace(source)
            (source / "handoff.md").write_text("next machine\n", encoding="utf-8")
            snapshot = create_snapshot(source, message="portable")
            bundle = export_snapshot_bundle(source, snapshot_ref=snapshot.snapshot_id)

            with git_repo() as target:
                initialize_workspace(target)
                imported = import_snapshot_bundle(target, bundle=bundle.path)
                restore = restore_snapshot(
                    target, snapshot_ref=imported.snapshot_id, apply=True
                )

                self.assertEqual(imported.snapshot_id, snapshot.snapshot_id)
                self.assertFalse(imported.replaced)
                self.assertEqual(restore.copied_untracked, ("handoff.md",))
                self.assertEqual(
                    (target / "handoff.md").read_text(encoding="utf-8"),
                    "next machine\n",
                )

    def test_import_rejects_unsafe_bundle_member(self) -> None:
        with git_repo() as root:
            bundle = root / "unsafe.wfbundle.tar.gz"
            with tarfile.open(bundle, "w:gz") as archive:
                data = b"escape"
                info = tarfile.TarInfo("../escape.txt")
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))

            with self.assertRaisesRegex(AgentSyncError, "Unsafe bundle member path"):
                import_snapshot_bundle(root, bundle=bundle)

    def test_doctor_reports_initialized_workspace(self) -> None:
        with git_repo() as root:
            initialize_workspace(root)

            checks = run_doctor(root)

            self.assertFalse(any(check.status == "error" for check in checks))
            self.assertIn("WaveForward config", {check.name for check in checks})

    def test_doctor_reports_non_git_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checks = run_doctor(Path(directory))

            self.assertEqual(checks[0].status, "error")
            self.assertEqual(checks[0].name, "git repository")


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
