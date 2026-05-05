# Public Readiness Checklist

Before changing this repository from private to public:

- Confirm this repository contains no private Git history from
  `waveforward-cloud`.
- Run secret scanning against the full repository history.
- Confirm `.github/workflows/ci.yml` passes on GitHub.
- Confirm the release workflow can publish a private test tag.
- Confirm `https://waveforward.tech/install.sh` uses the public core release
  manifest.
- Confirm issue templates, security reporting, and repository topics are set.
- Configure PyPI Trusted Publishing before publishing to PyPI.
- Review `README.md`, `SECURITY.md`, `CONTRIBUTING.md`, and `docs/security.md`.
- Confirm no hosted app, auth, billing, deployment, dogfood, customer, or
  private infrastructure files are present.
