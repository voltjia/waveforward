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
For this reason, agent execution requires explicit opt-in:

```bash
waveforward daemon-start --allow-agent-execution ...
```

The app-generated setup command includes this flag for the selected workspace.
For manual foreground daemon runs, set `WAVEFORWARD_ALLOW_UNSAFE_AGENT_EXECUTION=1`
or pass `waveforward daemon --allow-agent-execution ...`. Use a dedicated
workspace and review the connected service before enabling this setting.

## Tokens

Daemon setup tokens are short-lived. After registration, the local daemon stores
a machine token in `.waveforward/daemon.json`. The CLI redacts token values from
status output, but the file itself is sensitive.

## Updates

Release manifests include SHA-256 hashes for wheel artifacts. Use
`waveforward update-check --verify-asset` before applying updates.
