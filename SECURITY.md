# Security Policy

WaveForward Core runs on user-controlled machines and may execute configured
agent commands inside user-selected workspaces. Treat it as local developer
infrastructure with access to source code, Git metadata, and agent credentials
available in that environment.

## Reporting A Vulnerability

Do not open a public issue for a suspected vulnerability. Use GitHub private
vulnerability reporting or contact the maintainers privately. Include the
affected version, operating system, reproduction steps, and expected impact.

## Local Trust Model

- `.waveforward/daemon.json` can contain a long-lived machine token. Keep it
  private and do not commit it.
- The daemon polls a configured WaveForward-compatible service for jobs. Only
  connect to a service you trust.
- Agent execution is disabled by default for commands that use automatic edit or
  permission-bypass modes. Set `WAVEFORWARD_ALLOW_UNSAFE_AGENT_EXECUTION=1` only
  for workspaces where this behavior is intended.
- Installers and updates should verify release manifest hashes before applying
  code.

## Supported Versions

Security fixes target the latest released version unless otherwise stated.
