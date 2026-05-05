# Contributing

WaveForward Core is the machine-side runtime. Keep changes focused on local
workspace portability, daemon connectivity, agent runners, installation, and
release verification.

## Development

```bash
python -m pip install -e ".[dev]"
uvx ruff format .
uvx ruff check .
python -m unittest discover -s tests
sh -n scripts/install.sh && sh -n scripts/install-daemon-service.sh
```

Use Conventional Commits for commit messages.

## Scope

Suitable for this repository:

- Local snapshot, restore, bundle, and handoff behavior.
- Daemon client runtime.
- Local agent command execution.
- Installer and release verification logic.
- Public documentation for local security expectations.

Not suitable for this repository:

- Hosted app UI.
- Multi-user auth, invitations, email verification, billing, or admin tools.
- Production deployment automation for the hosted service.
- Private release distribution or dogfood notes.

## Security-Sensitive Changes

Be conservative with code that touches tokens, local workspaces, subprocess
execution, downloads, release verification, or archive extraction. Add tests for
failure paths and avoid logging plaintext credentials.
