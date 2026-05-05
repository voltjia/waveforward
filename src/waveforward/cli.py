"""Command line interface for WaveForward Core."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from waveforward import __version__
from waveforward.core import (
    DEFAULT_MAX_FILE_BYTES,
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
from waveforward.daemon import (
    UNSAFE_AGENT_EXECUTION_ENV,
    DaemonConfig,
    daemon_status,
    run_daemon,
    start_daemon_process,
)
from waveforward.update import check_for_update, download_update_wheel


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="waveforward",
        description="Capture and hand off portable coding-agent work state.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )

    subcommands = parser.add_subparsers(dest="command", required=True)

    init_parser = subcommands.add_parser(
        "init", help="Initialize .waveforward metadata."
    )
    init_parser.add_argument("--machine", help="Human-readable machine name.")
    init_parser.add_argument(
        "--force",
        action="store_true",
        help="Rewrite the existing local config.",
    )
    init_parser.set_defaults(func=_cmd_init)

    snapshot_parser = subcommands.add_parser(
        "snapshot",
        help="Capture the current workspace state.",
    )
    snapshot_parser.add_argument("-m", "--message", default="", help="Snapshot note.")
    snapshot_parser.add_argument("--task", default="", help="Current task summary.")
    snapshot_parser.add_argument(
        "--no-untracked",
        action="store_true",
        help="Do not copy small untracked files into the snapshot.",
    )
    snapshot_parser.add_argument(
        "--max-file-bytes",
        type=int,
        default=DEFAULT_MAX_FILE_BYTES,
        help="Maximum size for each captured untracked file.",
    )
    snapshot_parser.set_defaults(func=_cmd_snapshot)

    list_parser = subcommands.add_parser("list", help="List local snapshots.")
    list_parser.set_defaults(func=_cmd_list)

    handoff_parser = subcommands.add_parser(
        "handoff",
        help="Generate a Markdown handoff for another agent.",
    )
    handoff_parser.add_argument("snapshot", nargs="?", default="latest")
    handoff_parser.add_argument(
        "--to",
        default="generic",
        help="Target agent name, such as codex or claude-code.",
    )
    handoff_parser.set_defaults(func=_cmd_handoff)

    export_parser = subcommands.add_parser(
        "export",
        help="Export a snapshot as a portable bundle.",
    )
    export_parser.add_argument("snapshot", nargs="?", default="latest")
    export_parser.add_argument("-o", "--output", help="Bundle output path.")
    export_parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing bundle path.",
    )
    export_parser.set_defaults(func=_cmd_export)

    import_parser = subcommands.add_parser(
        "import",
        help="Import a portable snapshot bundle.",
    )
    import_parser.add_argument("bundle", help="Path to a .wfbundle.tar.gz file.")
    import_parser.add_argument(
        "--replace",
        action="store_true",
        help="Replace a local snapshot with the same id.",
    )
    import_parser.set_defaults(func=_cmd_import)

    verify_parser = subcommands.add_parser(
        "verify",
        help="Verify a local snapshot without restoring it.",
    )
    verify_parser.add_argument("snapshot", nargs="?", default="latest")
    verify_parser.set_defaults(func=_cmd_verify)

    restore_parser = subcommands.add_parser(
        "restore",
        help="Restore or preview a captured snapshot.",
    )
    restore_parser.add_argument("snapshot", nargs="?", default="latest")
    restore_parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply patches and copy captured untracked files.",
    )
    restore_parser.add_argument(
        "--force",
        action="store_true",
        help="Allow restore into a dirty workspace or overwrite untracked files.",
    )
    restore_parser.set_defaults(func=_cmd_restore)

    doctor_parser = subcommands.add_parser(
        "doctor",
        help="Check local WaveForward workspace health.",
    )
    doctor_parser.set_defaults(func=_cmd_doctor)

    daemon_parser = subcommands.add_parser(
        "daemon",
        help="Connect this machine to a WaveForward service.",
    )
    daemon_parser.add_argument("--server", help="WaveForward service URL.")
    daemon_parser.add_argument("--auth-user", help="Basic Auth username.")
    daemon_parser.add_argument("--auth-password", help="Basic Auth password.")
    daemon_parser.add_argument("--auth-token", help="Bearer token.")
    daemon_parser.add_argument("--machine", help="Human-readable machine name.")
    daemon_parser.add_argument(
        "--poll-interval",
        default=2.0,
        type=float,
        help="Seconds between daemon polls.",
    )
    daemon_parser.add_argument(
        "--once",
        action="store_true",
        help="Register, poll once, then exit. Mostly useful for tests.",
    )
    daemon_parser.add_argument(
        "--allow-agent-execution",
        action="store_true",
        help="Allow WaveForward to run local coding agents in this workspace.",
    )
    daemon_parser.set_defaults(func=_cmd_daemon)

    daemon_start_parser = subcommands.add_parser(
        "daemon-start",
        help="Start the WaveForward daemon in the background.",
    )
    daemon_start_parser.add_argument("--server", help="WaveForward service URL.")
    daemon_start_parser.add_argument("--auth-user", help="Basic Auth username.")
    daemon_start_parser.add_argument("--auth-password", help="Basic Auth password.")
    daemon_start_parser.add_argument("--auth-token", help="Bearer token.")
    daemon_start_parser.add_argument("--machine", help="Human-readable machine name.")
    daemon_start_parser.add_argument(
        "--poll-interval",
        default=2.0,
        type=float,
        help="Seconds between daemon polls.",
    )
    daemon_start_parser.add_argument(
        "--allow-agent-execution",
        action="store_true",
        help="Allow WaveForward to run local coding agents in this workspace.",
    )
    daemon_start_parser.set_defaults(func=_cmd_daemon_start)

    daemon_status_parser = subcommands.add_parser(
        "daemon-status",
        help="Show local daemon machine-token state.",
    )
    daemon_status_parser.set_defaults(func=_cmd_daemon_status)

    update_check_parser = subcommands.add_parser(
        "update-check",
        help="Check a release manifest for available updates.",
    )
    update_check_parser.add_argument(
        "manifest",
        help="Release manifest path or URL.",
    )
    update_check_parser.add_argument(
        "--current-version",
        default=__version__,
        help="Current version to compare. Defaults to this CLI version.",
    )
    update_check_parser.add_argument(
        "--current-commit",
        default="",
        help="Current release commit to compare when available.",
    )
    update_check_parser.add_argument(
        "--verify-asset",
        action="store_true",
        help="Download/read the manifest wheel and verify its SHA-256.",
    )
    update_check_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON.",
    )
    update_check_parser.add_argument(
        "--exit-code",
        action="store_true",
        help="Return exit code 2 when an update is available.",
    )
    update_check_parser.set_defaults(func=_cmd_update_check)

    update_install_parser = subcommands.add_parser(
        "update-install",
        help="Verify and install a release manifest update.",
    )
    update_install_parser.add_argument(
        "manifest",
        help="Release manifest path or URL.",
    )
    update_install_parser.add_argument(
        "--current-version",
        default=__version__,
        help="Current version to compare. Defaults to this CLI version.",
    )
    update_install_parser.add_argument(
        "--current-commit",
        default="",
        help="Current release commit to compare when available.",
    )
    update_install_parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python executable whose pip should install the update.",
    )
    update_install_parser.add_argument(
        "--apply",
        action="store_true",
        help="Install the verified update. Without this, only verify and preview.",
    )
    update_install_parser.add_argument(
        "--force",
        action="store_true",
        help="Install even when the manifest does not compare newer.",
    )
    update_install_parser.add_argument(
        "--json",
        action="store_true",
        help="Print machine-readable JSON.",
    )
    update_install_parser.set_defaults(func=_cmd_update_install)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.func(args)
    except AgentSyncError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    return int(result or 0)


def _cmd_init(args: argparse.Namespace) -> None:
    path = initialize_workspace(Path.cwd(), machine_name=args.machine, force=args.force)
    print(f"Initialized WaveForward metadata at {path}")


def _cmd_snapshot(args: argparse.Namespace) -> None:
    result = create_snapshot(
        Path.cwd(),
        message=args.message,
        task=args.task,
        include_untracked=not args.no_untracked,
        max_file_bytes=args.max_file_bytes,
    )
    captured = result.metadata["capture"]["untracked_files"]
    skipped = result.metadata["capture"]["skipped_untracked_files"]
    print(f"Created snapshot {result.snapshot_id}")
    print(f"Path: {result.path}")
    print(f"Captured untracked files: {captured}; skipped: {skipped}")


def _cmd_list(_args: argparse.Namespace) -> None:
    snapshots = list_snapshots(Path.cwd())
    if not snapshots:
        print("No snapshots yet.")
        return

    for item in snapshots:
        branch = item["git"].get("branch") or "(no branch)"
        message = item.get("message") or ""
        suffix = f" - {message}" if message else ""
        print(f"{item['id']}  {item['created_at']}  {branch}{suffix}")


def _cmd_handoff(args: argparse.Namespace) -> None:
    path = generate_handoff(Path.cwd(), snapshot_ref=args.snapshot, target=args.to)
    print(f"Generated handoff: {path}")


def _cmd_export(args: argparse.Namespace) -> None:
    result = export_snapshot_bundle(
        Path.cwd(),
        snapshot_ref=args.snapshot,
        output=args.output,
        overwrite=args.force,
    )
    print(f"Exported snapshot {result.snapshot_id}")
    print(f"Bundle: {result.path}")
    print(f"Bytes: {result.bytes}")


def _cmd_import(args: argparse.Namespace) -> None:
    result = import_snapshot_bundle(
        Path.cwd(),
        bundle=args.bundle,
        replace=args.replace,
    )
    action = "Replaced" if result.replaced else "Imported"
    print(f"{action} snapshot {result.snapshot_id}")
    print(f"Path: {result.path}")


def _cmd_verify(args: argparse.Namespace) -> None:
    result = verify_snapshot(Path.cwd(), snapshot_ref=args.snapshot)
    print(f"Verified snapshot {result.snapshot_id}")
    print(f"Path: {result.path}")
    print(f"Captured untracked files: {result.captured_untracked}")
    print(f"Skipped untracked files: {result.skipped_untracked}")


def _cmd_restore(args: argparse.Namespace) -> None:
    result = restore_snapshot(
        Path.cwd(),
        snapshot_ref=args.snapshot,
        apply=args.apply,
        force=args.force,
    )

    if not args.apply:
        print(f"Restore preview for snapshot {result.snapshot_id}")
        print(f"Patches: {', '.join(result.patches) if result.patches else 'none'}")
        print(f"Untracked files: {len(result.copied_untracked)}")
        if result.untracked_collisions:
            print("Existing files would collide:")
            for path in result.untracked_collisions:
                print(f"  {path}")
        print("Re-run with --apply to restore.")
        return

    print(f"Restored snapshot {result.snapshot_id}")
    print(f"Applied patches: {', '.join(result.patches) if result.patches else 'none'}")
    print(f"Copied untracked files: {len(result.copied_untracked)}")


def _cmd_doctor(_args: argparse.Namespace) -> int:
    checks = run_doctor(Path.cwd())
    for check in checks:
        print(f"[{check.status.upper()}] {check.name}: {check.detail}")
    return 1 if any(check.status == "error" for check in checks) else 0


def _daemon_config_from_args(args: argparse.Namespace) -> DaemonConfig:
    return DaemonConfig(
        server=args.server or os.getenv("WAVEFORWARD_DAEMON_SERVER", ""),
        auth_user=args.auth_user or os.getenv("WAVEFORWARD_DAEMON_USER"),
        auth_password=args.auth_password or os.getenv("WAVEFORWARD_DAEMON_PASSWORD"),
        auth_token=args.auth_token or os.getenv("WAVEFORWARD_DAEMON_TOKEN"),
        machine_name=args.machine or os.getenv("WAVEFORWARD_DAEMON_MACHINE"),
        poll_interval=args.poll_interval,
    )


def _cmd_daemon(args: argparse.Namespace) -> None:
    config = _daemon_config_from_args(args)
    if not config.server:
        raise AgentSyncError("Missing --server or WAVEFORWARD_DAEMON_SERVER.")
    if args.allow_agent_execution:
        os.environ[UNSAFE_AGENT_EXECUTION_ENV] = "1"
    print(f"Connecting WaveForward daemon to {config.server}")
    try:
        run_daemon(Path.cwd(), config=config, once=args.once)
    except KeyboardInterrupt:
        print("\nStopping WaveForward daemon.")


def _cmd_daemon_start(args: argparse.Namespace) -> None:
    config = _daemon_config_from_args(args)
    if not config.server:
        raise AgentSyncError("Missing --server or WAVEFORWARD_DAEMON_SERVER.")
    result = start_daemon_process(
        Path.cwd(),
        config=config,
        allow_agent_execution=args.allow_agent_execution,
    )
    if result["started"]:
        print("Started WaveForward daemon in the background.")
    else:
        print("WaveForward daemon is already running.")
    print(f"PID: {result['pid']}")
    print(f"Log: {result['log_path']}")


def _cmd_daemon_status(_args: argparse.Namespace) -> None:
    status = daemon_status(Path.cwd())
    print(f"Configured: {'yes' if status['configured'] else 'no'}")
    print(f"Machine id: {status['machine_id'] or '-'}")
    print(f"Machine token: {'present' if status['has_machine_token'] else 'missing'}")
    print(f"Daemon running: {'yes' if status['running'] else 'no'}")
    print(f"PID: {status['pid'] or '-'}")
    print(f"Log path: {status['log_path']}")
    print(f"State path: {status['state_path']}")


def _cmd_update_check(args: argparse.Namespace) -> int:
    result = check_for_update(
        args.manifest,
        current_version=args.current_version,
        current_commit=args.current_commit,
        verify_asset=args.verify_asset,
    )
    if args.json:
        print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    else:
        print(f"Current version: {result.current_version}")
        print(f"Current commit: {result.current_commit or '-'}")
        print(f"Latest version: {result.latest_version}")
        print(f"Latest commit: {result.latest_commit or '-'}")
        print(f"Wheel: {result.wheel_url}")
        print(f"Wheel SHA-256: {result.wheel_sha256}")
        print(f"Wheel verified: {'yes' if result.verified else 'not requested'}")
        print(f"Update available: {'yes' if result.update_available else 'no'}")
        print(f"Reason: {result.reason}")
    return 2 if args.exit_code and result.update_available else 0


def _cmd_update_install(args: argparse.Namespace) -> int:
    result = check_for_update(
        args.manifest,
        current_version=args.current_version,
        current_commit=args.current_commit,
        verify_asset=True,
    )
    should_install = bool(args.force or result.update_available)
    payload = {
        **result.as_dict(),
        "applied": False,
        "force": bool(args.force),
        "python": args.python,
        "would_install": should_install,
    }
    if not should_install:
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print("No WaveForward update will be installed.")
            print(f"Reason: {result.reason}")
        return 0

    if not args.apply:
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"Verified update: {result.latest_version}")
            print(f"Wheel: {result.wheel_url}")
            print("Re-run with --apply to install it.")
        return 0

    with tempfile.TemporaryDirectory() as temp:
        wheel = download_update_wheel(args.manifest, temp)
        _pip_install_update(args.python, wheel)

    payload["applied"] = True
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"Installed WaveForward update {result.latest_version}.")
        print("Restart any running WaveForward daemon.")
    return 0


def _pip_install_update(python: str, wheel: Path) -> None:
    executable = python.strip()
    if not executable:
        raise AgentSyncError("Python executable is required for update installation.")
    try:
        completed = subprocess.run(
            [
                executable,
                "-m",
                "pip",
                "install",
                "--upgrade",
                str(wheel),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise AgentSyncError(
            f"Could not run pip for update installation: {error}"
        ) from error
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        raise AgentSyncError(f"Update installation failed{suffix}")


if __name__ == "__main__":
    raise SystemExit(main())
