# WaveForward

WaveForward is the open-core local runtime for portable coding-agent session
continuity. It captures recoverable workspace state, prepares handoff context,
and runs the machine-side daemon that connects a workstation or server to a
WaveForward-compatible service.

This repository is intentionally limited to code that users may run on their
own machines. Hosted app code, multi-user auth, deployment automation, private
release distribution, and cloud administration live outside this repository.

## What Is Included

- Local workspace metadata initialization.
- Git-based snapshots of status, staged diff, unstaged diff, and small
  untracked files.
- Portable snapshot bundles for moving work between machines.
- Restore preview and restore application.
- Markdown handoff generation for agent continuation.
- Local agent command runners for Codex, Claude Code, and OpenCode.
- A daemon client that connects a local workspace to a WaveForward service.
- Release manifest verification and update helpers.

## Status

WaveForward Core is alpha software. The public API, release format, and service
protocol may change before a stable release.

## Install

The intended public installer path is:

```bash
curl -fsSL https://waveforward.tech/install.sh | sh
```

After installation, both commands are available:

```bash
waveforward --version
wf --version
```

After the PyPI project is configured, Python users can also install with:

```bash
python -m pip install waveforward
```

## Local Development

Run tests:

```bash
python3 -m unittest discover -s tests
```

Run the CLI from a checkout:

```bash
PYTHONPATH=src python3 -m waveforward.cli --help
```

Format and lint:

```bash
uvx ruff format .
uvx ruff check .
```

## Security Boundary

WaveForward may inspect local Git working trees and execute configured agent
commands inside user-selected workspaces. Treat daemon tokens and
`.waveforward/daemon.json` as sensitive local credentials. Do not commit
`.waveforward` state, API keys, agent credentials, or release artifacts that
should not be public.

Agent execution may use automatic edit or permission-bypass modes depending on
the selected agent. The app-generated `waveforward daemon-start` command includes
an explicit local acknowledgement for the selected workspace:

```bash
waveforward daemon-start --allow-agent-execution ...
```

See `docs/security.md` and `SECURITY.md`.

## License

WaveForward is licensed under the Apache License, Version 2.0. See
`LICENSE` and `NOTICE`.
