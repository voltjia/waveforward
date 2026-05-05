# PyPI Publishing

WaveForward should publish to PyPI with PyPI Trusted Publishing. Do not store a
PyPI password or long-lived API token in GitHub secrets.

## Why Publish To PyPI

PyPI gives Python users a familiar installation path and reserves the
`waveforward` package name:

```bash
python -m pip install waveforward
```

The curl installer remains useful because it creates an isolated environment and
installs the `waveforward` and `wf` commands without requiring users to manage a
project environment manually.

## One-Time PyPI Setup

1. Create the `waveforward` project on PyPI. The name was not present when this
   document was written.
2. Add a Trusted Publisher for GitHub Actions:

   - Owner: `voltjia`
   - Repository: `waveforward`
   - Workflow: `publish-pypi.yml`
   - Environment: `pypi`

3. In GitHub, create the `pypi` environment. Require manual approval before
   deployment if you want an extra release gate.

## Publish

1. Confirm the GitHub release workflow has produced and verified release
   artifacts for the tag.
2. Run the `Publish PyPI` workflow manually.
3. Enter the release tag, such as `v0.1.0`.
4. Verify:

   ```bash
   python -m pip index versions waveforward
   python -m pip install --upgrade waveforward
   waveforward --version
   ```

## Release Ordering

Recommended order:

1. Merge release commit to `main`.
2. Push tag.
3. Let GitHub release workflow build artifacts and manifest.
4. Publish the same tag to PyPI.
5. Verify both GitHub release and PyPI package.
