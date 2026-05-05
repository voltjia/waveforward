# Release Process

WaveForward Core releases are tag-based.

## Maintainer Checklist

1. Confirm the working tree is clean.
2. Run:

   ```bash
   uvx ruff format --check .
   uvx ruff check .
   python -m unittest discover -s tests
   sh -n scripts/install.sh && sh -n scripts/install-daemon-service.sh
   ```

3. Update the version in `pyproject.toml` and `src/waveforward/__init__.py`.
4. Create a signed tag when possible:

   ```bash
   git tag -s v0.1.0 -m "v0.1.0"
   git push origin v0.1.0
   ```

5. Confirm the release contains:

   - wheel
   - source distribution
   - `waveforward-release-manifest.json`
   - `SHA256SUMS`

The GitHub release workflow builds distributions, creates the manifest, and
uploads release assets.

## PyPI

Publish to PyPI only through Trusted Publishing. See `docs/pypi.md`.

## Installer Channel

The public installer at `https://waveforward.tech/install.sh` should point at
the latest stable release manifest from this repository before the repository is
made public.
