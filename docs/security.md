# Security Model

WaveForward Core is designed to make machine-side behavior auditable. The hosted
service can coordinate a conversation, but the local daemon is the component
that reads a workspace and invokes local agent tools.

## Workspace Access

WaveForward stores metadata under `.waveforward`. Snapshot and restore commands
read Git state, staged and unstaged diffs, and small untracked files. Do not run
WaveForward in a workspace that contains data you do not want captured.

## Agent Execution

WaveForward supports non-interactive local agent commands. Some supported tools
require flags that allow automatic edits or skip normal confirmation prompts.
Treat any connected daemon as capable of invoking the configured agent in the
workspace. The app-generated background daemon command includes an explicit
local acknowledgement:

```bash
waveforward daemon-start --allow-agent-execution ...
```

Use a dedicated workspace and review the connected service before starting a
daemon. Manual foreground daemon runs should only be used against a service you
control and trust.

## Tokens

Daemon setup tokens are short-lived. After registration, the local daemon stores
a machine token in `~/.waveforward/daemon.json`. The CLI redacts token values
from status output, but the file itself is sensitive.

## Updates

Release manifests include SHA-256 hashes for wheel artifacts. Use
`waveforward update-check --verify-asset` before applying updates.
