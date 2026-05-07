# Install WaveForward

WaveForward installs into an isolated Python virtual environment and exposes a
`waveforward` command. The installer also creates a `wf` shortcut when that name
is not already used on the machine.

## Requirements

- Python 3.12 or newer.
- `python3-venv`, or `uv` as an automatic installer fallback.
- Git when installing from a repository URL.
- systemd user services when installing a persistent Linux daemon.

## Install With Curl

```bash
curl -fsSL https://waveforward.tech/install.sh | sh
```

Then confirm:

```bash
waveforward --version
wf --version
waveforward doctor
waveforward daemon-status
```

The installer updates common shell profile files by default. Restart the shell,
or run the printed `export PATH=...` command for the current terminal if
`waveforward` is not found.

## Shortcut Behavior

By default, the installer creates `~/.local/bin/wf` only when no other `wf`
command is already found on `PATH`.

Disable the shortcut:

```bash
WAVEFORWARD_INSTALL_WF_ALIAS=0 curl -fsSL https://waveforward.tech/install.sh | sh
```

Force the shortcut:

```bash
WAVEFORWARD_INSTALL_WF_ALIAS=force curl -fsSL https://waveforward.tech/install.sh | sh
```

## Install From A Checkout

```bash
scripts/install.sh --source .
```

Install from GitHub:

```bash
scripts/install.sh --repo https://github.com/voltjia/waveforward.git --ref main
```

## Connect A Daemon

After installing the command, generate a setup token from a WaveForward service
and run the generated daemon command inside the workspace that should execute
agent turns:

```bash
waveforward daemon-start \
  --server https://waveforward.tech \
  --auth-token '<setup-token>' \
  --machine 'Laptop' \
  --allow-agent-execution
```

After first registration, the daemon stores its machine token under
`.waveforward/daemon.json` and writes background process state under
`.waveforward/daemon.pid` and `.waveforward/daemon.log`. Keep these files
private.

## Install A Persistent Linux Daemon

```bash
scripts/install-daemon-service.sh \
  --workspace /path/to/workspace \
  --server https://waveforward.tech \
  --token '<setup-token>' \
  --machine 'Desktop'
```

Useful commands:

```bash
systemctl --user status waveforward-daemon.service
systemctl --user restart waveforward-daemon.service
journalctl --user -u waveforward-daemon.service -f
waveforward daemon-status
```

The setup token is stored in `~/.config/waveforward/waveforward-daemon.env`
with file mode `0600`. It is short-lived; after the first successful
registration, the daemon continues with its local machine token.

## Upgrade

Run the installer again:

```bash
curl -fsSL https://waveforward.tech/install.sh | sh
```

Or install from a verified release manifest:

```bash
waveforward update-check waveforward-release-manifest.json --verify-asset
waveforward update-install waveforward-release-manifest.json --apply
```
